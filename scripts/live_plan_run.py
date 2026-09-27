"""Drive one live Qud episode with a plan, and report what the plan did.

A run like this tests the plan language, so this submits a program and then
reports the game's own account of it rather than narrating from the client: the
mod's log line for every decision boundary and answer, and plan_status for how
the program ended.

The control file the game writes names a websocket endpoint, and the mod serves
exactly that -- `session (webSocket: WebSocket)`, not an HTTP route. The envelope
is flat: request_id and op sit at the top level beside the op's own fields,
which is what the mod's dispatcher looks for.

The log is truncated first. It is append-mode, so it still holds the previous
run, and a report that quotes the previous run's conversation as this one's is
worse than no report.

Usage:  python scripts/live_plan_run.py <<'EOF'
goto watervine farmer
talk:watervine farmer
options 4, avail
EOF
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import secrets
import sys

import websockets

SUPPORT = pathlib.Path("~/Library/Application Support/com.FreeholdGames.CavesOfQud").expanduser()
CONTROL = SUPPORT / "QudGym-control.txt"
LOG = SUPPORT / "QudGym-diagnostic.txt"
PRESET = SUPPORT / "QudGym-preset.txt"

MAX_MESSAGE_BYTES = 1_048_576
INTERESTING = (
    "conversation popup",
    "option:",
    "plan answers",
    "conversation select",
    "menu '",
    "not on offer",
    "unavailable",
    "MODERROR",
)


def read_control() -> tuple[str, str]:
    """The game writes its own endpoint and token; both change per boot."""
    if not CONTROL.is_file():
        raise SystemExit("no control file; the game has not finished booting")
    lines = CONTROL.read_text().split("\n")
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        raise SystemExit("control file is incomplete")
    return lines[0].strip(), lines[1].strip()


class Rpc:
    """One websocket, one request at a time, in order.

    The mod dispatches each message on a pool thread, so replies could in
    principle arrive out of order. They are awaited in order regardless: a
    mutating op must not be overtaken, and a plan run is a sequence.
    """

    def __init__(self, socket: "websockets.ClientConnection"):
        self._socket = socket

    async def call(self, op: str, timeout: float = 300, **params) -> dict:
        # A fresh request id every call. The mod caches by id and answers a repeat
        # from that cache, so reusing one would look like a reply to an op that
        # was never sent.
        payload = {
            "protocol_version": "0.1",
            "request_id": secrets.token_hex(16),
            "op": op,
            **params,
        }
        await self._socket.send(json.dumps(payload))
        raw = await asyncio.wait_for(self._socket.recv(), timeout=timeout)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise SystemExit("reply exceeded the 1 MiB cap")
        reply = json.loads(raw)
        if "error" in reply:
            return {"_error": reply["error"]}
        return reply.get("result", reply)


def unwrap(reply: dict) -> dict:
    return reply.get("result", reply) if "_error" not in reply else {"_error": reply["_error"]}


async def run(program: str) -> None:
    url, token = read_control()
    print(f"control {url}")
    # Truncate before anything else: the log is append-mode, and quoting the
    # previous run's conversation as this one's is worse than quoting nothing.
    LOG.write_text("")

    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=MAX_MESSAGE_BYTES
    ) as socket:
        rpc = Rpc(socket)
        print("hello:", json.dumps(unwrap(await rpc.call("hello")))[:200])

        preset = PRESET.read_text().strip() if PRESET.is_file() else "artifex"
        print(f"reset (preset {preset}):", json.dumps(unwrap(await rpc.call("reset", seed=0)))[:300])

        print("plan:", json.dumps(unwrap(await rpc.call("plan", program=program))))

        # The plan runs on the game turn thread, one action per turn. Give it
        # room, then report how it ended rather than assuming that it did.
        for _ in range(32):
            await asyncio.sleep(15)
            status = unwrap(await rpc.call("plan_status", timeout=30))
            if "_error" in status:
                print(f"plan_status unavailable: {status}")
                break
            print(
                f"  running={status.get('running')} steps={status.get('steps_taken')} "
                f"trace={status.get('trace')!r} note={status.get('note')!r}"
            )
            if not status.get("running"):
                break

        observation = unwrap(await rpc.call("observe", timeout=120)).get("observation", {})

    print("\n=== observation ===")
    print("phase:", observation.get("phase"), "turn:", observation.get("turn"))
    print("player:", json.dumps(observation.get("player", {})))
    print("messages:")
    for line in (observation.get("messages") or [])[-12:]:
        print("  ", line)
    print("quests:")
    for line in observation.get("quests") or []:
        print("  ", line)
    prompt = observation.get("prompt")
    if prompt:
        unavailable = prompt.get("unavailable") or []
        print("prompt:", prompt.get("text", "")[:160])
        for i, option in enumerate(prompt.get("options") or [], start=1):
            mark = "   [the game will not accept this]" if i in unavailable else ""
            print(f"   {i}. {option}{mark}")
        print("   allow_cancel:", prompt.get("allow_cancel"))
    else:
        print("prompt: none")

    print("\n=== the mod's own account of this run ===")
    for line in LOG.read_text(errors="replace").split("\n"):
        if any(key in line for key in INTERESTING):
            print("  ", line.split(" thread=")[0])


def main() -> None:
    program = sys.stdin.read().strip()
    if not program:
        raise SystemExit("no program on stdin")
    asyncio.run(run(program))


if __name__ == "__main__":
    main()
