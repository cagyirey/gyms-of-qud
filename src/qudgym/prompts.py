"""Operating instructions for the model that chooses actions.

The instructions and the validator are generated from the same ActionSpace, so
they cannot drift apart: a term appears in the prompt only because it is legal
at this boundary. That is the whole point -- a hand-written prompt enumerates
actions that may not exist, and a model that follows it confidently is worse
than one that was never told.

Kept short on purpose. The observation already carries state, so the prompt
carries only the decision: what kind of boundary this is, what the action
affects, and the exact reply format. Long instructions did not make the model
more correct; they made it more likely to explain itself instead of answering.
"""
from __future__ import annotations

from .guidance import ActionSpace
from .models import Observation

SYSTEM = """\
You choose one action per turn in a turn-based roguelike.

Reply with exactly one action id from the legal list, and nothing else. \
No explanation, no punctuation, no code fence. If you are unsure, reply with \
the wait action.\
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

    by_kind: dict[str, list[str]] = {}
    for action in observation.actions:
        by_kind.setdefault(action.kind, []).append(action.id)
    rendered = "; ".join(f"{kind}: {', '.join(ids)}" for kind, ids in by_kind.items())
    lines.append(f"Legal actions -> {rendered}")

    return "\n".join(lines)


def build_messages(observation: Observation, space: ActionSpace) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": build_prompt(observation, space)},
    ]
