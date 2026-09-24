"""Finite-candidate scoring seam; no assumption about token generation or NIM support."""
import math
from collections.abc import Mapping
from typing import Protocol
from .models import Observation


class CandidateScorer(Protocol):
    def score(self, observation: Observation) -> Mapping[str, float]: ...


def choose_action(observation: Observation, scorer: CandidateScorer) -> str:
    ids = [a.id for a in observation.actions]
    if not ids:
        raise ValueError("No candidate actions at this boundary")
    scores = scorer.score(observation)
    if set(scores) != set(ids):
        raise ValueError("Scorer must return exactly one score for every current candidate")
    if not all(math.isfinite(v) for v in scores.values()):
        raise ValueError("All scores must be finite")
    return max(ids, key=scores.__getitem__)  # stable candidate-order tie break
