"""Synthetic capability arena. NOT Qud rules, builds, rewards, or evaluations.

Used to test perception/memory/action binding on CPU. One stationary practice
object, a three-cell visual radius, optional six-cell hearing, and zero-turn
inspection prompts. There are no game assets or external model calls.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .contracts import (Action, AgentView, Cell, Destination, Entity, Event, Evidence,
                        Fact, Frame, Layer, Position, Prompt, Relation, Zone)

PRESETS = ("blade", "bow", "listener")


def fact(attribute, value, turn, *, channel="self", status="observed", unit=None):
    return Fact(attribute=attribute, value=value, unit=unit,
                evidence=Evidence(channel=channel, turn=turn, status=status))


@dataclass
class ArenaFixture:
    preset: str = "bow"
    max_decisions: int = 32

    def __post_init__(self):
        if self.preset not in PRESETS:
            raise ValueError(f"synthetic preset must be one of {PRESETS}")
        if type(self.max_decisions) is not int or self.max_decisions < 1:
            raise ValueError("max_decisions must be positive")
        self.reset()

    def reset(self) -> Frame:
        self.episode = secrets.token_hex(12)
        self.sequence = self.turn = self.cooldown = 0
        self.x, self.y = 3, 2
        self.ammo = 3 if self.preset == "bow" else 0
        self._target_x, self._target_y, self._target_hp = 5, 2, 2
        self._hidden_artifact_name = "synthetic copper keepsake"
        self._unobserved_secret = "not-for-policy"
        self._contact_number = 0
        self._contact = None
        self._pending = self._identified = self._terminal = False
        self.outcome = "ongoing"
        self.message = "Synthetic arena: disable the practice target."
        self._refresh_contact()
        return self.observe()

    def _channel(self):
        if self._target_hp <= 0:
            return None
        d = abs(self._target_x - self.x) + abs(self._target_y - self.y)
        if d <= 3:
            return "vision"
        if self.preset == "listener" and d <= 6:
            return "hearing"
        return None

    def _refresh_contact(self):
        # Opaque handles track continuous perceived contact only. No hidden re-ID.
        if self._channel() is None:
            self._contact = None
        elif self._contact is None:
            self._contact_number += 1
            self._contact = f"c{self._contact_number}"

    def observe(self) -> Frame:
        t = self.turn
        self_ev = Evidence(channel="self", turn=t)
        vision = Evidence(channel="vision", turn=t)
        pos = Position(zone="arena", x=self.x, y=self.y, evidence=self_ev)
        entities = [
            Entity(id="p", kind="actor", location=pos, facts=(fact("hp", 4, t), fact("max_hp", 4, t))),
            Entity(id="hand", kind="body_part", facts=(fact("anatomy", "hand", t),)),
            Entity(id="weapon", kind="item", facts=(fact("name", "mock bow" if self.preset == "bow" else "mock blade", t),)),
            Entity(id="ability", kind="ability", facts=(
                fact("name", "fire" if self.preset == "bow" else "strike", t),
                fact("range", 4 if self.preset == "bow" else 1, t, unit="cells"),
                fact("cost", 1 if self.preset == "bow" else 0, t, unit="ammo"),
                fact("cooldown", self.cooldown, t, unit="turns"),
                fact("ready", self.cooldown == 0 and (self.preset != "bow" or self.ammo > 0), t))),
            Entity(id="ammo", kind="item", facts=(fact("resource", "ammo", t), fact("amount", self.ammo, t))),
            Entity(id="artifact", kind="item", facts=(
                fact("name", "unidentified trinket", t),
                fact("identity", self._hidden_artifact_name if self._identified else None, t,
                     channel="inspection" if self._identified else "unknown",
                     status="observed" if self._identified else "unknown"))),
        ]
        relations = [Relation(subject="p", predicate="carries", object="weapon", evidence=self_ev),
                     Relation(subject="p", predicate="carries", object="artifact", evidence=self_ev),
                     Relation(subject="p", predicate="carries", object="ammo", evidence=self_ev),
                     Relation(subject="hand", predicate="part_of", object="p", evidence=self_ev),
                     Relation(subject="weapon", predicate="equipped_in", object="hand", evidence=self_ev),
                     Relation(subject="weapon", predicate="provides", object="ability", evidence=self_ev)]
        if self.preset == "listener":
            entities.append(Entity(id="sense", kind="ability", facts=(fact("sensing", "synthetic hearing", t),)))
            relations.append(Relation(subject="p", predicate="provides", object="sense", evidence=self_ev))
        channel = self._channel()
        if channel and self._contact:
            contact_ev = Evidence(channel=channel, turn=t)
            entities.append(Entity(id=self._contact, kind="contact", location=Position(
                zone="arena", x=self._target_x, y=self._target_y, evidence=contact_ev), facts=(
                    fact("identity", "practice target" if channel == "vision" else None, t,
                         channel="vision" if channel == "vision" else "unknown",
                         status="observed" if channel == "vision" else "unknown"),)))
        cells = tuple(Cell(x=x, y=y, layers=(Layer(id="ground", fact=fact(
            "terrain", "#" if x in (0, 8) or y in (0, 4) else ".", t, channel="vision")),))
            for y in range(5) for x in range(9) if abs(x-self.x) + abs(y-self.y) <= 3)
        actions = []
        if self._pending and not self._terminal:
            actions = [Action(id="yes", operation="answer", label="Inspect", known_turn_cost=0),
                       Action(id="no", operation="answer", label="Cancel", known_turn_cost=0)]
        elif not self._terminal:
            if self.x < 7:
                actions.append(Action(id="forward", operation="move", label="Step east", source="p",
                                      destination=Destination(zone="arena", x=self.x+1, y=self.y), known_turn_cost=1))
            if self.x > 1:
                actions.append(Action(id="back", operation="move", label="Step west", source="p",
                                      destination=Destination(zone="arena", x=self.x-1, y=self.y), known_turn_cost=1))
            actions += [Action(id="rest", operation="wait", label="Wait", known_turn_cost=1),
                        Action(id="look", operation="inspect", label="Inspect trinket", target="artifact", known_turn_cost=0)]
            if self._contact and channel:
                actions.append(Action(id="use", operation="fire" if self.preset == "bow" else "attack",
                                      source="ability", target=self._contact, label="Attempt attack", known_turn_cost=1))
        return Frame(episode_id=self.episode, decision_id=f"d{self.sequence}", turn=t,
                     phase="terminal" if self._terminal else "prompt" if self._pending else "command",
                     controlled_actor="p", zones=(Zone(id="arena", width=9, height=5, cells=cells),),
                     entities=tuple(entities), relations=tuple(relations), actions=tuple(actions),
                     events=(Event(id=f"e{self.sequence}", kind="message", text=self.message,
                                   evidence=Evidence(channel="message", turn=t)),),
                     prompt=Prompt(kind="choice", text="Inspect the trinket?") if self._pending and not self._terminal else None)

    def step(self, action_id: str, *, decision_id: str) -> Frame:
        before = self.observe()
        if decision_id != before.decision_id:
            raise ValueError("stale decision")
        candidate = next((a for a in before.actions if a.id == action_id), None)
        if candidate is None:
            raise ValueError("invalid action or finished fixture")
        elapsed = candidate.known_turn_cost or 0
        # Determine feasibility using the BEFORE-decision state, never a post-tick cooldown.
        distance = abs(self._target_x-self.x) + abs(self._target_y-self.y)
        can_hit = (self.cooldown == 0 and distance <= (4 if self.preset == "bow" else 1)
                   and (self.preset != "bow" or self.ammo > 0))
        self.turn += elapsed
        self.cooldown = max(0, self.cooldown-elapsed)
        self.message = "No new event."
        if action_id in ("forward", "back"):
            self.x += 1 if action_id == "forward" else -1
        elif action_id == "look":
            self._pending = True
            self.message = "Inspection prompt opened; no game time elapsed."
        elif action_id in ("yes", "no"):
            self._pending = False
            if action_id == "yes":
                self._identified = True
                self.message = "Inspection reveals " + self._hidden_artifact_name + "."
        elif action_id == "use":
            if can_hit:
                self._target_hp -= 1
                if self.preset == "bow":
                    self.ammo -= 1
                self.cooldown = 2
                self.message = "The practice target is struck."
            else:
                self.message = "The attempted attack has no effect."
        self.sequence += 1
        if self._target_hp <= 0:
            self.outcome, self._terminal = "success", True
        elif self.sequence >= self.max_decisions:
            self.outcome, self._terminal = "decision_limit", True
        self._refresh_contact()
        return self.observe()


def _observed_in(entity, zone: str) -> bool:
    loc = getattr(entity, "location", None)
    return loc is not None and loc.evidence.status == "observed" and loc.zone == zone


class CapabilityScorer:
    """Diagnostic rule baseline, NOT learned intelligence or Qud strategy.

