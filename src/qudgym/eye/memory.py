"""Bounded, deterministic evidence memory. Receives ONLY sanitized Frames."""
from collections import OrderedDict

from .contracts import (
    MAX_VIEW_EVENTS,
    MAX_VIEW_RECORDS,
    AgentView,
    EventMemory,
    Fact,
    Frame,
    Hypothesis,
    MemoryRecord,
    Position,
)

ClaimKey = tuple[str, str, str, str]


def disclosed_records(frame: Frame) -> list[tuple[str, str, Fact | Position]]:
    """Do not infer absence from an omitted entity, property, cell, or relation."""
    records = []
    for entity in frame.entities:
        if entity.location is not None:
            records.append((entity.id, "location", entity.location))
        records.extend((entity.id, f.attribute, f) for f in entity.facts)
    for zone in frame.zones:
        for cell in zone.cells:
            for layer in cell.layers:
                records.append((f"cell:{zone.id}:{cell.x}:{cell.y}", f"layer:{layer.id}", layer.fact))
    for relation in frame.relations:
        # Relation contracts reject unknown evidence; keep this guard for
        # callers that construct records from older serialized frames.
        if relation.evidence.status == "unknown":
            continue
        records.append((relation.subject, f"relation:{relation.predicate}:{relation.object}",
                        Fact(attribute="description", value=f"{relation.predicate} {relation.object}",
                             evidence=relation.evidence)))
    return records


def _claim_key(subject: str, attribute: str, fact: Fact | Position) -> ClaimKey:
    return (subject, attribute, fact.evidence.status, fact.evidence.channel)


class EvidenceMemory:
    def __init__(self, *, max_records: int = 4096, max_decisions: int = 256, max_events: int = 256):
        if any(type(x) is not int or x < 1 for x in (max_records, max_decisions, max_events)):
            raise ValueError("memory limits must be positive integers")
        if max_records > MAX_VIEW_RECORDS or max_events > MAX_VIEW_EVENTS:
            raise ValueError("memory limits exceed the agent-view budget")
        self.max_records, self.max_decisions = max_records, max_decisions
        self.max_events = max_events
        self.reset()

    def reset(self) -> None:
        self._records: OrderedDict[ClaimKey, MemoryRecord] = OrderedDict()
        self._decisions: OrderedDict[str, None] = OrderedDict()
        self._frame: Frame | None = None
        self._forgotten = 0
        self._events: OrderedDict[tuple[str, str], EventMemory] = OrderedDict()
        self._forgotten_events = 0

    def update(self, frame: Frame, *, hypotheses: tuple[Hypothesis, ...] = ()) -> AgentView:
        if self._frame is not None:
            if frame.episode_id != self._frame.episode_id:
                raise ValueError("reset memory explicitly between episodes")
            if frame.turn < self._frame.turn:
                raise ValueError("rewind requires rebuilding memory from the restored history")
            if frame.branch_id != self._frame.branch_id:
                raise ValueError("branch changes require an explicit memory rebuild")
            if frame.decision_id in self._decisions:
                if frame != self._frame:
                    raise ValueError("reused decision ID or out-of-order frame")
                return self._view(frame, hypotheses)
            if frame.parent_decision_id != self._frame.decision_id:
                raise ValueError("decision parent does not match the active branch")
        elif frame.parent_decision_id is not None:
            raise ValueError("the first frame must not reference a parent decision")

        # Validate before mutating, including on a failed hypothesis submission.
        available: set[str] = set(
            (list(self._decisions) + [frame.decision_id])[-self.max_decisions:]
        )
        for hypothesis in hypotheses:
            if not set(hypothesis.based_on_decisions) <= available:
                raise ValueError("hypothesis references unknown decision evidence")

        fresh: list[tuple[ClaimKey, MemoryRecord]] = []
        for subject, attribute, fact in sorted(
                disclosed_records(frame),
                key=lambda record: (record[0], record[1], record[2].evidence.status,
                                     record[2].evidence.channel)):
            # Unknown now must not erase an earlier observation. It stays remembered.
            if fact.evidence.status == "unknown":
                continue
            key = _claim_key(subject, attribute, fact)
            fresh.append((key, MemoryRecord(
                subject=subject, attribute=attribute, fact=fact,
                last_decision=frame.decision_id, last_turn=fact.evidence.turn)))
        for key, record in fresh:
            self._records[key] = record
            self._records.move_to_end(key)
        while len(self._records) > self.max_records:
            self._records.popitem(last=False)
            self._forgotten += 1

        for event in frame.events:
            self._events[(frame.decision_id, event.id)] = EventMemory(
                event=event, decision_id=frame.decision_id)
        while len(self._events) > self.max_events:
            self._events.popitem(last=False)
            self._forgotten_events += 1

        self._decisions[frame.decision_id] = None
        while len(self._decisions) > self.max_decisions:
            self._decisions.popitem(last=False)
        self._frame = frame
        return self._view(frame, hypotheses)

    def _view(self, frame: Frame, hypotheses: tuple[Hypothesis, ...]) -> AgentView:
        for hypothesis in hypotheses:
            if not set(hypothesis.based_on_decisions) <= set(self._decisions):
                raise ValueError("hypothesis references unknown decision evidence")
        now = {
            _claim_key(subject, attribute, fact)
            for subject, attribute, fact in disclosed_records(frame)
            if fact.evidence.status != "unknown"
        }
        return AgentView(
            current=frame,
            remembered=tuple(record for key, record in sorted(self._records.items()) if key not in now),
            hypotheses=hypotheses,
            forgotten_records=self._forgotten,
            remembered_events=tuple(
                event for (decision, _), event in self._events.items()
                if decision != frame.decision_id),
            forgotten_events=self._forgotten_events,
        )
