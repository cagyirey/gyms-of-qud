"""Bounded mock rollout sessions shared by the NeMo adapter and local contract tests.

This is a reference single-process manager, not a distributed game worker pool.
"""
from __future__ import annotations

import secrets
import time
from threading import RLock

from .env import QudEnv
from .errors import QudGymError
from .mock import MockBackend


class SessionManager:
    def __init__(self, *, max_sessions: int = 32, ttl_seconds: float = 1800):
        if max_sessions < 1 or not 0 < ttl_seconds < float("inf"):
            raise ValueError("Session limits must be positive and finite")
        self.max_sessions, self.ttl_seconds = max_sessions, ttl_seconds
        self._sessions: dict[str, tuple[QudEnv, float]] = {}
        self._lock = RLock()

    def _reap(self):
        now = time.monotonic()
        for key, (env, touched) in list(self._sessions.items()):
            if now - touched > self.ttl_seconds:
                env.close()
                del self._sessions[key]

    def seed(self, *, seed=0, max_decisions=128):
        if isinstance(max_decisions, bool) or not isinstance(max_decisions, int) or not 1 <= max_decisions <= 10000:
            raise QudGymError("invalid_limit", "max_decisions must be in [1, 10000]")
        with self._lock:
            self._reap()
            if len(self._sessions) >= self.max_sessions:
                raise QudGymError("capacity", "Close a rollout before allocating another")
            env = QudEnv(MockBackend(max_decisions=max_decisions, allow_oracle=False))
            result = env.reset(seed=seed)
            key = secrets.token_hex(24)
            self._sessions[key] = (env, time.monotonic())
            return key, result.observation

    def _get(self, key):
        self._reap()
        if key not in self._sessions:
            raise QudGymError("session_missing", "Rollout session is missing or expired")
        env, _ = self._sessions[key]
        self._sessions[key] = (env, time.monotonic())
        return env

    def observe(self, key):
        with self._lock:
            return self._get(key).backend.observe()

    def step(self, key, *, action_id, decision_id):
        with self._lock:
            env = self._get(key)
            current = env.current
            if current is None:
                raise QudGymError("reset_required", "Reset before stepping")
            if current.observation.decision_id != decision_id:
                raise QudGymError("stale_decision", "Decision cursor is stale")
            return env.step(action_id)

    def verify(self, key):
        with self._lock:
            env = self._get(key)
            result = env.current
            if result is None:
                raise QudGymError("session_missing", "No authoritative result")
            # Only server-side game outcome counts. Never trust an agent's claimed score.
            return {"reward": float(result.metrics.outcome == "success"),
                    "outcome": result.metrics.outcome,
                    "turns": result.metrics.turns_elapsed,
                    "decisions": result.metrics.decisions_elapsed,
                    "is_mock": True}

    def close(self, key):
        with self._lock:
            if key in self._sessions:
                env, _ = self._sessions.pop(key)
                env.close()

    def active_keys(self):
        with self._lock:
            self._reap()
            return frozenset(self._sessions)
