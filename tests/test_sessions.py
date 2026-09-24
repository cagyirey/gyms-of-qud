import pytest
from qudgym.errors import QudGymError
from qudgym.sessions import SessionManager


def test_sessions_are_isolated_and_rewards_authoritative():
    manager = SessionManager()
    a, obs = manager.seed(seed=4)
    b, _ = manager.seed(seed=4)
    assert manager.verify(a)['reward'] == 0
    for action in ('move:E','move:E','answer:open','move:E','move:E'):
        result = manager.step(a, action_id=action, decision_id=obs.decision_id)
        obs = result.observation
    assert manager.verify(a)['reward'] == 1
    assert manager.verify(b)['reward'] == 0
    assert manager.observe(b).turn == 0


def test_capacity_close_and_missing_session():
    manager = SessionManager(max_sessions=1)
    session, _ = manager.seed()
    with pytest.raises(QudGymError) as exc:
        manager.seed()
    assert exc.value.code == 'capacity'
    manager.close(session)
    with pytest.raises(QudGymError) as exc:
        manager.verify(session)
    assert exc.value.code == 'session_missing'
    manager.seed()


def test_stale_actions_rejected_per_session():
    manager = SessionManager()
    session, obs = manager.seed()
    manager.step(session, action_id='wait', decision_id=obs.decision_id)
    with pytest.raises(QudGymError) as exc:
        manager.step(session, action_id='wait', decision_id=obs.decision_id)
    assert exc.value.code == 'stale_decision'


def test_expired_sessions_do_not_produce_fake_rewards(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr('qudgym.sessions.time.monotonic', lambda:clock[0])
    manager = SessionManager(ttl_seconds=5)
    session, _ = manager.seed()
    clock[0] = 7
    with pytest.raises(QudGymError) as exc:
        manager.verify(session)
    assert exc.value.code == 'session_missing'
    manager.seed()
