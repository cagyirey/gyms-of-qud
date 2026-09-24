"""Authenticated localhost reference service. Not a live Qud mod."""
from __future__ import annotations

import asyncio
import hmac
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from .client import MAX_MESSAGE_BYTES
from .models import RpcRequest
from .rpc import RpcService


def create_app(service: RpcService, *, token: str) -> FastAPI:
    if len(token) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Use an ASCII bearer token of at least 32 characters")
    app = FastAPI(title="QudGym reference controller", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.post("/rpc")
    async def rpc(request: Request):
        # This is not a browser API. In particular, do not enable wildcard CORS.
        if request.headers.get("origin") is not None:
            raise HTTPException(403, "Browser-origin requests are not supported")
        supplied = request.headers.get("authorization", "").encode("utf-8")
        expected = f"Bearer {token}".encode("ascii")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "Authentication required")
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > MAX_MESSAGE_BYTES:
                raise HTTPException(413, "Request too large")
        try:
            parsed = RpcRequest.model_validate_json(bytes(data))
        except ValidationError:
            # Avoid echoing request contents, which may contain sensitive payloads.
            raise HTTPException(422, "Invalid QudGym protocol envelope") from None
        response = await asyncio.to_thread(service.handle, parsed)
        return response.model_dump(mode="json")

    return app
