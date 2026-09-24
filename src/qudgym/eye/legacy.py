"""One-way adapter from foundation RPC observations, NOT from Qud internals.

The legacy observation is too sparse to supply real anatomy, resources, sensory
contacts or equipment. Missing information stays missing; do not synthesize it.
"""
from pydantic import BaseModel, ConfigDict, ValidationError

from ..models import Observation
from .contracts import Action, Cell, Destination, Entity, Event, Evidence, Fact, Frame, Layer, Position, Prompt, Zone


_TEXT = 8192
_ZONE = 1024


def _clip(value: str) -> str:
    return value if len(value) <= _TEXT else value[:_TEXT]


class MoveOffset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dx: int
    dy: int


def _inside(x: int, y: int, width: int, height: int) -> bool:
    return 0 <= x < width and 0 <= y < height


def from_observation(observation: Observation) -> Frame:
    o = observation
    self_ev = Evidence(channel="self", turn=o.turn)
    vision = Evidence(channel="vision", turn=o.turn)
    # Zone identity is not supplied by the old contract. Do not claim persistence
    # across live zones. This adapter is explicitly for the corridor mock.
    # Cells past the agent-eye zone cap, and positions outside that rectangle,
    # are omitted rather than invented or rejected.
    zone_id = "legacy-mock-zone"
    player_id = "legacy-player"
    rows = tuple(row[:_ZONE] for row in o.tiles[:_ZONE])
    width = min(_ZONE, max((len(row) for row in rows), default=0))
    height = len(rows)
    if width < 1 or height < 1:
        width, height = 1, 1
        rows = ()
    cells = tuple(Cell(x=x, y=y, layers=(Layer(id="glyph", fact=Fact(
        attribute="glyph", value=c, evidence=vision)),))
        for y, row in enumerate(rows) for x, c in enumerate(row[:width]) if c != "?")
    zones = (Zone(id=zone_id, width=width, height=height, cells=cells),)

    def place(x: int, y: int, evidence: Evidence) -> Position | None:
        if not _inside(x, y, width, height):
            return None
        return Position(zone=zone_id, x=x, y=y, evidence=evidence)

    entities = [Entity(id=player_id, kind="actor", location=place(o.player.x, o.player.y, self_ev), facts=(
            Fact(attribute="hp", value=o.player.hp, evidence=self_ev),
            Fact(attribute="max_hp", value=o.player.max_hp, evidence=self_ev)))]
    seen = {player_id}
    for e in o.entities:
        if e.id in seen or e.id.startswith("cell:"):
            continue
        seen.add(e.id)
        facts = [Fact(attribute="name", value=e.name, evidence=vision)]
        if e.perceived_status is not None:
            facts.append(Fact(attribute="effect", value=e.perceived_status, evidence=vision))
        entities.append(Entity(id=e.id, kind="contact", location=place(e.x, e.y, vision), facts=tuple(facts)))
    operations = {"ability": "use"}
    actions = []
    for a in o.actions:
        destination = None
        if a.arguments:
            if a.kind != "move":
                raise ValueError("legacy action metadata requires an explicit, lossless mapping")
            try:
                offset = MoveOffset.model_validate(a.arguments)
            except ValidationError as exc:
                raise ValueError("legacy action metadata requires an explicit, lossless mapping") from exc
            placed = place(o.player.x + offset.dx, o.player.y + offset.dy, self_ev)
            if placed is not None:
                destination = Destination(zone=zone_id, x=placed.x, y=placed.y)
        actions.append(Action(id=a.id, operation=operations.get(a.kind, a.kind), label=_clip(a.label),
                              source=player_id, destination=destination))
    events = tuple(Event(id=f"message:{i}", kind="message", text=_clip(m),
                         evidence=Evidence(channel="message", turn=o.turn))
                   for i, m in enumerate(o.messages))
    return Frame(episode_id=o.episode_id, decision_id=o.decision_id, turn=o.turn,
                 phase=o.phase, controlled_actor=player_id, zones=zones, entities=tuple(entities),
                 events=events, actions=tuple(actions),
                 prompt=Prompt(kind=o.prompt.kind, text=_clip(o.prompt.text)) if o.prompt else None)
