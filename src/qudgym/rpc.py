"""Serialized, versioned controller protocol with bounded duplicate suppression."""
from __future__ import annotations

from collections import OrderedDict
from threading import RLock

from .backend import Backend
from .errors import QudGymError
from .models import RpcError, RpcRequest, RpcResponse


class RpcService:
    def __init__(self, backend: Backend, *, cache_size: int = 256):
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self.backend = backend
        self.cache_size = cache_size
        self._lock = RLock()
        self._cache: OrderedDict[str, tuple[str, RpcResponse]] = OrderedDict()
        self._faulted = False

    def handle(self, request: RpcRequest) -> RpcResponse:
        fingerprint = request.operation.model_dump_json()
        with self._lock:
            cached = self._cache.get(request.request_id)
            if cached:
                old_fingerprint, response = cached
                if old_fingerprint != fingerprint:
                    return self._error(request, "request_id_conflict", "Request ID reused with different content")
                self._cache.move_to_end(request.request_id)
                return response.model_copy(deep=True)
            try:
                if self._faulted and request.operation.op not in ("hello", "reset"):
                    raise QudGymError("worker_faulted", "Reset this worker after an internal failure")
                result = self._dispatch(request)
                response = RpcResponse(request_id=request.request_id, result=result)
            except QudGymError as exc:
                response = self._error(request, exc.code, str(exc))
            except Exception:
                # A backend bug may have partially mutated state. Never turn it into a loss.
                self._faulted = True
                response = self._error(request, "internal_error", "Backend failed; worker requires reset")
            self._cache[request.request_id] = (fingerprint, response.model_copy(deep=True))
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            return response

    def _dispatch(self, request: RpcRequest):
        op = request.operation
        if op.op == "hello":
            value = self.backend.capabilities()
        elif op.op == "reset":
            value = self.backend.reset(seed=op.seed)
            self._faulted = False
        elif op.op == "observe":
            value = self.backend.observe()
        elif op.op == "step":
            value = self.backend.step(op.action_id, decision_id=op.decision_id)
        elif op.op == "snapshot":
            value = self.backend.snapshot()
        elif op.op == "restore":
            value = self.backend.restore(op.handle)
        elif op.op == "release":
            self.backend.release(op.handle)
            return {"released": True}
        elif op.op == "state_hash":
            value = self.backend.state_hash()
        else:
            raise QudGymError("unsupported", "Unknown operation")
        return value.model_dump(mode="json")

    @staticmethod
    def _error(request, code, message):
        return RpcResponse(request_id=request.request_id, error=RpcError(code=code, message=message))
