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

    def render(self, observation: Observation) -> str:
        if self.representation == "native":
            return observation.model_dump_json()
        return text(self.memory.update(from_observation(observation)))
