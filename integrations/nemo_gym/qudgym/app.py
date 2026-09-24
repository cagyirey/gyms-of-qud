"""NeMo Gym native GymnasiumServer adapter; mock backend only in this first slice.

Reviewed against NVIDIA-NeMo/Gym commit
1c8261080bdc881b3e9b7f870e6418f160516991. Not runtime-tested without NeMo.
Place this directory at resources_servers/qudgym in a pinned NeMo Gym checkout,
with the qudgym package installed in that resource server's environment.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import Field, PrivateAttr, ValidationError

from nemo_gym.base_resources_server import BaseResourcesServerConfig
from nemo_gym.openai_utils import NeMoGymResponse
from nemo_gym.server_utils import SESSION_ID_KEY
from resources_servers.gymnasium.base import GymnasiumServer, extract_text

from qudgym.errors import QudGymError
from qudgym.models import Identifier, Model
from qudgym.sessions import SessionManager


class QudGymConfig(BaseResourcesServerConfig):
    pass


class ActionSelection(Model):
    action_id: Identifier
    decision_id: Identifier


class SeedSpec(Model):
    seed: int = Field(default=0, ge=0, le=2**32-1, strict=True)
    max_decisions: int = Field(default=128, ge=1, le=10000, strict=True)


class Empty(Model):
    pass


class QudGymServer(GymnasiumServer):
    config: QudGymConfig
    _sessions: SessionManager = PrivateAttr(default_factory=SessionManager)

    def setup_webserver(self):
        app = super().setup_webserver()
        app.post('/close')(self.close_endpoint)
        return app

    def _prune(self):
        active = self._sessions.active_keys()
        for key, state in list(self.session_state.items()):
            if state['internal'] not in active:
                self.session_state.pop(key, None)

    async def reset(self, metadata: dict, session_id: str | None = None):
        if not session_id:
            raise HTTPException(400, 'NeMo session middleware did not provide a session ID')
        self._prune()
        spec = SeedSpec(seed=metadata.get('seed', 0), max_decisions=metadata.get('max_decisions', 128))
        await self.close_session(session_id)
        try:
            internal, observation = self._sessions.seed(seed=spec.seed, max_decisions=spec.max_decisions)
        except QudGymError as exc:
            # Infrastructure failure is an HTTP error, NOT a fabricated zero-reward episode.
            raise HTTPException(503, exc.code) from exc
        self.session_state[session_id] = {'internal': internal, 'attempts': 0,
                                          'limit': spec.max_decisions}
        return observation.model_dump_json(), {'is_mock': True, 'task_id': 'mock-reach-exit-v1',
                                                'objective_version': 'sparse-v1',
                                                'supports_explicit_close': True}

    async def step(self, action: NeMoGymResponse, metadata: dict, session_id: str | None = None):
        self._prune()
        if session_id not in self.session_state:
            raise HTTPException(410, 'Rollout session is missing or expired')
        state = self.session_state[session_id]
        state['attempts'] += 1
        try:
            text = extract_text(action)
            if len(text) > 1024:
                raise ValueError('Action output is too long')
            chosen = ActionSelection.model_validate_json(text)
            result = self._sessions.step(state['internal'], action_id=chosen.action_id,
                                         decision_id=chosen.decision_id)
        except (ValidationError, ValueError):
            return self._policy_error(state, 'invalid_action_json')
        except QudGymError as exc:
            if exc.code in ('invalid_action', 'stale_decision'):
                return self._policy_error(state, exc.code)
            raise HTTPException(410 if exc.code == 'session_missing' else 500, exc.code) from exc
        capped = state['attempts'] >= state['limit'] and not result.terminated
        info = {'is_mock': True, **result.metrics.model_dump(mode='json'),
                'agent_attempts': state['attempts']}
        if capped and not result.truncated:
            info['outcome'] = 'agent_attempt_limit'
        return (result.observation.model_dump_json(), result.reward, result.terminated,
                result.truncated or capped, info)

    def _policy_error(self, state, code):
        # Invalid model output consumes harness budget but does not advance game/RNG state.
        observation = self._sessions.observe(state['internal'])
        capped = state['attempts'] >= state['limit']
        return (observation.model_dump_json(), 0.0, False, capped,
                {'is_mock': True, 'policy_error': code, 'agent_attempts': state['attempts'],
                 'outcome': 'agent_attempt_limit' if capped else 'ongoing'})

    async def close_session(self, session_id: str | None):
        state = self.session_state.pop(session_id, None)
        if state:
            self._sessions.close(state['internal'])

    async def close_endpoint(self, body: Empty, request: Request):
        await self.close_session(request.session.get(SESSION_ID_KEY))
        return {'closed': True}


if __name__ == '__main__':
    QudGymServer.run_webserver()
