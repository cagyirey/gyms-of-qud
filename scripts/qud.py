"""Run a plan against live Qud and report the server's outcome.

    printf 'goto mehmet\\ntalk:mehmet\\noptions 3, 1, 2\\n' | python scripts/qud.py --watch

Exit 0 means the server reported a finished plan and returned a final observation,
not that a quest was completed. Exit 1 means failure or an unclassified outcome;
124 means the polling budget expired. Closing the connection does not prove that
the current server implementation cancelled an in-flight game action or plan.
"""
from __future__ import annotations

import asyncio
import json
import math
import pathlib
import secrets
import sys
from typing import Any

import websockets
from websockets.exceptions import WebSocketException

SUPPORT = pathlib.Path("~/Library/Application Support/com.FreeholdGames.CavesOfQud").expanduser()
CONTROL = SUPPORT / "QudGym-control.txt"
PRESET = SUPPORT / "QudGym-preset.txt"
LOG = SUPPORT / "QudGym-diagnostic.txt"
MAX_MESSAGE_BYTES = 1_048_576


class Game:
    """Sequential RPC; a lost or uncorrelated reply makes this connection unusable."""

    def __init__(self, socket: "websockets.ClientConnection"):
        self._socket = socket
        self._usable = True

    async def call(self, op: str, timeout: float = 300, **params: Any) -> dict:
        if not self._usable:
            raise RuntimeError("previous request outcome is unresolved; do not resend on this connection")
        request_id = secrets.token_hex(16)
        payload = json.dumps({**params, "protocol_version": "0.1", "request_id": request_id, "op": op})
        try:
            async with asyncio.timeout(timeout):
                await self._socket.send(payload)
                raw = await self._socket.recv()
            size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            if size > MAX_MESSAGE_BYTES:
                raise RuntimeError("reply exceeded the 1 MiB cap")
            reply = json.loads(raw)
            if not isinstance(reply, dict) or reply.get("protocol_version") != "0.1":
                raise RuntimeError("invalid RPC response envelope")
            if reply.get("request_id") != request_id:
                raise RuntimeError("reply does not identify the outstanding request")
            if ("result" in reply) == ("error" in reply):
                raise RuntimeError("reply must carry exactly one result or error")
            if "error" in reply:
                error = reply["error"]
                if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                    raise RuntimeError("invalid RPC error")
                return {"_error": error}
            if not isinstance(reply["result"], dict):
                raise RuntimeError("expected an object result")
            return reply["result"]
        except (Exception, asyncio.CancelledError):
            # No automatic retry: the operation may already have affected the game.
            # A late reply must not be consumed as the answer to a different request.
            self._usable = False
            raise


def observation_of(result: dict) -> dict:
    """reset wraps the observation; observe returns it bare."""
    return result.get("observation", result)


def describe_prompt(prompt: dict) -> None:
    unavailable = prompt.get("unavailable") or []
    print(f"  prompt: {len(prompt.get('options') or [])} option(s), "
          f"refused {unavailable or 'none'}, allow_cancel={prompt.get('allow_cancel')}")
    for i, option in enumerate(prompt.get("options") or [], start=1):
        mark = "   [the game will not accept this]" if i in unavailable else ""
        print(f"    {i}. {option}{mark}")


async def play(program: str, *, seconds: float, watch: bool) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("--seconds must be finite and positive")
    if not CONTROL.is_file():
        print("no control file: the game has not finished booting", file=sys.stderr)
        return 1
    url, token = (CONTROL.read_text().split("\n") + ["", ""])[:2]
    # Watch only new diagnostic bytes, without modifying the game's log.
    mark = LOG.stat().st_size if watch and LOG.is_file() else 0
    exit_code = 1

    async with websockets.connect(
        url.strip(),
        additional_headers={"Authorization": f"Bearer {token.strip()}"},
        max_size=MAX_MESSAGE_BYTES,
        close_timeout=3,
    ) as socket:
        game = Game(socket)
        preset = PRESET.read_text().strip() if PRESET.is_file() else "artifex"
        print(f"reset (preset {preset})")
        started = observation_of(await game.call("reset", seed=0))
        if "_error" in started:
            print("reset failed:", json.dumps(started["_error"]))
            return 1
        print(f"  episode {started.get('episode_id')}  turn {started.get('turn')}  "
              f"player {json.dumps(started.get('player', {}))}")
        submitted = await game.call("plan", program=program)
        if "_error" in submitted:
            print("the program did not parse:", json.dumps(submitted["_error"]))
            return 1
        print(f"  program: {submitted.get('plan_actions')}\n")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        status: dict = {}
        previous = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                print("plan polling deadline expired; server cancellation is not confirmed", file=sys.stderr)
                return 124
            try:
                status = await game.call("plan_status", timeout=min(30, remaining))
            except TimeoutError:
                print("plan status timed out; request outcome and server cancellation are unconfirmed",
                      file=sys.stderr)
                return 124
            if "_error" in status:
                print("plan_status unavailable:", json.dumps(status["_error"]))
                break
            if type(status.get("running")) is not bool:
                print("plan_status did not report whether a plan is running", file=sys.stderr)
                break
            if not status["running"]:
                exit_code = 0 if status.get("trace") == "finished" else 1
                break
            line = f"  {status.get('steps_taken')} step(s), trace {status.get('trace')!r}"
            if line != previous:
                print(line)
                previous = line
            await asyncio.sleep(min(1, max(0, deadline - loop.time())))

        print(f"\nserver plan status: running={status.get('running')} steps={status.get('steps_taken')}")
        if status.get("note"):
            print(f"  note:  {status['note']}")
        if status.get("trace"):
            print(f"  trace: {status['trace']}")
        print("\n=== the game's last published state ===")
        final = await game.call("observe", timeout=60)
        if "_error" in final:
            print("  ", json.dumps(final["_error"]))
            exit_code = 1
        else:
            seen = observation_of(final)
            print(f"  phase {seen.get('phase')}  turn {seen.get('turn')}  "
                  f"player {json.dumps(seen.get('player', {}))}")
            for line in (seen.get("messages") or [])[-10:]:
                print("   ", line)
            for line in seen.get("quests") or []:
                print("  quest:", line)
            if seen.get("prompt"):
                describe_prompt(seen["prompt"])

    if watch and LOG.is_file():
        print("\n=== new mod diagnostic lines ===")
        with LOG.open("rb") as handle:
            handle.seek(mark if LOG.stat().st_size >= mark else 0)
            fresh = handle.read().decode("utf-8", "replace")
        for line in fresh.splitlines():
            body = line.split(" thread=")[0]
            if any(k in body for k in ("conversation popup", "menu '", "plan answers",
                                      "not on offer", "reembark", "MODERROR")):
                print("  ", body)
    return exit_code


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=300.0,
                        help="plan-status polling budget after submission (seconds)")
    parser.add_argument("--watch", action="store_true",
                        help="print new diagnostic lines without truncating the log")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    program = sys.stdin.read().strip()
    if not program:
        parser.error("no program on stdin")
    try:
        code = asyncio.run(play(program, seconds=args.seconds, watch=args.watch))
    except KeyboardInterrupt:
        code = 130
    except (OSError, ValueError, RuntimeError, WebSocketException) as exc:
        print(f"plan run failed: {exc}; no request was retried", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
