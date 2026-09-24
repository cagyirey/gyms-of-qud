"""Versioned wire types. Observations never contain oracle state or credentials."""
from __future__ import annotations

from typing import Annotated, ClassVar, Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

WIRE_VERSION = "0.1"
Identifier = Annotated[str, Field(min_length=1, max_length=160)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CandidateAction(Model):
    id: Identifier
    kind: Literal["move", "wait", "answer", "interact", "ability", "inventory"]
    label: str
    # Arguments are descriptive metadata, not an arbitrary command execution surface.
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class Player(Model):
    x: int
    y: int
    hp: int = Field(ge=0)
    max_hp: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_hp(self):
        if self.hp > self.max_hp:
            raise ValueError("hp exceeds max_hp")
        return self


class PerceivedEntity(Model):
    id: Identifier
    name: str
    x: int
    y: int
    # Deliberately no blueprint, true identity, precise enemy HP, or hidden effects.
    perceived_status: str | None = None


class Prompt(Model):
    kind: Literal["choice", "direction", "target", "text"]
    text: str


class Observation(Model):
    episode_id: Identifier
    decision_id: Identifier
    turn: int = Field(ge=0)
    phase: Literal["command", "prompt", "terminal"]
    player: Player
    tiles: tuple[str, ...]
    entities: tuple[PerceivedEntity, ...] = ()
    messages: tuple[str, ...] = ()
    prompt: Prompt | None = None
    actions: tuple[CandidateAction, ...]

    @model_validator(mode="after")
    def consistent_boundary(self):
        ids = [a.id for a in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique at a decision boundary")
        if (self.phase == "prompt") != (self.prompt is not None):
            raise ValueError("prompt phase and prompt payload disagree")
        if self.phase == "terminal" and self.actions:
            raise ValueError("terminal observations must not advertise actions")
        if self.phase != "terminal" and not self.actions:
            raise ValueError("a nonterminal boundary needs at least one candidate")
        if self.tiles and len({len(row) for row in self.tiles}) != 1:
            raise ValueError("tile rows must have equal widths")
        return self


class Metrics(Model):
    task_id: str
    objective_version: str
    outcome: Literal["ongoing", "success", "death", "time_limit"]
    turns_elapsed: int = Field(ge=0)
    decisions_elapsed: int = Field(ge=0)


class Transition(Model):
    observation: Observation
    reward: float
    terminated: bool
    truncated: bool
    metrics: Metrics

    @model_validator(mode="after")
    def consistent_end(self):
        if self.terminated and self.truncated:
            raise ValueError("this protocol uses mutually exclusive terminal outcomes")
        done = self.terminated or self.truncated
        if done != (self.observation.phase == "terminal"):
            raise ValueError("transition outcome and observation phase disagree")
        if self.terminated != (self.metrics.outcome in ("success", "death")):
            raise ValueError("terminated and outcome disagree")
        if self.truncated != (self.metrics.outcome == "time_limit"):
            raise ValueError("truncated and outcome disagree")
        return self


class Capabilities(Model):
    protocol_version: Literal["0.1"] = WIRE_VERSION
    backend: str
    game_build: str
    is_mock: bool
    snapshot: bool = False
    deterministic_restore: bool = False
    full_state_hash: bool = False


class SnapshotHandle(Model):
    handle: Identifier


class StateHash(Model):
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    scope: Literal["full_simulator_state"] = "full_simulator_state"


class Hello(Model):
    op: Literal["hello"] = "hello"


class Released(Model):
    released: Literal[True]


class Reset(Model):
    op: Literal["reset"] = "reset"
    seed: int = Field(default=0, ge=0, le=2**32 - 1, strict=True)
    result_model: ClassVar[type[Transition]] = Transition


class Observe(Model):
    op: Literal["observe"] = "observe"


class Step(Model):
    op: Literal["step"] = "step"
    decision_id: Identifier
    action_id: Identifier
    result_model: ClassVar[type[Transition]] = Transition


class Snapshot(Model):
    op: Literal["snapshot"] = "snapshot"
    result_model: ClassVar[type[SnapshotHandle]] = SnapshotHandle


class Restore(Model):
    op: Literal["restore"] = "restore"
    handle: Identifier
    result_model: ClassVar[type[Transition]] = Transition


class Release(Model):
    op: Literal["release"] = "release"
    handle: Identifier
    result_model: ClassVar[type[Released]] = Released


class HashState(Model):
    op: Literal["state_hash"] = "state_hash"


Operation = Annotated[
    Hello | Reset | Observe | Step | Snapshot | Restore | Release | HashState,
    Field(discriminator="op"),
]


class RpcRequest(Model):
    protocol_version: Literal["0.1"] = WIRE_VERSION
    request_id: Identifier
    operation: Operation


class RpcError(Model):
    code: str
    message: str


class RpcResponse(Model):
    protocol_version: Literal["0.1"] = WIRE_VERSION
    request_id: Identifier
    result: JsonValue | None = None
    error: RpcError | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.result is None) == (self.error is None):
            raise ValueError("exactly one of result or error is required")
        return self
