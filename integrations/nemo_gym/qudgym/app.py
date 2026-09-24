"""NeMo Gym native GymnasiumServer adapter; mock backend only in this first slice.

Reviewed against NVIDIA-NeMo/Gym commit
1c8261080bdc881b3e9b7f870e6418f160516991. Not runtime-tested without NeMo.
Place this directory at resources_servers/qudgym in a pinned NeMo Gym checkout,
with the qudgym package installed in that resource server's environment.
"""
from __future__ import annotations

import json
from copy import deepcopy

from fastapi import HTTPException, Request
from pydantic import Field, PrivateAttr, ValidationError

from nemo_gym.base_resources_server import BaseResourcesServerConfig
from nemo_gym.openai_utils import NeMoGymResponse
from nemo_gym.server_utils import SESSION_ID_KEY
from resources_servers.gymnasium.base import GymnasiumServer, extract_text

from qudgym.errors import QudGymError
from qudgym.eye.presenter import ObservationPresenter, Representation
from qudgym.models import Identifier, Model
from qudgym.sessions import SessionManager


class QudGymConfig(BaseResourcesServerConfig):
    pass


class ActionSelection(Model):
    action_id: Identifier
    decision_id: Identifier


class SeedSpec(Model):
    representation: Representation = "native"
    seed: int = Field(default=0, ge=0, le=2**32-1, strict=True)
    max_decisions: int = Field(default=128, ge=1, le=10000, strict=True)


class Empty(Model):
    pass


def _reset_identity(metadata: dict, *, seed: int, max_decisions: int,
                    representation: str) -> str | None:
    names = ('_ng_reset_request_id', '_ng_task_index', '_ng_rollout_index', '_ng_attempt_index')
    identity = {name: metadata[name] for name in names if metadata.get(name) is not None}
    if not identity:
        return None
    identity['seed'] = seed
    identity['max_decisions'] = max_decisions
    identity['representation'] = representation
    return json.dumps(identity, sort_keys=True, default=str)


class QudGymServer(GymnasiumServer):
    config: QudGymConfig
    _sessions: SessionManager = PrivateAttr(default_factory=SessionManager)
    # Keyed by the NeMo session cookie. Kept after close_session: the base
    # endpoint closes a terminal step before the client may have read the reply.
    _step_cache: dict = PrivateAttr(default_factory=dict)
    _reset_cache: dict = PrivateAttr(default_factory=dict)

    def setup_webserver(self):
        app = super().setup_webserver()
        app.post('/close')(self.close_endpoint)
        return app

    def _prune(self):
        active = self._sessions.active_keys()
        for key, state in list(self.session_state.items()):
            if state['internal'] not in active:
                self.session_state.pop(key, None)
                self._step_cache.pop(key, None)

    def _cached_step(self, session_id, request_id, fingerprint):
        cached = self._step_cache.get(session_id)
        if cached is None or cached[0] != request_id or fingerprint is None:
            return None
        if cached[1] != fingerprint:
            raise HTTPException(409, 'Step request id reused with different action')
        observation, reward, terminated, truncated, info = cached[2]
        return observation, reward, terminated, truncated, deepcopy(info)

    @staticmethod
    def _render(presenter: ObservationPresenter, observation):
        try:
            return presenter.render(observation)
        except (ValidationError, ValueError) as exc:
            # A failed presentation is an infrastructure failure, not a game loss.
            raise HTTPException(500, 'observation representation failed') from exc

    async def reset(self, metadata: dict, session_id: str | None = None):
        if not session_id:
            raise HTTPException(400, 'NeMo session middleware did not provide a session ID')
        self._prune()
        spec = SeedSpec(seed=metadata.get('seed', 0), max_decisions=metadata.get('max_decisions', 128),
                        representation=metadata.get('representation', 'native'))
        key = _reset_identity(metadata, seed=spec.seed, max_decisions=spec.max_decisions,
                              representation=spec.representation)
        if key is not None:
            cached = self._reset_cache.get(key)
            if cached is not None:
                old_session, payload = cached
                state = self.session_state.get(old_session)
                if state is not None:
                    if old_session != session_id:
                        # The retry never received the first Set-Cookie, so it has a new id.
                        self.session_state[session_id] = self.session_state.pop(old_session)
                        moved = self._step_cache.pop(old_session, None)
                        if moved is not None:
                            self._step_cache[session_id] = moved
                        self._reset_cache[key] = (session_id, payload)
                    observation, info = payload
                    return observation, deepcopy(info)
                self._reset_cache.pop(key, None)
        self._step_cache.pop(session_id, None)
        await self.close_session(session_id)
        internal = None
        try:
            internal, observation = self._sessions.seed(seed=spec.seed, max_decisions=spec.max_decisions)
            presenter = ObservationPresenter(spec.representation)
            rendered = self._render(presenter, observation)
            self.session_state[session_id] = {'internal': internal, 'attempts': 0,
                                              'limit': spec.max_decisions,
                                              'presenter': presenter}
            internal = None
            payload = (rendered, {
                'is_mock': True, 'representation': spec.representation,
                'task_id': 'mock-reach-exit-v1', 'objective_version': 'sparse-v1',
                'supports_explicit_close': True,
                # gymnasium_agent sends _ng_step_request_id only when this is true.
                'supports_step_idempotency': True,
            })
            if key is not None:
                self._reset_cache[key] = (session_id, payload)
                if len(self._reset_cache) > 256:
                    self._reset_cache.pop(next(iter(self._reset_cache)))
            return payload[0], deepcopy(payload[1])
        except QudGymError as exc:
            # Infrastructure failure is an HTTP error, NOT a fabricated zero-reward episode.
            raise HTTPException(503, exc.code) from exc
        finally:
            if internal is not None:
                self._sessions.close(internal)

    async def step(self, action: NeMoGymResponse, metadata: dict, session_id: str | None = None):
        self._prune()
        request_id = metadata.get('_ng_step_request_id')
        if request_id is not None and (
                not isinstance(request_id, str) or not request_id or len(request_id) > 128):
            raise HTTPException(422, '_ng_step_request_id must be a non-empty string of at most 128 characters')
        fingerprint = None
        if request_id is not None:
            try:
                fingerprint = extract_text(action)
                if len(fingerprint) > 1024:
                    raise ValueError('Action output is too long')
            except (ValidationError, ValueError, AttributeError):
                fingerprint = None
            cached = self._cached_step(session_id, request_id, fingerprint)
            if cached is not None:
                return cached
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
        info = {'is_mock': True, 'representation': state['presenter'].representation,
                **result.metrics.model_dump(mode='json'), 'agent_attempts': state['attempts']}
        if capped and not result.truncated:
            info['outcome'] = 'agent_attempt_limit'
        rendered = self._render(state['presenter'], result.observation)
        payload = (rendered, result.reward, result.terminated,
                   result.truncated or capped, info)
        if request_id is not None and fingerprint is not None:
            self._step_cache[session_id] = (request_id, fingerprint, deepcopy(payload))
            if len(self._step_cache) > 256:
                self._step_cache.pop(next(iter(self._step_cache)))
        return payload[0], payload[1], payload[2], payload[3], deepcopy(payload[4])

    def _policy_error(self, state, code):
        # Invalid model output consumes harness budget but does not advance game/RNG state.
        observation = self._sessions.observe(state['internal'])
        capped = state['attempts'] >= state['limit']
        rendered = self._render(state['presenter'], observation)
        return (rendered, 0.0, False, capped,
                {'is_mock': True, 'representation': state['presenter'].representation,
                 'policy_error': code, 'agent_attempts': state['attempts'],
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
