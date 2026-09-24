import json
import math
import pytest
from pydantic import ValidationError
from qudgym import QudEnv, MockBackend
from qudgym.errors import QudGymError
from qudgym.models import Observation, RpcRequest, RpcResponse
from qudgym.policy import choose_action
from qudgym.trajectory import TrajectoryRecorder


def act(backend, action):
    return backend.step(action, decision_id=backend.observe().decision_id)


def test_smoke_success():
    with QudEnv(MockBackend()) as env:
        env.reset(seed=7)
        for action in ('move:E', 'move:E', 'answer:open', 'move:E', 'move:E'):
            result = env.step(action)
        assert result.terminated and not result.truncated
        assert result.reward == 1
        assert result.metrics.turns_elapsed == 4
        assert result.metrics.decisions_elapsed == 5
        assert result.observation.actions == ()


def test_prompt_is_a_zero_turn_decision_boundary():
    b = MockBackend()
    b.reset()
    first = act(b, 'move:E')
    prompt = act(b, 'move:E')
    assert prompt.observation.phase == 'prompt'
    assert prompt.observation.turn == first.observation.turn
    assert prompt.observation.decision_id != first.observation.decision_id
    cancel = act(b, 'answer:cancel')
    assert cancel.observation.phase == 'command'
    assert cancel.observation.turn == first.observation.turn


def test_replay_includes_rng_and_invalidates_old_cursors():
    b = MockBackend(allow_oracle=True)
    initial = b.reset(seed=14)
    snapshot = b.snapshot()
    root_hash = b.state_hash()
    actions = ('move:E', 'move:E', 'answer:open', 'move:E', 'wait')
    for action in actions:
        first = act(b, action)
    branch_hash = b.state_hash()
    restored = b.restore(snapshot.handle)
    assert b.state_hash() == root_hash
    assert restored.observation.decision_id != initial.observation.decision_id
    with pytest.raises(QudGymError, match='boundary has changed'):
        b.step('wait', decision_id=initial.observation.decision_id)
    for action in actions:
        replay = act(b, action)
    assert b.state_hash() == branch_hash
    assert first.observation.player == replay.observation.player
    assert first.metrics == replay.metrics


def test_reset_same_seed_has_same_full_state_hash():
    b = MockBackend(allow_oracle=True)
    b.reset(seed=123)
    h = b.state_hash()
    b.reset(seed=123)
    assert b.state_hash() == h
    b.reset(seed=124)
    assert b.state_hash() != h


def test_invalid_action_does_not_mutate_any_state():
    b = MockBackend(allow_oracle=True)
    b.reset()
    h = b.state_hash()
    cursor = b.observe().decision_id
    with pytest.raises(QudGymError) as exc:
        act(b, 'inject:arbitrary-command')
    assert exc.value.code == 'invalid_action'
    assert b.state_hash() == h
    assert b.observe().decision_id == cursor


@pytest.mark.parametrize('method,args', [('snapshot', ()), ('state_hash', ()), ('restore', ('x',))])
def test_oracle_disabled_by_default(method, args):
    b = MockBackend()
    b.reset()
    assert not b.capabilities().snapshot
    with pytest.raises(QudGymError) as exc:
        getattr(b, method)(*args)
    assert exc.value.code == 'unsupported'


def test_snapshot_limits_release_and_episode_binding():
    b = MockBackend(allow_oracle=True, max_snapshots=1)
    b.reset()
    s = b.snapshot()
    with pytest.raises(QudGymError) as exc:
        b.snapshot()
    assert exc.value.code == 'snapshot_limit'
    b.release(s.handle)
    with pytest.raises(QudGymError):
        b.restore(s.handle)
    s = b.snapshot()
    b.reset()
    with pytest.raises(QudGymError):
        b.restore(s.handle)


def test_snapshot_aliasing_cannot_change_stored_world():
    b = MockBackend(allow_oracle=True)
    b.reset()
    s = b.snapshot()
    h = b.state_hash()
    for _ in range(3):
        b.restore(s.handle)
        act(b, 'wait')
    b.restore(s.handle)
    assert b.state_hash() == h


def test_time_limit_distinct_from_death_even_at_prompt():
    b = MockBackend(max_decisions=2)
    b.reset()
    act(b, 'move:E')
    result = act(b, 'move:E')
    assert result.truncated and not result.terminated
    assert result.metrics.outcome == 'time_limit'
    assert result.observation.prompt is None
    assert result.observation.actions == ()
    with pytest.raises(QudGymError) as exc:
        act(b, 'wait')
    assert exc.value.code == 'episode_finished'


def test_death_is_terminal():
    b = MockBackend()
    b.reset(seed=42)
    for action in ('move:E', 'move:E', 'answer:open', 'move:E'):
        result = act(b, action)
    while not result.terminated:
        result = act(b, 'wait')
    assert result.metrics.outcome == 'death'
    assert result.observation.player.hp == 0
    assert result.reward == -1


def test_lifecycle():
    b = MockBackend()
    with pytest.raises(QudGymError) as exc:
        b.observe()
    assert exc.value.code == 'reset_required'
    b.reset()
    b.close()
    b.close()
    with pytest.raises(QudGymError) as exc:
        b.reset()
    assert exc.value.code == 'closed'


def test_no_privileged_data_in_observation():
    b = MockBackend(allow_oracle=True)
    b.reset(seed=999)
    b.snapshot()
    raw = b.observe().model_dump_json()
    for secret in ('rng', 'seed', 'snapshot', 'token', 'blueprint', 'true_identity'):
        assert secret not in raw
    assert '?' in raw


def test_schema_rejects_duplicate_actions_and_extra_fields():
    b = MockBackend()
    b.reset()
    data = b.observe().model_dump(mode='json')
    data['actions'].append(data['actions'][0])
    with pytest.raises(ValidationError):
        Observation.model_validate(data)
    with pytest.raises(ValidationError):
        RpcRequest.model_validate({'protocol_version': '0.2', 'request_id': 'x', 'operation': {'op': 'hello'}})
    with pytest.raises(ValidationError):
        RpcRequest.model_validate({'request_id': 'x', 'operation': {'op': 'hello', 'exec': 'anything'}})
    with pytest.raises(ValidationError):
        RpcResponse(request_id='x')


def test_candidate_scorer_contract():
    b = MockBackend()
    b.reset()
    obs = b.observe()
    class Scorer:
        def score(self, observation):
            return {a.id: float(a.id == 'wait') for a in observation.actions}
    assert choose_action(obs, Scorer()) == 'wait'
    class Bad:
        def score(self, observation):
            return {}
    with pytest.raises(ValueError):
        choose_action(obs, Bad())
    class NaN:
        def score(self, observation):
            return {a.id: math.nan for a in observation.actions}
    with pytest.raises(ValueError):
        choose_action(obs, NaN())


def test_trajectory_is_explicit_mock_and_no_overwrite(tmp_path):
    path = tmp_path / 'trace.jsonl'
    with QudEnv(MockBackend()) as env, TrajectoryRecorder(env, path) as trace:
        trace.reset(seed=12)
        trace.step('wait')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]['capabilities']['is_mock'] is True
    assert rows[0]['control_metadata']['seed'] == 12
    assert rows[1]['before']['decision_id'] != rows[1]['transition']['observation']['decision_id']
    assert 'logprobs' not in rows[1]
    with pytest.raises(FileExistsError):
        TrajectoryRecorder(QudEnv(MockBackend()), path)


@pytest.mark.parametrize('seed', [-1, 2**32, True, 1.2])
def test_invalid_seed(seed):
    with pytest.raises(QudGymError):
        MockBackend().reset(seed=seed)
