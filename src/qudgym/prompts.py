"""Operating instructions for the model that chooses actions.

The instructions and the validator are generated from the same ActionSpace, so
they cannot drift apart: a term appears in the prompt only because it is legal
at this boundary. That is the whole point -- a hand-written prompt enumerates
actions that may not exist, and a model that follows it confidently is worse
than one that was never told.

The action list carries each action's description and arguments, not just its
id. An id is an opaque handle; a model shown bare ids is being asked to guess
what they do, and a list of ids is not an interface.
"""
from __future__ import annotations

import json

from .guidance import ActionSpace
from .models import CandidateAction, Observation

SYSTEM = """\
You are playing Caves of Qud, a turn-based roguelike, through a harness.

Each turn you receive the current game state and the complete list of actions
available to you. You choose one, the game executes it, and you see the
resulting state.

The action_id values are opaque handles. Choose by what an action does, using
its description and arguments, not by how its id happens to read.

Reply with exactly one action_id, copied from the list, and nothing else. No
explanation, no punctuation, no code fence, no restating of the state. If no
action is clearly better than waiting, reply with the wait action.
"""


def build_prompt(observation: Observation, space: ActionSpace) -> str:
    """Render the per-turn user message."""
    lines: list[str] = []

    phase = observation.phase
    if phase == "prompt":
        lines.append("A prompt is open. Only answering is legal this turn.")
    elif phase == "terminal":
        lines.append("The episode is over. There is no action to take.")
    else:
        lines.append(f"Turn {observation.turn}. Choose your next action.")

    p = observation.player
    lines.append(f"You have {p.hp}/{p.max_hp} health at ({p.x},{p.y}).")

    if observation.entities:
        # Nearest first: the ordering carries the useful signal, and the model
        # should not have to compute distances.
        here = [(abs(e.x - p.x) + abs(e.y - p.y), e) for e in observation.entities]
        here.sort(key=lambda pair: pair[0])
        near = ", ".join(
            f"{e.name} ({(e.x - p.x):+d},{(e.y - p.y):+d})" for _, e in here[:6]
        )
        lines.append(f"Nearby: {near}.")

    if observation.messages:
        recent = " / ".join(m.strip() for m in observation.messages[-2:] if m.strip())
        if recent:
            lines.append(f"Most recent: {recent}")

    if observation.prompt:
        lines.append(f'The prompt asks: "{observation.prompt.text}"')

    by_kind: dict[str, list[CandidateAction]] = {}
    for action in observation.actions:
        by_kind.setdefault(action.kind, []).append(action)

    lines.append("You may take exactly one of these actions this turn:")
    for kind, actions in by_kind.items():
        lines.append(f"  {kind}:")
        for action in actions:
            detail = f"action_id={action.id}"
            if action.label:
                detail += f"  ({action.label})"
            if action.arguments:
                detail += "  args=" + json.dumps(action.arguments, sort_keys=True)
            lines.append(f"    {detail}")

    return "\n".join(lines)


def build_messages(observation: Observation, space: ActionSpace) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": build_prompt(observation, space)},
    ]
