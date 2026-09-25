"""NeMo adapter idempotency. Stubs stand in for the pinned GymnasiumServer import."""
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qudgym.mock import MockBackend

ROOT = Path(__file__).resolve().parents[1]
_NEMO_STUBBED = False


def _install_nemo_stubs():
    global _NEMO_STUBBED
    if 'resources_servers.gymnasium.base' in sys.modules and hasattr(sys.modules['resources_servers.gymnasium.base'], 'extract_text'):
        return _NEMO_STUBBED

    nemo = types.ModuleType('nemo_gym')
    base_resources = types.ModuleType('nemo_gym.base_resources_server')
    openai_utils = types.ModuleType('nemo_gym.openai_utils')
    server_utils = types.ModuleType('nemo_gym.server_utils')
    resources = types.ModuleType('resources_servers')
    gymnasium_pkg = types.ModuleType('resources_servers.gymnasium')
    gymnasium_base = types.ModuleType('resources_servers.gymnasium.base')

    class BaseResourcesServerConfig(BaseModel):
        model_config = ConfigDict(extra='allow')

    class NeMoGymResponse(BaseModel):
        model_config = ConfigDict(extra='allow')
        output: list = Field(default_factory=list)

    class GymnasiumServer(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        session_state: dict = Field(default_factory=dict)

        async def close_session(self, session_id):
            self.session_state.pop(session_id, None)

    def extract_text(response):
        parts = []
        for item in response.output:
            if item.type == 'message':
                content = item.content
                if isinstance(content, str):
                    parts.append(content)
                else:
                    for piece in content:
                        if piece.type == 'output_text':
                            parts.append(piece.text)
        return ''.join(parts)

    base_resources.BaseResourcesServerConfig = BaseResourcesServerConfig
    openai_utils.NeMoGymResponse = NeMoGymResponse
    server_utils.SESSION_ID_KEY = 'session_id'
    gymnasium_base.GymnasiumServer = GymnasiumServer
    gymnasium_base.extract_text = extract_text
    gymnasium_pkg.base = gymnasium_base
    resources.gymnasium = gymnasium_pkg
    nemo.base_resources_server = base_resources
    nemo.openai_utils = openai_utils
    nemo.server_utils = server_utils
    sys.modules.update({
        'nemo_gym': nemo,
        'nemo_gym.base_resources_server': base_resources,
        'nemo_gym.openai_utils': openai_utils,
        'nemo_gym.server_utils': server_utils,
        'resources_servers': resources,
        'resources_servers.gymnasium': gymnasium_pkg,
        'resources_servers.gymnasium.base': gymnasium_base,
    })
    _NEMO_STUBBED = True
    return True


def _load_app():
    _install_nemo_stubs()
    name = 'qudgym_nemo_app_under_test'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / 'integrations/nemo_gym/qudgym/app.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Piece:
    def __init__(self, text):
        self.type = 'output_text'
        self.text = text


class _Message:
    def __init__(self, text):
        self.type = 'message'
        self.content = [_Piece(text)]


class _Action:
    def __init__(self, text):
        self.output = [_Message(text)]


def _server(**config):
    app = _load_app()
    if _install_nemo_stubs():
        return app.QudGymServer(config=app.QudGymConfig(**config))
    from nemo_gym.server_utils import ServerClient
    values = {
        'host': '127.0.0.1',
        'port': 0,
        'entrypoint': 'app.py',
        'name': 'qudgym-test',
        **config,
    }
    return app.QudGymServer(
        config=app.QudGymConfig(**values),
        server_client=MagicMock(spec=ServerClient),
    )


def _action(action_id, decision_id):
    return _Action(json.dumps({'action_id': action_id, 'decision_id': decision_id}))


def test_multiple_server_workers_are_rejected():
    with pytest.raises(ValidationError):
        _server(num_workers=2)


def test_retried_step_returns_the_committed_transition_once():
    async def scenario():
        server = _server()
        observation, info = await server.reset(
            {'seed': 7, 'max_decisions': 8, '_ng_task_index': 1, '_ng_rollout_index': 0}, 'cookie-1')
        assert info['supports_step_idempotency'] is True
        decision_id = json.loads(observation)['decision_id']
        action = _action('wait', decision_id)
        metadata = {'_ng_step_request_id': 'step-1'}
        first = await server.step(action, metadata, 'cookie-1')
        second = await server.step(action, metadata, 'cookie-1')
        assert second[0] == first[0]
        assert second[1] == first[1]
        assert json.loads(second[0])['turn'] == 1
        assert second[4]['agent_attempts'] == 1
        assert len(server._sessions.active_keys()) == 1

    asyncio.run(scenario())


def test_terminal_step_replay_survives_session_close():
    async def scenario():
        server = _server()
        observation, _info = await server.reset(
            {'seed': 7, 'max_decisions': 1, '_ng_task_index': 1, '_ng_rollout_index': 2}, 'cookie-1')
        decision_id = json.loads(observation)['decision_id']
        action = _action('wait', decision_id)
        metadata = {'_ng_step_request_id': 'terminal-1'}
        first = await server.step(action, metadata, 'cookie-1')
        assert first[3] is True and first[1] == 0.0
        # GymnasiumServer._step_endpoint closes after a terminal or truncated step.
        await server.close_session('cookie-1')
        replay = await server.step(action, metadata, 'cookie-1')
        assert replay[1:] == first[1:]
        assert json.loads(replay[0])['turn'] == 1

    asyncio.run(scenario())


def test_closed_step_cache_is_globally_bounded_and_expires(monkeypatch):
    async def scenario():
        app = _load_app()
        clock = [1.0]
        monkeypatch.setattr(app.time, 'monotonic', lambda: clock[0])
        server = _server(closed_step_cache_max_sessions=2, closed_step_cache_ttl_seconds=5)
        for index in range(4):
            session_id = f'cookie-{index}'
            metadata = {'seed': 7, 'max_decisions': 1, '_ng_task_index': index}
            observation, _info = await server.reset(metadata, session_id)
            decision_id = json.loads(observation)['decision_id']
            await server.step(
                _action('wait', decision_id),
                {'_ng_step_request_id': f'step-{index}'},
                session_id,
            )
            await server.close_session(session_id)
        assert len(server._step_cache) == 2
        assert len(server._closed_step_cache) == 2
        clock[0] = 10.0
        server._prune_closed_step_caches()
        assert not server._step_cache
        assert not server._closed_step_cache

    asyncio.run(scenario())


def test_model_visible_native_and_agent_eye_views_exclude_backend_sentinel(monkeypatch):
    sentinel = "backend-only-sentinel-must-not-render"

    class SentinelBackend(MockBackend):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.hidden_sentinel = sentinel
            self._rng.seed(sentinel)

    import qudgym.sessions as sessions_module
    monkeypatch.setattr(sessions_module, "MockBackend", SentinelBackend)

    async def scenario():
        for index, representation in enumerate(("native", "agent-eye-v1")):
            server = _server()
            observation, _info = await server.reset(
                {"representation": representation, "seed": 7, "max_decisions": 16},
                f"privacy-{index}",
            )
            rendered = [observation]
            for step, action in enumerate(("move:E", "move:E", "answer:open", "move:E", "move:E"), 1):
                current = server._sessions.observe(server._state(f"privacy-{index}")["internal"])
                result = await server.step(
                    _action(action, current.decision_id),
                    {"_ng_step_request_id": f"privacy-{index}-{step}"},
                    f"privacy-{index}",
                )
                observation = result[0]
                rendered.append(observation)
            assert all(sentinel not in text for text in rendered)

    asyncio.run(scenario())


def test_same_step_id_with_a_different_action_is_rejected():
    async def scenario():
        server = _server()
        observation, _info = await server.reset(
            {'seed': 3, 'max_decisions': 8, '_ng_task_index': 4, '_ng_rollout_index': 0}, 'cookie-1')
        decision_id = json.loads(observation)['decision_id']
        metadata = {'_ng_step_request_id': 'same-id'}
        await server.step(_action('wait', decision_id), metadata, 'cookie-1')
        replayed = json.loads((await server.step(_action('wait', decision_id), metadata, 'cookie-1'))[0])
        assert replayed['turn'] == 1
        with pytest.raises(HTTPException) as exc:
            await server.step(_action('move:E', decision_id), metadata, 'cookie-1')
        assert exc.value.status_code == 409
        assert json.loads((await server.step(_action('wait', decision_id), metadata, 'cookie-1'))[0])['turn'] == 1

    asyncio.run(scenario())


def test_invalid_step_reply_is_cached_before_a_retry():
    async def scenario():
        server = _server()
        await server.reset(
            {'seed': 7, 'max_decisions': 8, '_ng_task_index': 5, '_ng_rollout_index': 0}, 'cookie-1')
        action = _Action('not-json')
        metadata = {'_ng_step_request_id': 'invalid-1'}
        first = await server.step(action, metadata, 'cookie-1')
        second = await server.step(action, metadata, 'cookie-1')
        assert second == first
        assert second[4]['agent_attempts'] == 1

    asyncio.run(scenario())


def test_render_failure_faults_worker_instead_of_restepping(monkeypatch):
    async def scenario():
        server = _server()
        observation, _info = await server.reset(
            {'seed': 7, 'max_decisions': 8, '_ng_task_index': 6, '_ng_rollout_index': 0}, 'cookie-1')
        decision_id = json.loads(observation)['decision_id']
        state = server.session_state['cookie-1']
        monkeypatch.setattr(state['presenter'], 'render', lambda _observation: (_ for _ in ()).throw(ValueError('render failed')))
        metadata = {'_ng_step_request_id': 'render-failure'}
        with pytest.raises(HTTPException) as first:
            await server.step(_action('wait', decision_id), metadata, 'cookie-1')
        assert first.value.status_code == 500
        assert state['faulted'] is True
        with pytest.raises(HTTPException) as retry:
            await server.step(_action('wait', decision_id), metadata, 'cookie-1')
        assert retry.value.status_code == 500
        with pytest.raises(HTTPException) as fresh:
            await server.step(_action('wait', decision_id), {'_ng_step_request_id': 'fresh'}, 'cookie-1')
        assert fresh.value.status_code == 500

    asyncio.run(scenario())


def test_reset_rebind_rejects_an_active_target_cookie():
    async def scenario():
        server = _server()
        meta = {'seed': 7, 'max_decisions': 8, '_ng_task_index': 7, '_ng_rollout_index': 0}
        await server.reset(meta, 'cookie-source')
        await server.reset({**meta, '_ng_rollout_index': 1}, 'cookie-target')
        with pytest.raises(HTTPException) as conflict:
            await server.reset(meta, 'cookie-target')
        assert conflict.value.status_code == 409

    asyncio.run(scenario())


def test_reset_identity_separates_representations():
    async def scenario():
        server = _server()
        metadata = {'seed': 7, 'max_decisions': 8, '_ng_task_index': 8, '_ng_rollout_index': 0}
        native, _ = await server.reset(metadata, 'native-cookie')
        rich, _ = await server.reset({**metadata, 'representation': 'agent-eye-v1'}, 'rich-cookie')
        assert json.loads(native)['episode_id'] != json.loads(rich)['current']['episode_id']
        assert len(server._sessions.active_keys()) == 2

    asyncio.run(scenario())


def test_expired_session_does_not_replay_a_cached_step(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr('qudgym.sessions.time.monotonic', lambda: clock[0])

    async def scenario():
        server = _server()
        observation, _info = await server.reset(
            {'seed': 7, 'max_decisions': 8, '_ng_task_index': 1, '_ng_rollout_index': 4}, 'cookie-1')
        decision_id = json.loads(observation)['decision_id']
        action = _action('wait', decision_id)
        metadata = {'_ng_step_request_id': 'step-expire'}
        await server.step(action, metadata, 'cookie-1')
        clock[0] = 2000
        with pytest.raises(HTTPException) as exc:
            await server.step(action, metadata, 'cookie-1')
        assert exc.value.status_code == 410

    asyncio.run(scenario())


def test_lost_reset_rebinds_one_session_to_the_retried_cookie():
    async def scenario():
        server = _server()
        meta = {'seed': 7, 'max_decisions': 8, '_ng_task_index': 3, '_ng_rollout_index': 1}
        first, _info = await server.reset(meta, 'cookie-lost')
        second, _info = await server.reset(meta, 'cookie-retry')
        assert json.loads(first)['episode_id'] == json.loads(second)['episode_id']
        assert len(server._sessions.active_keys()) == 1
        other, _info = await server.reset({**meta, '_ng_rollout_index': 9}, 'cookie-other')
        assert json.loads(other)['episode_id'] != json.loads(first)['episode_id']
        assert len(server._sessions.active_keys()) == 2

    asyncio.run(scenario())


def test_invalid_task_fields_are_rejected_before_session_creation():
    async def scenario():
        server = _server()
        with pytest.raises(HTTPException) as exc:
            await server.reset({'seed': -1, 'max_decisions': 8}, 'cookie-invalid')
        assert exc.value.status_code == 422
        assert len(server._sessions.active_keys()) == 0

    asyncio.run(scenario())


def test_scripted_native_actions_reach_mock_success():
    async def scenario():
        server = _server()
        observation, info = await server.reset(
            {'seed': 7, 'max_decisions': 16, '_ng_task_index': 0, '_ng_rollout_index': 0},
            'cookie-success',
        )
        assert info['is_mock'] is True
        result = None
        for index, action in enumerate(('move:E', 'move:E', 'answer:open', 'move:E', 'move:E'), 1):
            decision_id = json.loads(observation)['decision_id']
            result = await server.step(
                _action(action, decision_id),
                {'_ng_step_request_id': f'success-{index}'},
                'cookie-success',
            )
            observation, reward, terminated, truncated, step_info = result
            if terminated or truncated:
                break
        assert reward == 1.0
        assert terminated is True
        assert truncated is False
        assert step_info['outcome'] == 'success'
        assert step_info['turns_elapsed'] == 4
        assert step_info['decisions_elapsed'] == 5
        assert step_info['is_mock'] is True

    asyncio.run(scenario())
