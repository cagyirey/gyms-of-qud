"""Agent-eye/1: player-information contracts, independent of controller RPC 0.1.

These types validate structure, not whether a game adapter obtained information
fairly. An adapter MUST collect through the player's permitted perception paths.
Never construct a Frame from an unrestricted game-object dictionary.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ref = Annotated[str, Field(min_length=1, max_length=160)]
# cell:{zone}:{x}:{y} keeps a full zone id plus coordinates through 1023.
MemorySubject = Annotated[str, Field(min_length=1, max_length=175)]
Text = Annotated[str, Field(max_length=8192)]
Tick = Annotated[int, Field(ge=0, strict=True)]
Scalar = str | int | float | bool | None
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_ZONES = 16
MAX_CELLS = 65_536
MAX_ENTITIES = 4_096
MAX_RELATIONS = 16_384
MAX_EVENTS = 4_096
MAX_ACTIONS = 4_096
MAX_HYPOTHESES = 256
MAX_VIEW_RECORDS = 4096
MAX_VIEW_EVENTS = 4096


class EyeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Evidence(EyeModel):
    """Source is a player-visible channel, never the engine field read to obtain it."""
    status: Literal["observed", "reported", "unknown"] = "observed"
    channel: Literal["vision", "hearing", "smell", "psychic", "electromagnetic",
                     "self", "inspection", "message", "dialogue", "journal", "unknown"]
    turn: Tick
    detail: Text = ""

    @model_validator(mode="after")
    def unknown_channel(self):
        if self.status == "unknown" and self.channel != "unknown":
            raise ValueError("unknown evidence requires unknown channel")
        if self.channel == "unknown" and self.status != "unknown":
            raise ValueError("unknown channel requires unknown evidence status")
        return self


class Fact(EyeModel):
    # Extend this explicit vocabulary with reviewed fields, not engine class names.
    attribute: Literal["name", "description", "hp", "max_hp", "count", "charges",
                       "ready", "cooldown", "range", "cost", "amount", "capacity",
                       "effect", "glyph", "terrain", "blocking", "attitude", "slot",
                       "anatomy", "temperature", "level", "skill", "mutation", "sensing",
                       "objective", "identity", "resource"]
    value: Scalar
    unit: Annotated[str, Field(max_length=64)] | None = None
    evidence: Evidence

    @model_validator(mode="after")
    def unknown_is_not_zero(self):
        if (self.value is None) != (self.evidence.status == "unknown"):
            raise ValueError("unknown facts have null values; known facts need a value")
        return self


class Position(EyeModel):
    zone: Ref
    x: Annotated[int, Field(ge=0, strict=True)]
    y: Annotated[int, Field(ge=0, strict=True)]
    evidence: Evidence

    @model_validator(mode="after")
    def known_location(self):
        if self.evidence.status == "unknown":
            raise ValueError("use location=null for an unknown position")
        return self


class Entity(EyeModel):
    """An observation-facing handle; never a promise of hidden engine identity."""
    id: Ref
    kind: Literal["actor", "contact", "item", "body_part", "ability", "effect",
                  "container", "faction", "quest", "landmark"]
    location: Position | None = None
    facts: tuple[Fact, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_properties(self):
        if self.id.startswith("cell:"):
            raise ValueError("cell: is a reserved memory namespace")
        keys = [f.attribute for f in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("use separate effect/ability entities, not duplicate fact keys")
        return self


class Relation(EyeModel):
    subject: Ref
    predicate: Literal["carries", "equipped_in", "part_of", "contains", "provides",
                       "affects", "member_of", "requires", "leads_to"]
    object: Ref
    evidence: Evidence

    @model_validator(mode="after")
    def known_or_omitted(self):
        if self.evidence.status == "unknown":
            raise ValueError("unknown relations must be omitted")
        return self


class Layer(EyeModel):
    id: Ref
    fact: Fact


class Cell(EyeModel):
    x: Annotated[int, Field(ge=0, strict=True)]
    y: Annotated[int, Field(ge=0, strict=True)]
    # Multiple layers can coexist; omitted cells/layers mean unknown, never empty.
    layers: tuple[Layer, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.layers:
            raise ValueError("omit undisclosed cells instead of emitting empty layers")
        if len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("duplicate layer IDs")
        return self


class Zone(EyeModel):
    id: Ref
    width: Annotated[int, Field(gt=0, le=1024, strict=True)]
    height: Annotated[int, Field(gt=0, le=1024, strict=True)]
    cells: tuple[Cell, ...] = Field(default=(), max_length=MAX_CELLS)

    @model_validator(mode="after")
    def valid_cells(self):
        seen = set()
        for cell in self.cells:
            if cell.x >= self.width or cell.y >= self.height:
                raise ValueError("cell outside zone")
            key = (cell.x, cell.y)
            if key in seen:
                raise ValueError("duplicate cell")
            seen.add(key)
        return self


class Event(EyeModel):
    id: Ref
    kind: Literal["message", "inspection", "dialogue", "journal", "resource_change",
                  "effect_change", "perception_change", "prompt"]
    text: Text
    evidence: Evidence
    # Only references whose identity the permitted event actually establishes.
    subjects: tuple[Ref, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def known_or_omitted(self):
        if self.evidence.status == "unknown":
            raise ValueError("unknown events must be omitted")
        return self


class Destination(EyeModel):
    zone: Ref
    x: Annotated[int, Field(ge=0, strict=True)]
    y: Annotated[int, Field(ge=0, strict=True)]


class Action(EyeModel):
    id: Ref
    operation: Literal["move", "wait", "answer", "interact", "use", "inventory",
                       "inspect", "attack", "fire", "equip", "travel", "progress"]
    label: Text
    source: Ref | None = None
    target: Ref | None = None
    destination: Destination | None = None
    mode: Annotated[str, Field(max_length=160)] | None = None
    quantity: Annotated[int, Field(gt=0, strict=True)] | None = None
    # Known cost, not an omniscient outcome prediction. null means not established.
    known_turn_cost: Tick | None = None


class Prompt(EyeModel):
    kind: Literal["choice", "direction", "target", "text"]
    text: Text


class Frame(EyeModel):
    schema_version: Literal["agent-eye/1"] = "agent-eye/1"
    episode_id: Ref
    # A branch is an explicit active lineage. Restoring into a new branch
    # requires rebuilding memory; a bounded decision cache is not lineage.
    branch_id: Ref
    decision_id: Ref
    parent_decision_id: Ref | None = None
    turn: Tick
    phase: Literal["creation", "command", "prompt", "terminal"]
    controlled_actor: Ref | None
    zones: tuple[Zone, ...] = Field(default=(), max_length=MAX_ZONES)
    entities: tuple[Entity, ...] = Field(default=(), max_length=MAX_ENTITIES)
    relations: tuple[Relation, ...] = Field(default=(), max_length=MAX_RELATIONS)
    events: tuple[Event, ...] = Field(default=(), max_length=MAX_EVENTS)
    prompt: Prompt | None = None
    actions: tuple[Action, ...] = Field(max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def consistent(self):
        if self.parent_decision_id == self.decision_id:
            raise ValueError("a decision cannot be its own parent")
        ids = [e.id for e in self.entities]
        refs = set(ids)
        if len(ids) != len(refs):
            raise ValueError("duplicate entity handles")
        if len({z.id for z in self.zones}) != len(self.zones):
            raise ValueError("duplicate zones")
        if len({a.id for a in self.actions}) != len(self.actions):
            raise ValueError("duplicate actions")
        if len({e.id for e in self.events}) != len(self.events):
            raise ValueError("duplicate event handles")
        relation_keys = [(r.subject, r.predicate, r.object) for r in self.relations]
        if len(set(relation_keys)) != len(relation_keys):
            raise ValueError("duplicate relation triples")
        if self.controlled_actor is not None:
            actor = next((e for e in self.entities if e.id == self.controlled_actor), None)
            if actor is None or actor.kind != "actor":
                raise ValueError("controlled_actor must reference a supplied actor")
        elif self.phase not in ("creation", "terminal"):
            raise ValueError("in-world decision requires a controlled actor")
        if (self.prompt is not None) != (self.phase == "prompt"):
            raise ValueError("prompt and phase disagree")
        if bool(self.actions) == (self.phase == "terminal"):
            raise ValueError("terminal must have no actions; other phases require actions")
        zones = {z.id: z for z in self.zones}
        evidence = []
        for entity in self.entities:
            evidence.extend(f.evidence for f in entity.facts)
            if entity.location:
                p = entity.location
                if p.zone not in zones or p.x >= zones[p.zone].width or p.y >= zones[p.zone].height:
                    raise ValueError("entity location must be inside a supplied zone")
                evidence.append(p.evidence)
        for z in self.zones:
            evidence.extend(layer.fact.evidence for c in z.cells for layer in c.layers)
        for r in self.relations:
            if r.subject not in refs or r.object not in refs:
                raise ValueError("dangling relation reference")
            evidence.append(r.evidence)
        for e in self.events:
            if not set(e.subjects) <= refs:
                raise ValueError("dangling event reference")
            evidence.append(e.evidence)
        for a in self.actions:
            if any(ref is not None and ref not in refs for ref in (a.source, a.target)):
                raise ValueError("dangling action reference")
            if a.destination:
                p = a.destination
                if p.zone not in zones or p.x >= zones[p.zone].width or p.y >= zones[p.zone].height:
                    raise ValueError("action destination outside supplied zone")
        if any(e.turn > self.turn for e in evidence):
            raise ValueError("evidence cannot come from a future turn")
        # 'observed' in a Frame means current observation, not a stale engine cache.
        if any(e.status == "observed" and e.turn != self.turn for e in evidence):
            raise ValueError("past observations belong in memory, not current perception")
        if len(self.model_dump_json()) > MAX_FRAME_BYTES:
            raise ValueError("frame exceeds the aggregate byte budget")
        return self


class MemoryRecord(EyeModel):
    subject: MemorySubject
    attribute: str
    fact: Fact | Position
    last_decision: Ref
    last_turn: Tick


class EventMemory(EyeModel):
    event: Event
    decision_id: Ref


class Hypothesis(EyeModel):
    """Optional model output, deliberately NOT a Fact. Scores are not calibrated probabilities."""
    id: Ref
    text: Text
    based_on_decisions: tuple[Ref, ...] = Field(min_length=1, max_length=MAX_HYPOTHESES)
    model: Ref
    score: float | None = None


class AgentView(EyeModel):
    schema_version: Literal["agent-view/1"] = "agent-view/1"
    current: Frame
    remembered: tuple[MemoryRecord, ...] = Field(default=(), max_length=MAX_VIEW_RECORDS)
    hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=MAX_HYPOTHESES)
    remembered_events: tuple[EventMemory, ...] = Field(default=(), max_length=MAX_VIEW_EVENTS)
    forgotten_events: Tick = 0
    forgotten_records: Tick = 0

    @model_validator(mode="after")
    def no_future_memory(self):
        if any(m.event.evidence.turn > self.current.turn for m in self.remembered_events):
            raise ValueError("event memory from a future branch is forbidden")
        if any(m.last_turn != m.fact.evidence.turn for m in self.remembered):
            raise ValueError("memory provenance turn does not match its evidence")
        if any(m.last_turn > self.current.turn for m in self.remembered):
            raise ValueError("memory from a future branch is forbidden")
        return self
