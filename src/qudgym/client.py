"""Loopback HTTP client: no proxy, redirect following, or implicit mutation retries."""
from __future__ import annotations

import http.client
import ipaddress
import secrets
from urllib.parse import urlsplit

from pydantic import ValidationError

from .errors import QudGymError, TransportUncertain
from .models import (Capabilities, HashState, Hello, Observe, Observation, Release, Reset,
                     Restore, RpcRequest, RpcResponse, Snapshot, SnapshotHandle, StateHash,
                     Step, Transition)

MAX_MESSAGE_BYTES = 1_048_576


class HttpBackend:
    def __init__(self, url: str, *, token: str, timeout: float = 10):
        parsed = urlsplit(url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
            port = parsed.port or 80
        except ValueError as exc:
            raise ValueError("Use a literal loopback address, e.g. http://127.0.0.1:8765/rpc") from exc
        if (parsed.scheme != "http" or not local or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/rpc"):
            raise ValueError("Only an HTTP /rpc endpoint on a literal loopback address is supported")
        if len(token) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Use an ASCII bearer token of at least 32 characters")
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be finite and positive")
        self.host, self.port, self.timeout = parsed.hostname, port, timeout
        self._token = token
        self._closed = False

    def _call(self, operation):
        if self._closed:
            raise QudGymError("closed", "Client has been closed")
        request = RpcRequest(request_id=secrets.token_hex(16), operation=operation)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            raw = request.model_dump_json().encode()
            connection.request("POST", "/rpc", body=raw,
                               headers={"Authorization": f"Bearer {self._token}",
                                        "Content-Type": "application/json"})
            response = connection.getresponse()
            payload = response.read(MAX_MESSAGE_BYTES + 1)
            if len(payload) > MAX_MESSAGE_BYTES:
                raise TransportUncertain("Oversized reply; reconcile worker state")
            if response.status != 200:
                raise TransportUncertain(f"HTTP {response.status}; no mutation was retried")
            envelope = RpcResponse.model_validate_json(payload)
            if envelope.request_id != request.request_id:
                raise TransportUncertain("Reply correlation failed; reconcile worker state")
        except (OSError, http.client.HTTPException, ValidationError) as exc:
            raise TransportUncertain() from exc
        finally:
            connection.close()
        if envelope.error:
            raise QudGymError(envelope.error.code, envelope.error.message)
        return envelope.result

    def _decode(self, operation, model):
        try:
            return model.model_validate(self._call(operation))
        except ValidationError as exc:
            raise TransportUncertain("Invalid result schema; reconcile worker state") from exc

    def capabilities(self):
        return self._decode(Hello(), Capabilities)

    def reset(self, *, seed=0):
        return self._decode(Reset(seed=seed), Transition)

    def observe(self):
        return self._decode(Observe(), Observation)

    def step(self, action_id, *, decision_id):
        return self._decode(Step(action_id=action_id, decision_id=decision_id), Transition)

    def snapshot(self):
        return self._decode(Snapshot(), SnapshotHandle)

    def restore(self, handle):
        return self._decode(Restore(handle=handle), Transition)

    def release(self, handle):
        if self._call(Release(handle=handle)) != {"released": True}:
            raise TransportUncertain("Invalid release acknowledgement")

    def state_hash(self):
        return self._decode(HashState(), StateHash)

    def close(self):
        # Closing the client does not send a command that terminates somebody else's game.
        self._closed = True
