"""Bounded, deterministic evidence memory. Receives ONLY sanitized Frames."""
from collections import OrderedDict
from .contracts import AgentView, EventMemory, Fact, Frame, Hypothesis, MemoryRecord


def disclosed_records(frame: Frame):
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
    for r in frame.relations:
        records.append((r.subject, f"relation:{r.predicate}:{r.object}",
                        Fact(attribute="description", value=f"{r.predicate} {r.object}",
                             evidence=r.evidence)))
    return records


class EvidenceMemory:
    def __init__(self, *, max_records: int = 4096, max_decisions: int = 256, max_events: int = 256):
        if any(type(x) is not int or x < 1 for x in (max_records, max_decisions, max_events)):
            raise ValueError("memory limits must be positive integers")
        self.max_records, self.max_decisions = max_records, max_decisions
        self.max_events = max_events
        self.reset()

    def reset(self):
        self._records: OrderedDict[tuple[str, str], MemoryRecord] = OrderedDict()
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
            if frame.decision_id in self._decisions:
                if frame != self._frame:
                    raise ValueError("reused decision ID or out-of-order frame")
                return self._view(frame, hypotheses)
        # Validate before mutating, including on a failed hypothesis submission.
        available = set((list(self._decisions) + [frame.decision_id])[-self.max_decisions:])
        for h in hypotheses:
            if not set(h.based_on_decisions) <= available:
                raise ValueError("hypothesis references unknown decision evidence")
        for subject, attribute, fact in sorted(disclosed_records(frame), key=lambda r: (r[0], r[1])):
            key = (subject, attribute)
            # Unknown now must not erase an earlier observation. It stays remembered.
            if fact.evidence.status == "unknown":
                continue
            self._records[key] = MemoryRecord(subject=subject, attribute=attribute, fact=fact,
                                               last_decision=frame.decision_id,
                                               last_turn=fact.evidence.turn)
            self._records.move_to_end(key)
        while len(self._records) > self.max_records:
            self._records.popitem(last=False)
            self._forgotten += 1
        for e in frame.events:
            self._events[(frame.decision_id, e.id)] = EventMemory(event=e, decision_id=frame.decision_id)
        while len(self._events) > self.max_events:
            self._events.popitem(last=False)
            self._forgotten_events += 1
        self._decisions[frame.decision_id] = None
        while len(self._decisions) > self.max_decisions:
            self._decisions.popitem(last=False)
        self._frame = frame
        return self._view(frame, hypotheses)

    def _view(self, frame, hypotheses):
        for h in hypotheses:
            if not set(h.based_on_decisions) <= set(self._decisions):
                raise ValueError("hypothesis references unknown decision evidence")
        now = {(s, a) for s, a, f in disclosed_records(frame) if f.evidence.status != "unknown"}
        return AgentView(current=frame,
                         remembered=tuple(r for k, r in sorted(self._records.items()) if k not in now),
                         hypotheses=hypotheses, forgotten_records=self._forgotten,
                         remembered_events=tuple(e for (d, _), e in self._events.items() if d != frame.decision_id),
                         forgotten_events=self._forgotten_events)