Scores only current observed capabilities/positions, ignores preset names and
opaque IDs. Object references bind actions to the same entity table as perception.
"""
    def score(self, view: AgentView) -> dict[str, float]:
        f = view.current
        entities = {e.id: e for e in f.entities}
        actor = entities.get(f.controlled_actor)
        result = {}
        for a in f.actions:
            score = -10.0
            if a.operation == "wait":
                score = 0.0
            zone = actor.location.zone if actor and actor.location and actor.location.evidence.status == "observed" else None
            if a.operation in ("attack", "fire") and zone is not None:
                source, target = entities.get(a.source), entities.get(a.target)
                if source and _observed_in(target, zone):
                    values = {p.attribute: p.value for p in source.facts if p.evidence.status == "observed"}
                    distance = abs(actor.location.x-target.location.x) + abs(actor.location.y-target.location.y)
                    reach = values.get("range")
                    if values.get("ready") is True and type(reach) in (int, float) and distance <= reach:
                        score = 100.0
            if a.operation == "move" and zone is not None and a.destination and a.destination.zone == zone:
                targets = [e for e in f.entities if e.kind == "contact" and _observed_in(e, zone)]
                if targets:
                    old = min(abs(e.location.x-actor.location.x)+abs(e.location.y-actor.location.y) for e in targets)
                    new = min(abs(e.location.x-a.destination.x)+abs(e.location.y-a.destination.y) for e in targets)
                    score = float(old-new)
            if a.operation == "answer":
                score = 1.0
            result[a.id] = score
        return result
