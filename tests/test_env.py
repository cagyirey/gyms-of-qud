from __future__ import annotations

import pytest

from qudgym import QudEnv
from qudgym.errors import TransportUncertain
from qudgym.models import Capabilities


def test_a_lost_reply_blocks_the_next_mutation_even_when_unreplayable():
    """A request that may have committed must not be followed by another one.

    `replayable` says whether the transport can re-derive the answer, not
    whether the world is in a known state. Guarding only the replayable case let
    a live request whose reply was lost be followed by a further mutation.
    """
    class Lost:
        def capabilities(self):
            return Capabilities(
                backend="qud-live", game_build="2.0.0", is_mock=False,
                snapshot=False, deterministic_restore=False, full_state_hash=False,
            )

        def reset(self, *, seed=0):
            raise TransportUncertain("reply lost", replayable=False)

    env = QudEnv(Lost())
    with pytest.raises(TransportUncertain):
        env.reset(seed=0)
    assert env.uncertain is True
    assert env.reward_unknown is True
