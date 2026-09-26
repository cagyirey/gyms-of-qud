"""NeMo Gym native GymnasiumServer adapter; mock backend only in this first slice.

Reviewed against NVIDIA-NeMo/Gym commit
1c8261080bdc881b3e9b7f870e6418f160516991 and runtime-smoked through the native
Gym resource/agent/model servers with a deterministic Responses API fixture.
Place this directory at resources_servers/qudgym in that pinned checkout; the
staging helper installs both local projects into the isolated server environment.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from typing import TypeAlias, TypedDict, cast

from fastapi import HTTPException, Request
from nemo_gym.base_resources_server import BaseResourcesServerConfig
from nemo_gym.openai_utils import NeMoGymResponse
from nemo_gym.server_utils import SESSION_ID_KEY
from pydantic import Field, PrivateAttr, ValidationError
from resources_servers.gymnasium.base import GymnasiumServer, extract_text

from qudgym.errors import QudGymError
from qudgym.eye.presenter import ObservationPresenter, Representation
from qudgym.models import Identifier, Model, Observation
from qudgym.sessions import SessionManager


class StepInfo(TypedDict, total=False):
    is_mock: bool
    representation: Representation
    task_id: str
    objective_version: str
    outcome: str
    turns_elapsed: int
    decisions_elapsed: int
    agent_attempts: int
    policy_error: str


class StepPayload(TypedDict):
    observation: str
    reward: float
    terminated: bool
    truncated: bool
    info: StepInfo


StepReply: TypeAlias = tuple[str, float, bool, bool, StepInfo]


class ResetInfo(TypedDict):
    is_mock: bool
    representation: Representation
    task_id: str
    objective_version: str
    supports_explicit_close: bool
    supports_step_idempotency: bool


ResetPayload: TypeAlias = tuple[str, ResetInfo]


class SessionState(TypedDict):
    internal: str
    attempts: int
    limit: int
    presenter: ObservationPresenter
    faulted: bool


class CachedStep(TypedDict):
    fingerprint: str
    payload: StepPayload


class CachedFault(TypedDict):
    request_id: str
    fingerprint: str


class QudGymConfig(BaseResourcesServerConfig):
    # Session and replay state are process-local; multiple uvicorn workers are unsafe.
    num_workers: int = Field(default=1, ge=1, le=1, strict=True)
    closed_step_cache_max_sessions: int = Field(default=256, ge=1, le=10000, strict=True)
    closed_step_cache_ttl_seconds: float = Field(default=300.0, gt=0, le=86400, strict=True)


class ActionSelection(Model):
    action_id: Identifier
    decision_id: Identifier


class SeedSpec(Model):
    representation: Representation = "native"
    seed: int = Field(default=0, ge=0, le=2**32-1, strict=True)
    max_decisions: int = Field(default=128, ge=1, le=10000, strict=True)


def _seed_spec(metadata: Mapping[str, object]) -> SeedSpec:
    values = {
        'representation': metadata.get('representation', 'native'),
        'seed': metadata.get('seed', 0),
        'max_decisions': metadata.get('max_decisions', 128),
    }
    try:
        return SeedSpec.model_validate(values)
    except ValidationError as exc:
        raise HTTPException(422, 'invalid QudGym task data') from exc


class Empty(Model):
    pass


def _reset_identity(metadata: Mapping[str, object], *, seed: int, max_decisions: int,
                    representation: Representation) -> str | None:
    names = ('_ng_reset_request_id', '_ng_task_index', '_ng_rollout_index', '_ng_attempt_index')
    identity: dict[str, object] = {
        name: metadata[name] for name in names if metadata.get(name) is not None
    }
    if not identity:
        return None
    identity['seed'] = seed
    identity['max_decisions'] = max_decisions
    identity['representation'] = representation
    return json.dumps(identity, sort_keys=True, default=str)


class QudGymServer(GymnasiumServer):
    config: QudGymConfig
    _sessions: SessionManager = PrivateAttr(default_factory=SessionManager)
    # Keyed by the NeMo session cookie. Kept briefly after close_session: the
    # base endpoint closes a terminal step before the client may have read the
    # reply. Closed-session entries are globally bounded by config and TTL.
    _step_cache: dict[str, OrderedDict[str, CachedStep]] = PrivateAttr(default_factory=dict)
    _closed_step_cache: OrderedDict[str, float] = PrivateAttr(default_factory=OrderedDict)
    _step_faults: dict[str, CachedFault] = PrivateAttr(default_factory=dict)
    _reset_cache: dict[str, tuple[str, ResetPayload]] = PrivateAttr(default_factory=dict)

    def setup_webserver(self):
        app = super().setup_webserver()
        app.post('/close')(self.close_endpoint)
        return app

    def _state(self, session_id: str) -> SessionState:
        return cast(SessionState, self.session_state[session_id])

    def _forget_step_cache(self, session_id: str) -> None:
        self._step_cache.pop(session_id, None)
        self._closed_step_cache.pop(session_id, None)
        self._step_faults.pop(session_id, None)

    def _prune_closed_step_caches(self) -> None:
        now = time.monotonic()
        ttl = self.config.closed_step_cache_ttl_seconds
        while self._closed_step_cache:
            session_id, touched = next(iter(self._closed_step_cache.items()))
            if now - touched <= ttl:
                break
            self._forget_step_cache(session_id)
        while len(self._closed_step_cache) > self.config.closed_step_cache_max_sessions:
            session_id, _ = self._closed_step_cache.popitem(last=False)
            self._step_cache.pop(session_id, None)
            self._step_faults.pop(session_id, None)

    def _mark_closed_step_cache(self, session_id: str) -> None:
        if session_id in self._step_cache:
            self._closed_step_cache[session_id] = time.monotonic()
            self._closed_step_cache.move_to_end(session_id)
            self._prune_closed_step_caches()

    def _prune(self) -> None:
        self._prune_closed_step_caches()
        active = self._sessions.active_keys()
        for key in list(self.session_state):
            state = self._state(key)
            if state['internal'] not in active:
                self.session_state.pop(key, None)
                self._forget_step_cache(key)

    @staticmethod
    def _action_fingerprint(action: NeMoGymResponse) -> str:
        try:
            value = extract_text(action)
        except (ValidationError, ValueError, AttributeError):
            try:
                value = action.model_dump_json()
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("action response is not serializable") from exc
        if not isinstance(value, str):
            raise TypeError("action response text is not a string")
        if len(value) > 4096:
            return hashlib.sha256(value.encode('utf-8')).hexdigest()
        return value

    def _cached_step(self, session_id: str | None, request_id: str | None,
                     fingerprint: str | None) -> StepReply | None:
        if session_id is None or request_id is None:
            return None
        self._prune_closed_step_caches()
        fault = self._step_faults.get(session_id)
        if fault is not None:
            if fault['request_id'] != request_id:
                return None
            if fault['fingerprint'] != fingerprint:
                raise HTTPException(409, 'Step request id reused with different action')
            raise HTTPException(500, 'Step presentation failed; worker requires reset')
        cache = self._step_cache.get(session_id)
        if cache is None:
            return None
        cached = cache.get(request_id)
        if cached is None:
            return None
        if cached['fingerprint'] != fingerprint:
            raise HTTPException(409, 'Step request id reused with different action')
        if session_id in self._closed_step_cache:
            self._closed_step_cache[session_id] = time.monotonic()
            self._closed_step_cache.move_to_end(session_id)
        payload = cached['payload']
        return (payload['observation'], payload['reward'], payload['terminated'],
                payload['truncated'], deepcopy(payload['info']))

    def _cache_step(self, session_id: str, request_id: str | None,
                    fingerprint: str | None, payload: StepPayload) -> None:
        if request_id is None or fingerprint is None:
            return
        cache = self._step_cache.setdefault(session_id, OrderedDict())
        cache[request_id] = {'fingerprint': fingerprint, 'payload': deepcopy(payload)}
        cache.move_to_end(request_id)
        while len(cache) > 256:
            cache.popitem(last=False)

    def _return_step(self, session_id: str, request_id: str | None,
                     fingerprint: str | None, payload: StepPayload) -> StepReply:
        self._cache_step(session_id, request_id, fingerprint, payload)
        return (payload['observation'], payload['reward'], payload['terminated'],
                payload['truncated'], deepcopy(payload['info']))

    @staticmethod
    def _render(presenter: ObservationPresenter, observation: Observation) -> str:
        try:
            return presenter.render(observation)
        except (ValidationError, ValueError) as exc:
            # A failed presentation is an infrastructure failure, not a game loss.
            raise HTTPException(500, 'observation representation failed') from exc

    async def reset(self, metadata: Mapping[str, object], session_id: str | None = None):
        if not session_id:
            raise HTTPException(400, 'NeMo session middleware did not provide a session ID')
        self._prune()
        spec = _seed_spec(metadata)
        key = _reset_identity(metadata, seed=spec.seed, max_decisions=spec.max_decisions,
                              representation=spec.representation)
        if key is not None:
            cached = self._reset_cache.get(key)
            if cached is not None:
                old_session, payload = cached
                state = self.session_state.get(old_session)
                if state is not None:
                    if old_session != session_id:
                        if session_id in self.session_state:
                            raise HTTPException(409, 'Target session is already active')
                        # The retry never received the first Set-Cookie, so it has a new id.
                        self.session_state[session_id] = self.session_state.pop(old_session)
                        moved = self._step_cache.pop(old_session, None)
                        if moved is not None:
                            self._step_cache[session_id] = moved
                        self._closed_step_cache.pop(old_session, None)
                        moved_fault = self._step_faults.pop(old_session, None)
                        if moved_fault is not None:
                            self._step_faults[session_id] = moved_fault
                        self._reset_cache[key] = (session_id, payload)
                    observation, info = payload
                    return observation, deepcopy(info)
                self._reset_cache.pop(key, None)
        self._forget_step_cache(session_id)
        await self.close_session(session_id)
        internal: str | None = None
        try:
            internal, observation = self._sessions.seed(seed=spec.seed, max_decisions=spec.max_decisions)
            presenter = ObservationPresenter(spec.representation)
            rendered = self._render(presenter, observation)
            state: SessionState = {
                'internal': internal, 'attempts': 0, 'limit': spec.max_decisions,
                'presenter': presenter, 'faulted': False,
            }
            self.session_state[session_id] = state
            internal = None
            info: ResetInfo = {
                'is_mock': True, 'representation': spec.representation,
                'task_id': 'mock-reach-exit-v1', 'objective_version': 'sparse-v1',
                'supports_explicit_close': True, 'supports_step_idempotency': True,
            }
            payload = (rendered, info)
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

    async def step(self, action: NeMoGymResponse, metadata: Mapping[str, object],
                   session_id: str | None = None):
        self._prune()
        raw_request_id = metadata.get('_ng_step_request_id')
        request_id: str | None
        if raw_request_id is None:
            request_id = None
        elif isinstance(raw_request_id, str) and raw_request_id and len(raw_request_id) <= 128:
            request_id = raw_request_id
        else:
            raise HTTPException(422, '_ng_step_request_id must be a non-empty string of at most 128 characters')
        try:
            fingerprint: str | None = self._action_fingerprint(action) if request_id is not None else None
        except ValueError as exc:
            raise HTTPException(422, 'invalid action response') from exc
        cached = self._cached_step(session_id, request_id, fingerprint)
        if cached is not None:
            return cached
        if session_id is None or session_id not in self.session_state:
            raise HTTPException(410, 'Rollout session is missing or expired')
        state = self._state(session_id)
        if state['faulted']:
            raise HTTPException(500, 'Rollout worker is faulted; reset before continuing')
        state['attempts'] += 1
        try:
            text = extract_text(action)
            if len(text) > 1024:
                raise ValueError('Action output is too long')
            chosen = ActionSelection.model_validate_json(text)
            result = self._sessions.step(state['internal'], action_id=chosen.action_id,
                                         decision_id=chosen.decision_id)
        except (ValidationError, ValueError, AttributeError):
            return self._policy_reply(session_id, state, 'invalid_action_json', request_id, fingerprint)
        except QudGymError as exc:
            if exc.code in ('invalid_action', 'stale_decision'):
                return self._policy_reply(session_id, state, exc.code, request_id, fingerprint)
            raise HTTPException(410 if exc.code == 'session_missing' else 500, exc.code) from exc
        capped = state['attempts'] >= state['limit'] and not result.terminated
        info: StepInfo = {
            'is_mock': True, 'representation': state['presenter'].representation,
            'task_id': result.metrics.task_id,
            'objective_version': result.metrics.objective_version,
            'outcome': result.metrics.outcome,
            'turns_elapsed': result.metrics.turns_elapsed,
            'decisions_elapsed': result.metrics.decisions_elapsed,
            'agent_attempts': state['attempts'],
        }
        if capped and not result.truncated:
            info['outcome'] = 'agent_attempt_limit'
        try:
            rendered = self._render(state['presenter'], result.observation)
        except HTTPException:
            state['faulted'] = True
            if request_id is not None and fingerprint is not None:
                self._step_faults[session_id] = {
                    'request_id': request_id, 'fingerprint': fingerprint,
                }
            raise
        payload: StepPayload = {
            'observation': rendered, 'reward': result.reward,
            'terminated': result.terminated, 'truncated': result.truncated or capped,
            'info': info,
        }
        return self._return_step(session_id, request_id, fingerprint, payload)

    def _policy_reply(self, session_id: str, state: SessionState, code: str,
                      request_id: str | None, fingerprint: str | None) -> StepReply:
        try:
            observation = self._sessions.observe(state['internal'])
            capped = state['attempts'] >= state['limit']
            rendered = self._render(state['presenter'], observation)
        except HTTPException:
            state['faulted'] = True
            raise
        payload: StepPayload = {
            'observation': rendered, 'reward': 0.0, 'terminated': False,
            'truncated': capped,
            'info': {
                'is_mock': True, 'representation': state['presenter'].representation,
                'policy_error': code, 'agent_attempts': state['attempts'],
                'outcome': 'agent_attempt_limit' if capped else 'ongoing',
            },
        }
        return self._return_step(session_id, request_id, fingerprint, payload)

    async def close_session(self, session_id: str | None):
        if session_id is not None:
            self._prune_closed_step_caches()
            self._mark_closed_step_cache(session_id)
        state = self.session_state.pop(session_id, None)
        self._step_faults.pop(session_id, None)
        if state:
            self._sessions.close(cast(SessionState, state)['internal'])

    async def close_endpoint(self, body: Empty, request: Request):
        await self.close_session(request.session.get(SESSION_ID_KEY))
        return {'closed': True}


if __name__ == '__main__':
    QudGymServer.run_webserver()
