"""Native structured environment; not dependent on Gymnasium or a model SDK."""
from .backend import Backend
from .errors import QudGymError
from .models import Transition


class QudEnv:
    def __init__(self, backend: Backend):
        self.backend = backend
        self.current: Transition | None = None

    def reset(self, *, seed: int = 0) -> Transition:
        self.current = self.backend.reset(seed=seed)
        return self.current

    def step(self, action_id: str) -> Transition:
        if self.current is None:
            raise QudGymError("reset_required", "Reset before stepping")
        self.current = self.backend.step(action_id, decision_id=self.current.observation.decision_id)
        return self.current

    def restore(self, handle: str) -> Transition:
        self.current = self.backend.restore(handle)
        return self.current

    def close(self) -> None:
        self.backend.close()
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
