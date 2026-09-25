"""Typed task-row contract for the mock QudGym NeMo Gym resources server."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskData(BaseModel):
    """Server-owned task fields; none are added to the model observation."""

    model_config = ConfigDict(extra="allow")

    seed: int = Field(default=0, ge=0, le=2**32 - 1, strict=True)
    max_decisions: int = Field(default=128, ge=1, le=10_000, strict=True)
    representation: Literal["native", "agent-eye-v1"] = "native"
