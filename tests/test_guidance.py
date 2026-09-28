"""The action space is the one place that decides what is legal.

These run against the mock backend, so they hold regardless of whether a live
Qud session is available.
"""
from __future__ import annotations

import pytest

from qudgym.guidance import ActionSpace, IllegalAction, build_action_space
from qudgym.models import CandidateAction, Observation, Player, PerceivedEntity


def make_observation(*actions: CandidateAction) -> Observation:
    return Observation(
        episode_id="ep",
        decision_id="ep:0",
        turn=1,
        phase="command",
        player=Player(x=37, y=22, hp=17, max_hp=17),
        tiles=("...",),
        entities=(PerceivedEntity(id="e1", name="Mehmet", x=40, y=21),),
        actions=actions or (
            CandidateAction(id="move:E", kind="move", label="Move E"),
            CandidateAction(id="wait", kind="wait", label="Wait one turn"),
        ),
    )


def test_grammar_accepts_exactly_the_candidate_ids():
    space = build_action_space(make_observation())
    assert space.candidates == ("move:E", "wait")
    assert space.grammar == 'start: "move:E" | "wait"\n'


def test_validate_rejects_an_action_outside_the_candidate_set():
    space = build_action_space(make_observation())
    assert space.validate("move:E") == "move:E"
    with pytest.raises(IllegalAction) as excinfo:
        space.validate("move:N")
    assert excinfo.value.action_id == "move:N"
    # The error carries the candidates, so a caller can retry without guessing.
    assert excinfo.value.candidates == ["move:E", "wait"]


def test_intent_verbs_survive_into_the_grammar():
    obs = make_observation(
        CandidateAction(id="talk:Mehmet", kind="interact", label="talk Mehmet",
                        arguments={"target": "Mehmet"}),
        CandidateAction(id="get:door", kind="inventory", label="get door"),
        CandidateAction(id="history", kind="info", label="Message history"),
    )
    space = build_action_space(obs)
    assert space.contains("talk:Mehmet")
    assert space.kinds["talk:Mehmet"] == "interact"
    assert space.kinds["get:door"] == "inventory"
    assert space.kinds["history"] == "info"


def test_extract_accepts_the_shapes_models_actually_return():
    space = build_action_space(make_observation())
    assert space.extract("move:E") == "move:E"
    assert space.extract("```\nmove:E\n```") == "move:E"
    assert space.extract('{"action_id": "wait"}') == "wait"
    assert space.extract("I will move east.\nmove:E") == "move:E"
    assert space.extract("- move:E") == "move:E"


def test_extract_refuses_anything_else_instead_of_guessing():
    space = build_action_space(make_observation())
    for bad in ("move:N", "I think we should go north", "", "{}"):
        with pytest.raises(IllegalAction):
            space.extract(bad)


def test_extract_accepts_the_markdown_a_told_model_actually_produces():
    # A model told to reply with an action id habitually wraps it. Rejecting
    # these would reject a compliant answer and burn a retry for nothing.
    space = build_action_space(make_observation())
    for good in (
        "**Action: `move:E`**",
        "**move:E**",
        "`move:E`",
        "Action: move:E",
        "**Action:** move:E",
        "```\nAction: move:E\n```",
        "I will move east.\n\n**Action: `move:E`**",
        '{"action": "move:E"}',
    ):
        assert space.extract(good) == "move:E", good


def test_the_retry_keeps_the_observation_rather_than_replacing_it():
    # Replacing the prompt with a bare id list left the model with no game
    # state, and it answered that it was a coding assistant instead.
    space = build_action_space(make_observation())
    correction = space.retry_message("I pick something")
    assert "move:E" in correction
    assert "wait" in correction
    assert "not accepted" in correction


def test_a_boundary_with_no_actions_is_rejected():
    # The contract already forbids a nonterminal boundary with no actions; the
    # action space must not be the place that discovers it late.
    space = ActionSpace(candidates=(), kinds={}, grammar='start: ""\n')
    assert space.action_ids == []
    with pytest.raises(IllegalAction):
        space.validate("anything")


def test_grammar_is_valid_for_llguidance_when_available():
    llguidance = pytest.importorskip("llguidance")
    space = build_action_space(make_observation())
    # Raises if the grammar is malformed.
    llguidance.LLMatcher.validate_grammar(space.grammar)


def _space() -> ActionSpace:
    """A candidate set where one movement is a prefix of another."""
    return build_action_space(
        make_observation(
            CandidateAction(id="move:N", kind="move", label="Move north"),
            CandidateAction(id="move:NE", kind="move", label="Move northeast"),
            CandidateAction(id="move:E", kind="move", label="Move east"),
            CandidateAction(id="wait", kind="wait", label="Wait"),
        )
    )


def test_a_prefix_candidate_never_wins_over_the_longer_one():
    # move:N is a prefix of move:NE, so substring matching turned a compliant
    # northeast answer into north.
    space = _space()
    assert space.extract("**Action: `move:NE`**") == "move:NE"
    assert space.extract("move:NE") == "move:NE"


def test_a_longer_word_is_not_truncated_into_a_candidate():
    space = _space()
    for bad in ("move:Evil", "do not wait", "waited",
                "The action is *move:E*."):
        with pytest.raises(IllegalAction):
            space.extract(bad)


def test_naming_two_candidates_is_ambiguous_not_first_wins():
    space = _space()
    with pytest.raises(IllegalAction):
        space.extract("move:N\nmove:E")
    with pytest.raises(IllegalAction):
        space.extract("action_id: move:N or maybe move:NE")
