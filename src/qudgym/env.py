"""Native structured environment; not dependent on Gymnasium or a model SDK."""
from pydantic import ValidationError

from .backend import Backend
from .errors import QudGymError, TransportUncertain
from .models import Observation, Transition


class QudEnv:
    def __init__(self, backend: Backend):
        self.backend = backend
        self.current: Transition | None = None
        self._cursor: str | None = None
        self.uncertain = False
        self.reward_unknown = False

    def _guard(self):
        if self.uncertain:
            raise QudGymError("reconcile_required",
                              "Replay the uncertain request or reconcile before a new mutation")

    def _accept(self, result: Transition) -> Transition:
        self.current = result
        self._cursor = result.observation.decision_id
        self.uncertain = False
        self.reward_unknown = False
        return result

    def _invoke(self, call):
        self._guard()
        try:
            result = call()
        except TransportUncertain as exc:
            if exc.replayable:
                self.uncertain = True
            raise
        return self._accept(result)

    def reset(self, *, seed: int = 0) -> Transition:
        return self._invoke(lambda: self.backend.reset(seed=seed))

    def step(self, action_id: str) -> Transition:
        self._guard()
        if self._cursor is None:
            raise QudGymError("reset_required", "Reset before stepping")
        return self._invoke(lambda: self.backend.step(action_id, decision_id=self._cursor))

    def restore(self, handle: str) -> Transition:
        return self._invoke(lambda: self.backend.restore(handle))

    def replay(self) -> Transition:
        if not self.uncertain:
            raise QudGymError("nothing_to_replay", "No uncertain mutation to replay")
        replay = getattr(self.backend, "replay", None)
        if replay is None:
            raise QudGymError("unsupported", "This backend cannot replay an uncertain request")
        result = replay()
        try:
            transition = Transition.model_validate(result)
        except ValidationError as exc:
            raise QudGymError("nothing_to_replay", "In-flight request did not return a transition") from exc
        return self._accept(transition)

    def reconcile(self) -> Observation:
        # The lost reply's reward is not recoverable from observe(). Leave it unknown.
        observation = self.backend.observe()
        abandon = getattr(self.backend, "abandon_uncertain", None)
        if abandon is not None:
            abandon()
        self._cursor = observation.decision_id
        self.current = None
        self.uncertain = False
        self.reward_unknown = True
        return observation

    def close(self) -> None:
        self.backend.close()
        self.current = None
        self._cursor = None
        self.uncertain = False
        self.reward_unknown = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
