"""Opt-in NeMo observation presentation; no model, tokenizer, or trainer proxy."""
from typing import Literal

from ..models import Observation
from .encoding import text
from .legacy import from_observation
from .memory import EvidenceMemory

Representation = Literal["native", "agent-eye-v1"]


class ObservationPresenter:
    def __init__(self, representation: Representation = "native"):
        if representation not in ("native", "agent-eye-v1"):
            raise ValueError("unsupported observation representation")
        self.representation = representation
        self.memory = EvidenceMemory()
        self._branch_id: str | None = None
        self._last_decision_id: str | None = None
        self._last_parent_decision_id: str | None = None

    def render(self, observation: Observation) -> str:
        if self.representation == "native":
            return observation.model_dump_json()
        if self._branch_id != observation.episode_id:
            # A new episode is an explicit memory boundary. A restore within an
            # episode must be rebuilt by the caller with a new branch ID.
            self.memory.reset()
            self._branch_id = observation.episode_id
            self._last_decision_id = None
            self._last_parent_decision_id = None
        parent = (self._last_decision_id
                  if self._last_decision_id != observation.decision_id
                  else self._last_parent_decision_id)
        frame = from_observation(
            observation,
            branch_id=self._branch_id,
            parent_decision_id=parent,
        )
        try:
            rendered = text(self.memory.update(frame))
        except ValueError:
            # Do not leave a committed memory prefix behind when the serialized
            # policy view cannot be emitted.
            self.memory.reset()
            self._last_decision_id = None
            self._last_parent_decision_id = None
            raise
        self._last_parent_decision_id = self._last_decision_id
        self._last_decision_id = observation.decision_id
        return rendered
