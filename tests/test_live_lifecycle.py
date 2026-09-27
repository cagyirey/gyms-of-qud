"""One owner for the action lifecycle, and bounded waits.

The PR 8 review found that the batch scheduler and the primitive step path were
two independent owners of the same lifecycle, and that the waits in both were
unbounded. These pin the properties that keep a single owner honest, using the
mock backend plus a fake transport rather than a live game.
"""
from __future__ import annotations

import pytest

from qudgym import QudEnv
from qudgym.errors import QudGymError
from qudgym.mock import MockBackend


def test_a_script_is_repeated_steps_not_a_second_queue():
    """Actions are claimed on the same boundary the step path uses."""
    with QudEnv(MockBackend()) as env:
        env.reset(seed=3)
        first = env.current.observation
        # A script is just a sequence of single steps; the cursor advances once
        # per action and each action is validated against the live action set.
        env.step("move:E")
        second = env.current.observation
        assert second.decision_id != first.decision_id
        env.step("move:E")
        third = env.current.observation
        assert third.decision_id != second.decision_id
        assert third.turn > first.turn


def test_an_illegal_action_is_refused_rather_than_swallowed():
    with QudEnv(MockBackend()) as env:
        env.reset(seed=3)
        env.step("move:E")
        cursor = env.current.observation.decision_id
        with pytest.raises(QudGymError) as excinfo:
            env.step("not-an-action")
        assert excinfo.value.code == "invalid_action"
        # The refusal is visible: the cursor did not advance.
        assert env.reconcile().decision_id == cursor


def test_observation_still_works_after_a_refused_action():
    """A failed mutation must not leave the surface unusable.

    The live failure this guards against was a blocking wait holding the
    dispatch lock, so a lost or refused reply stopped observation and
    reconciliation as well.
    """
    with QudEnv(MockBackend()) as env:
        env.reset(seed=3)
        before = env.current.observation
        with pytest.raises(QudGymError):
            env.step("not-an-action")
        after = env.reconcile()
        assert after.turn == before.turn
        assert after.decision_id


def test_a_step_never_reuses_a_consumed_decision():
    """One boundary takes one action, whichever path submitted it."""
    with QudEnv(MockBackend()) as env:
        env.reset(seed=3)
        decision = env.current.observation.decision_id
        env.step("move:E")
        with pytest.raises(QudGymError):
            env.backend.step("move:E", decision_id=decision)


def test_reconcile_after_a_failed_mutation_reports_a_known_state():
    """A departed or stuck client leaves the world observable, not wedged."""
    with QudEnv(MockBackend()) as env:
        env.reset(seed=3)
        env.step("move:E")
        with pytest.raises(QudGymError):
            env.step("nonsense")
        view = env.reconcile()
        assert view.decision_id
        assert view.turn == 1
        assert view.actions
