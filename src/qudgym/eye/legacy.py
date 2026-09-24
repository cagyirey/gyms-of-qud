"""One-way adapter from foundation RPC observations, NOT from Qud internals.

The legacy observation is too sparse to supply real anatomy, resources, sensory
contacts or equipment. Missing information stays missing; do not synthesize it.
"""
from ..models import Observation
from .contracts import Action, Cell, Destination, Entity, Event, Evidence, Fact, Frame, Layer, Position, Prompt, Zone


def from_observation(observation: Observation) -> Frame:
    o = observation
    self_ev = Evidence(channel="self", turn=o.turn)
    vision = Evidence(channel="vision", turn=o.turn)
    # Zone identity is not supplied by the old contract. Do not claim persistence
    # across live zones. This adapter is explicitly for the corridor mock.
    zone_id = "legacy-mock-zone"
    player_id = "legacy-player"
    if any(e.id == player_id for e in o.entities):
        raise ValueError("legacy entity collides with player handle")
    cells = tuple(Cell(x=x, y=y, layers=(Layer(id="glyph", fact=Fact(
        attribute="glyph", value=c, evidence=vision)),))
        for y, row in enumerate(o.tiles) for x, c in enumerate(row) if c != "?")
    if not o.tiles:
        raise ValueError("legacy adapter needs a known map extent")
    zones = (Zone(id=zone_id, width=len(o.tiles[0]), height=len(o.tiles), cells=cells),)
    entities = [Entity(id=player_id, kind="actor", location=Position(
        zone=zone_id, x=o.player.x, y=o.player.y, evidence=self_ev), facts=(
            Fact(attribute="hp", value=o.player.hp, evidence=self_ev),
            Fact(attribute="max_hp", value=o.player.max_hp, evidence=self_ev)))]
    for e in o.entities:
        facts = [Fact(attribute="name", value=e.name, evidence=vision)]
        if e.perceived_status is not None:
            facts.append(Fact(attribute="effect", value=e.perceived_status, evidence=vision))
        entities.append(Entity(id=e.id, kind="contact", location=Position(
            zone=zone_id, x=e.x, y=e.y, evidence=vision), facts=tuple(facts)))
    operations = {"ability": "use"}
    actions = []
    for a in o.actions:
        destination = None
        if a.arguments:
            if a.kind != "move" or set(a.arguments) != {"dx", "dy"}:
                raise ValueError("legacy action metadata requires an explicit, lossless mapping")
            dx, dy = a.arguments["dx"], a.arguments["dy"]
            if type(dx) is not int or type(dy) is not int:
                raise ValueError("legacy move offsets must be integers")
            destination = Destination(zone=zone_id, x=o.player.x + dx, y=o.player.y + dy)
        actions.append(Action(id=a.id, operation=operations.get(a.kind, a.kind), label=a.label,
                              source=player_id, destination=destination))
    events = tuple(Event(id=f"message:{i}", kind="message", text=m,
                         evidence=Evidence(channel="message", turn=o.turn))
                   for i, m in enumerate(o.messages))
    return Frame(episode_id=o.episode_id, decision_id=o.decision_id, turn=o.turn,
                 phase=o.phase, controlled_actor=player_id, zones=zones, entities=tuple(entities),
                 events=events, actions=tuple(actions),
                 prompt=Prompt(kind=o.prompt.kind, text=o.prompt.text) if o.prompt else None)
