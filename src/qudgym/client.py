"""Loopback clients: HTTP for the mock server, WebSocket for the live game."""
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
_MUTATING = frozenset({"reset", "step", "restore", "release", "snapshot"})


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
        self._inflight: tuple[str, Reset | Step | Restore | Snapshot | Release] | None = None
        self._last_request_id: str | None = None

    def _remember(self, request_id, operation):
        if operation.op in _MUTATING:
            self._inflight = (request_id, operation)

    def _call(self, operation, *, request_id: str | None = None):
        if self._closed:
            raise QudGymError("closed", "Client has been closed")
        if operation.op in _MUTATING and self._inflight is not None and request_id is None:
            raise QudGymError("reconcile_required",
                              "Replay or reconcile the uncertain request before a new mutation")
        if request_id is None:
            request_id = secrets.token_hex(16)
        self._last_request_id = request_id
        request = RpcRequest(request_id=request_id, operation=operation)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            raw = request.model_dump_json().encode()
            connection.request("POST", "/rpc", body=raw,
                               headers={"Authorization": f"Bearer {self._token}",
                                        "Content-Type": "application/json"})
            response = connection.getresponse()
            payload = response.read(MAX_MESSAGE_BYTES + 1)
            if len(payload) > MAX_MESSAGE_BYTES:
                self._remember(request_id, operation)
                raise TransportUncertain("Oversized reply; reconcile worker state", request_id=request_id)
            if response.status != 200:
                # create_app rejects these before RpcService.handle, so nothing committed.
                raise TransportUncertain(f"HTTP {response.status}; no mutation was retried",
                                         request_id=request_id, replayable=False)
            envelope = RpcResponse.model_validate_json(payload)
            if envelope.request_id != request.request_id:
                self._remember(request_id, operation)
                raise TransportUncertain("Reply correlation failed; reconcile worker state",
                                         request_id=request_id)
        except TransportUncertain:
            raise
        except (OSError, http.client.HTTPException, ValidationError) as exc:
            self._remember(request_id, operation)
            raise TransportUncertain(request_id=request_id) from exc
        finally:
            connection.close()
        if self._inflight and self._inflight[0] == request_id:
            self._inflight = None
        if envelope.error:
            raise QudGymError(envelope.error.code, envelope.error.message)
        return envelope.result

    def _decode(self, operation, model, *, request_id: str | None = None):
        try:
            raw = (self._call(operation) if request_id is None
                   else self._call(operation, request_id=request_id))
            return model.model_validate(raw)
        except ValidationError as exc:
            remembered = request_id or self._last_request_id
            if remembered is not None:
                self._remember(remembered, operation)
            raise TransportUncertain("Invalid result schema; reconcile worker state",
                                     request_id=remembered) from exc

    def replay(self):
        if self._inflight is None:
            raise QudGymError("nothing_to_replay", "No uncertain mutation to replay")
        request_id, operation = self._inflight
        return self._decode(operation, operation.result_model, request_id=request_id)

    def abandon_uncertain(self):
        # The caller has observed the server cursor and will not replay the lost reply.
        self._inflight = None

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

    def release(self, handle: str) -> None:
        self._decode(Release(handle=handle), Release.result_model)

    def state_hash(self):
        return self._decode(HashState(), StateHash)

    def close(self):
        # Closing the client does not send a command that terminates somebody else's game.
        self._closed = True


class WebSocketBackend:
    """One long-lived loopback socket. The live game speaks the same envelopes as HTTP."""

    def __init__(self, url: str, *, token: str, timeout: float = 10):
        parsed = urlsplit(url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError as exc:
            raise ValueError("Use a literal loopback address, e.g. ws://127.0.0.1:8765/rpc") from exc
        if (parsed.scheme != "ws" or not local or not parsed.port or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path != "/rpc"):
            raise ValueError("Only a ws:// /rpc endpoint on a literal loopback address is supported")
        if len(token) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Use an ASCII bearer token of at least 32 characters")
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be finite and positive")
        from websockets.sync.client import connect
        self.timeout = timeout
        self._token = token
        self._closed = False
        self._inflight: tuple[str, Reset | Step | Restore | Snapshot | Release] | None = None
        self._last_request_id: str | None = None
        try:
            self._socket = connect(
                url,
                additional_headers={"Authorization": f"Bearer {token}"},
                origin=None,
                proxy=None,
                open_timeout=timeout,
                max_size=MAX_MESSAGE_BYTES,
            )
        except OSError as exc:
            raise QudGymError("unavailable", "Live control is not accepting connections") from exc

    def _remember(self, request_id, operation):
        if operation.op in _MUTATING:
            self._inflight = (request_id, operation)

    def _call(self, operation, *, request_id: str | None = None):
        if self._closed:
            raise QudGymError("closed", "Client has been closed")
        if operation.op in _MUTATING and self._inflight is not None and request_id is None:
            raise QudGymError("reconcile_required",
                              "Replay or reconcile the uncertain request before a new mutation")
        if request_id is None:
            request_id = secrets.token_hex(16)
        self._last_request_id = request_id
        request = RpcRequest(request_id=request_id, operation=operation)
        from websockets.exceptions import WebSocketException
        try:
            self._socket.send(request.model_dump_json())
            payload = self._socket.recv(timeout=self.timeout)
        except TimeoutError as exc:
            self._remember(request_id, operation)
            raise TransportUncertain("Timed out waiting for the game thread", request_id=request_id) from exc
        except (OSError, WebSocketException) as exc:
            self._remember(request_id, operation)
            raise TransportUncertain(request_id=request_id) from exc
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
            self._remember(request_id, operation)
            raise TransportUncertain("Oversized reply; reconcile worker state", request_id=request_id)
        try:
            envelope = RpcResponse.model_validate_json(payload)
        except ValidationError as exc:
            self._remember(request_id, operation)
            raise TransportUncertain("Invalid result schema; reconcile worker state",
                                     request_id=request_id) from exc
        if envelope.request_id != request.request_id:
            self._remember(request_id, operation)
            raise TransportUncertain("Reply correlation failed; reconcile worker state",
                                     request_id=request_id)
        if self._inflight and self._inflight[0] == request_id:
            self._inflight = None
        if envelope.error:
            raise QudGymError(envelope.error.code, envelope.error.message)
        return envelope.result

    def _decode(self, operation, model, *, request_id: str | None = None):
        try:
            raw = (self._call(operation) if request_id is None
                   else self._call(operation, request_id=request_id))
            return model.model_validate(raw)
        except ValidationError as exc:
            remembered = request_id or self._last_request_id
            if remembered is not None:
                self._remember(remembered, operation)
            raise TransportUncertain("Invalid result schema; reconcile worker state",
                                     request_id=remembered) from exc

    def replay(self):
        if self._inflight is None:
            raise QudGymError("nothing_to_replay", "No uncertain mutation to replay")
        request_id, operation = self._inflight
        return self._decode(operation, operation.result_model, request_id=request_id)

    def abandon_uncertain(self):
        self._inflight = None

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

    def release(self, handle: str) -> None:
        self._decode(Release(handle=handle), Release.result_model)

    def state_hash(self):
        return self._decode(HashState(), StateHash)

    def close(self):
        self._closed = True
        socket = getattr(self, "_socket", None)
        if socket is not None:
            socket.close()
