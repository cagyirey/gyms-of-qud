"""The prompt must never mention an action the validator would reject.

If the instructions and the grammar disagree, the model is being asked to
choose between two authorities. These tests hold the instructions to the
ActionSpace, which is the same object the guard enforces at the boundary.
"""
from __future__ import annotations

import pytest

from qudgym.guidance import build_action_space
from qudgym.models import (
    CandidateAction,
    Observation,
    PerceivedEntity,
    Player,
    Prompt,
)
from qudgym.prompts import SYSTEM, build_messages, build_prompt


def obs(*actions: CandidateAction, phase: str = "command", prompt: Prompt | None = None,
        messages: tuple[str, ...] = ()) -> Observation:
    return Observation(
        episode_id="ep",
        decision_id="ep:0",
        turn=7,
        phase=phase,
        player=Player(x=37, y=22, hp=11, max_hp=17),
        tiles=("...",),
        entities=(
            PerceivedEntity(id="e1", name="Mehmet", x=40, y=21),
            PerceivedEntity(id="e2", name="watervine", x=36, y=24),
        ),
        messages=messages,
        prompt=prompt,
        actions=actions or (CandidateAction(id="move:E", kind="move", label="Move E"),),
    )


def test_prompt_lists_exactly_the_legal_actions():
    o = obs(
        CandidateAction(id="move:E", kind="move", label="Move E"),
        CandidateAction(id="talk:Mehmet", kind="interact", label="talk Mehmet"),
        CandidateAction(id="wait", kind="wait", label="Wait one turn"),
    )
    space = build_action_space(o)
    text = build_prompt(o, space)
    for action in space.action_ids:
        assert action in text
    # Nothing beyond the legal set may be suggested.
    assert "move:N" not in text
    assert "move:Z" not in text


def test_entities_are_ordered_nearest_first():
    # Mehmet (+3,-1) is distance 4; watervine (-1,+2) is distance 3, so the
    # watervine sorts first. The invariant is distance order, not a fixed name.
    o = obs()
    text = build_prompt(o, build_action_space(o))
    assert text.index("watervine") < text.index("Mehmet")
    # Offsets are relative, so the model does not do the arithmetic.
    assert "(+3,-1)" in text and "(-1,+2)" in text


def test_prompt_phase_states_that_only_answering_is_legal():
    o = obs(
        CandidateAction(id="answer:open", kind="answer", label="Open the passage"),
        CandidateAction(id="answer:cancel", kind="answer", label="Cancel"),
        phase="prompt",
        prompt=Prompt(kind="choice", text="Open the passage?"),
    )
    text = build_prompt(o, build_action_space(o))
    assert "Only answering is legal" in text
    assert "Open the passage?" in text
    assert "move:E" not in text


def test_messages_are_surfaced_because_quests_live_there():
    o = obs(messages=("You are thirsty.", "The watervine farmer wants water."))
    text = build_prompt(o, build_action_space(o))
    assert "watervine farmer wants water" in text


def test_system_prompt_forbids_anything_but_the_id():
    assert "nothing else" in SYSTEM
    assert "wait" in SYSTEM


def test_messages_have_the_documented_shape():
    o = obs()
    msgs = build_messages(o, build_action_space(o))
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == SYSTEM


@pytest.mark.parametrize("phase", ["command", "prompt"])
def test_prompt_is_bounded_so_it_stays_cheap(phase: str):
    o = obs(
        *(
            (CandidateAction(id="answer:open", kind="answer", label="Open"),
             CandidateAction(id="answer:cancel", kind="answer", label="Cancel"))
            if phase == "prompt"
            else (CandidateAction(id="move:E", kind="move", label="Move E"),)
        ),
        phase=phase,
        prompt=Prompt(kind="choice", text="Open the passage?") if phase == "prompt" else None,
    )
    text = build_prompt(o, build_action_space(o))
    # A long prompt is a prompt the model starts explaining in.
    assert len(text) < 600
