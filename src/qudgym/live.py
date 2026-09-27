"""Live Caves of Qud over the mod's websocket RPC, and the operating model.

Two seams, deliberately separate. LiveBackend is the environment: it speaks
the same Backend protocol the mock implements, so QudEnv, SessionRecorder and
the action guard are unchanged between a mock episode and a real one.
OpenCodeModel is the operating model, and it is the only thing that knows about
opencode.

Two rules shape this module.

Snapshot, restore and state hash stay disabled. There is no evidence-backed
deterministic replay for live Qud, so those raise rather than pretending. A
save file is not a snapshot.

A mutation that may have committed is never resent. If a step is sent and the
reply is lost, the outcome is unknown, so this raises TransportUncertain with
replayable=False and the caller must reconcile. Resending under a fresh request
id could apply the action twice, and request ids are deduplicated per game
process, not per connection, so a retry would not even be recognised as a
duplicate.
"""
from __future__ import annotations

import ipaddress
import json
import pathlib
import secrets
import time
import urllib.parse
from typing import Any

from .errors import QudGymError, TransportUncertain
from .models import Capabilities, Observation, Transition

MAX_MESSAGE_BYTES = 1_048_576
_MUTATING = frozenset({"reset", "step", "restore", "release", "snapshot"})


class LiveBackend:
    """Synchronous Backend over one websocket to the in-game mod."""

    def __init__(self, url: str, *, token: str, timeout: float = 180.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise ValueError("Use a ws:// endpoint, e.g. ws://127.0.0.1:8765/rpc")
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError("Only a literal loopback address is supported")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Credentials and query strings are not accepted in the URL")
        if parsed.path != "/rpc":
            raise ValueError("Only the /rpc endpoint is served by the mod")
        if len(token) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Use an ASCII bearer token of at least 32 characters")
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be finite and positive")
        self.url, self.token, self.timeout = url, token, timeout
        self._closed = False
        self._counter = 0
        # Request ids are deduplicated in a dictionary that lives for the whole
        # game process, so ids must be unique per process, not per connection.
        self._prefix = secrets.token_hex(8)
        self._caps: Capabilities | None = None
        self._ws: Any = None

    # -- transport ---------------------------------------------------------

    def _connect(self) -> None:
        if self._ws is not None:
            return
        from websockets.sync.client import connect

        try:
            self._ws = connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self.token}"},
                user_agent_header="qudgym",
                open_timeout=20,
                # The sync client sends no Origin header, which is what the mod
                # requires: it rejects any request carrying one.
                max_size=MAX_MESSAGE_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a transport failure
            raise QudGymError("unavailable", f"Cannot reach the game: {exc}") from exc

    def _call(self, op: str, **fields: Any) -> dict:
        if self._closed:
            raise QudGymError("closed", "Client has been closed")
        self._connect()
        self._counter += 1
        request_id = f"{self._prefix}-{self._counter}"
        payload = json.dumps({"request_id": request_id, "op": op, **fields})
        mutating = op in _MUTATING
        try:
            self._ws.send(payload)
            raw = self._ws.recv(timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            if mutating:
                # The action may already have been applied in the game. Report
                # the outcome as unknown and never resend it.
                raise TransportUncertain(
                    f"{op} was sent and the reply was lost: {exc}",
                    request_id=request_id,
                    replayable=False,
                ) from exc
            raise QudGymError("unavailable", f"{op} failed: {exc}") from exc

        message = json.loads(raw)
        if message.get("request_id") not in (None, request_id):
            raise QudGymError(
                "protocol_error",
                f"Reply is for request {message.get('request_id')!r}, not {request_id!r}",
            )
        if "error" in message and message["error"] is not None:
            error = message["error"]
            code = error.get("code", "error")
            if code == "stale_decision":
                raise QudGymError(code, error.get("message", "decision cursor is stale"))
            raise QudGymError(code, error.get("message", "request failed"))
        if "result" not in message:
            raise QudGymError("protocol_error", "Reply carried neither result nor error")
        return message["result"]

    # -- Backend protocol --------------------------------------------------

    def capabilities(self) -> Capabilities:
        if self._caps is None:
            self._caps = Capabilities.model_validate(self._call("hello"))
        return self._caps

    def reset(self, *, seed: int = 0, boundary_timeout: float = 600.0) -> Transition:
        """Claim the episode, waiting for the game's first decision boundary.

        The mod answers no_boundary when it has not published a boundary yet,
        which is the normal state for the first minute or two of a live game.
        Retrying on that specific code is safe and is not the forbidden case:
        the server has said the request did not commit, so there is no
        ambiguity to reconcile. An ambiguous outcome still raises
        TransportUncertain and is never retried.
        """
        deadline = time.monotonic() + boundary_timeout
        while True:
            try:
                return Transition.model_validate(self._call("reset", seed=seed))
            except QudGymError as exc:
                if exc.code != "no_boundary" or time.monotonic() >= deadline:
                    raise
                time.sleep(2.0)

    def observe(self) -> Observation:
        return Observation.model_validate(self._call("observe"))

    def step(self, action_id: str, *, decision_id: str) -> Transition:
        return Transition.model_validate(
            self._call("step", decision_id=decision_id, action_id=action_id)
        )

    def snapshot(self):
        raise QudGymError(
            "unsupported",
            "Live Qud has no evidence-backed deterministic replay, so snapshot is disabled",
        )

    def restore(self, handle: str):
        raise QudGymError("unsupported", "restore is disabled for live Qud")

    def release(self, handle: str) -> None:
        raise QudGymError("unsupported", "release is disabled for live Qud")

    def state_hash(self):
        raise QudGymError("unsupported", "full_state_hash is disabled for live Qud")

    def close(self) -> None:
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing must not mask a real error
                pass


def discover_opencode_service() -> tuple[str, str]:
    """Find the running opencode service URL and its local credential.

    Both are discovered rather than configured. The port is chosen per service
    start, so a hardcoded one goes stale, and the credential lives in the
    service registration file. Returns (base_url, basic_auth_header_value) with
    the header value already base64 encoded, ready to use.

    The credential is a local loopback service password. It is read here and
    never logged, recorded into a trajectory, or written anywhere in the repo.
    """
    import base64
    import subprocess

    registration = pathlib.Path.home() / ".config" / "opencode" / "service.json"
    if not registration.is_file():
        raise QudGymError(
            "unavailable",
            "No opencode service registration; start one with `opencode service`",
        )
    password = json.loads(registration.read_text(encoding="utf-8")).get("password")
    if not isinstance(password, str) or not password:
        raise QudGymError("unavailable", "Service registration carries no password")

    try:
        out = subprocess.run(
            ["opencode", "service", "status"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise QudGymError("unavailable", f"Cannot query the opencode service: {exc}") from exc
    urls = [line.strip() for line in out.splitlines() if line.strip().startswith("http")]
    if not urls:
        raise QudGymError("unavailable", f"`opencode service status` reported no URL: {out[:200]!r}")

    header = base64.b64encode(f"opencode:{password}".encode()).decode()
    return urls[0], header


class OpenCodeModel:
    """The operating model, reached over opencode's own HTTP API.

    Uses POST /api/session/{id}/generate rather than the agent prompt route.
    That endpoint returns a plain completion with no agent loop, no tool
    dispatch and no permission round trip, which is the right shape for a game
    loop: every decision needs one completion, not a harness invocation.

    One session is created and reused for the whole episode, so opencode keeps
    its prompt cache across turns. Reusing the session id is also what the
    provider requires: the one-shot route refuses a request that arrives
    without a session header.
    """

    def __init__(self, base_url: str, *, auth: str, session_id: str, model: str,
                 provider: str, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.session_id = session_id
        self.model_id, self.provider_id = model, provider
        self.timeout = timeout
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._created = False

    def _post(self, path: str, body: dict) -> dict:
        try:
            return self._post_once(path, body)
        except (TimeoutError, OSError) as exc:
            # A slow completion is not a refusal. The game is turn based and
            # can wait, so this is reported as its own outcome rather than
            # folded into model_error, which would read as "the model said no".
            raise QudGymError(
                "model_timeout",
                f"opencode did not answer within {self.timeout:.0f}s",
            ) from exc

    def _post_once(self, path: str, body: dict) -> dict:
        import http.client

        parsed = urllib.parse.urlsplit(self.base_url)
        connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port or 80, timeout=self.timeout
        )
        try:
            raw = json.dumps(body).encode()
            connection.request(
                "POST",
                path,
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Basic {self.auth}",
                    # opencode routes and caches on this header; the provider
                    # rejects a request that arrives without it.
                    "x-opencode-session": self.session_id,
                },
            )
            response = connection.getresponse()
            payload = response.read()
        finally:
            connection.close()
        if response.status >= 400:
            detail = payload[:200].decode(errors="replace")
            raise QudGymError("model_error", f"opencode returned {response.status}: {detail}")
        return json.loads(payload)

    def _ensure_session(self) -> None:
        """Create the episode's session once, and adopt the id opencode issues.

        The id is not ours to choose: /generate validates it against a real
        session, and a made-up one is rejected as invalid. Adopting the issued
        id is also what makes the caching work the provider expects, because
        every turn then lands in the same session rather than a fresh
        conversation that would miss the prefix cache.
        """
        if self._created:
            return
        created = self._post("/api/session", {"title": "qudgym episode"})
        session_id = ((created.get("data") or {}).get("id")) or created.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise QudGymError("model_error", "opencode created a session with no id")
        self.session_id = session_id
        self._created = True

    def complete(self, system: str, prompt: str) -> str:
        """One completion, with the system instruction kept separate."""
        self._ensure_session()
        body = {
            "model": {"id": self.model_id, "providerID": self.provider_id,
                      "modelID": self.model_id},
            "system": system,
            "prompt": prompt,
        }
        result = self._post(f"/api/session/{self.session_id}/generate", body)
        data = result.get("data") or {}
        self.calls += 1
        self.prompt_tokens += int((result.get("usage") or {}).get("input", 0) or 0)
        self.completion_tokens += int((result.get("usage") or {}).get("output", 0) or 0)
        return str(data.get("text", ""))
