"""Play a plan program against the live game, and narrate it as it goes.

This is the tool the workflow was missing. Before it, every question about the
plan language cost a bespoke script and a game boot; after it, a question is a
line of text:

    printf 'goto mehmet\\ntalk:mehmet\\noptions 3, 1, 2, enter, esc\\n' | qud play

The program comes from stdin, the game's own options and answers are printed as
they happen, and the episode ends by being reported -- not by timing out. Nothing
here decides anything: it prints what the game published, what the plan pressed,
and what the game did with it.

The transport is the websocket the mod serves. Its envelope is flat -- request_id
and op at the top level beside the op's own fields -- and `observe` returns the
observation bare while `reset` wraps it under "observation", so both shapes are
accepted here rather than in each caller.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import secrets
import sys
from typing import Any

import websockets

SUPPORT = pathlib.Path("~/Library/Application Support/com.FreeholdGames.CavesOfQud").expanduser()
CONTROL = SUPPORT / "QudGym-control.txt"
PRESET = SUPPORT / "QudGym-preset.txt"
LOG = SUPPORT / "QudGym-diagnostic.txt"

MAX_MESSAGE_BYTES = 1_048_576


class Game:
    """One websocket, one request at a time, in order.

    The mod dispatches each message on a pool thread, so replies could in
    principle arrive out of order. They are awaited in order regardless: a
    mutating op must not be overtaken, and playing a program is a sequence.
    """

    def __init__(self, socket: "websockets.ClientConnection"):
        self._socket = socket

    async def call(self, op: str, timeout: float = 300, **params: Any) -> dict:
        await self._socket.send(
            json.dumps(
                {
                    "protocol_version": "0.1",
                    # A fresh id every call: the mod caches by id and answers a
                    # repeat from that cache, so reusing one would look like a
                    # reply to an op that was never sent.
                    "request_id": secrets.token_hex(16),
                    "op": op,
                    **params,
                }
            )
        )
        raw = await asyncio.wait_for(self._socket.recv(), timeout=timeout)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise RuntimeError("reply exceeded the 1 MiB cap")
        reply = json.loads(raw)
        if "error" in reply:
            return {"_error": reply["error"]}
        return reply.get("result", {})


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
    if not CONTROL.is_file():
        print("no control file: the game has not finished booting", file=sys.stderr)
        return 1
    url, token = (CONTROL.read_text().split("\n") + ["", ""])[:2]
    if watch:
        # The log is append-mode, so without this a report quotes the previous
        # run's conversation as this one's.
        LOG.write_text("")

    async with websockets.connect(
        url.strip(),
        additional_headers={"Authorization": f"Bearer {token.strip()}"},
        max_size=MAX_MESSAGE_BYTES,
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

        deadline = asyncio.get_event_loop().time() + seconds
        status: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            status = await game.call("plan_status", timeout=30)
            if "_error" in status:
                print("plan_status unavailable:", json.dumps(status["_error"]))
                break
            if not status.get("running"):
                break
            print(f"  ... running, {status.get('steps_taken')} step(s), "
                  f"trace {status.get('trace')!r}")

        print(f"\nprogram ended: running={status.get('running')} "
              f"steps={status.get('steps_taken')}")
        if status.get("note"):
            print(f"  note:  {status['note']}")
        if status.get("trace"):
            print(f"  trace: {status['trace']}")

        print("\n=== the game's last published state ===")
        final = await game.call("observe", timeout=60)
        if "_error" in final:
            print("  ", json.dumps(final["_error"]))
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

    if watch:
        print("\n=== the mod's account, from the log ===")
        for line in LOG.read_text(errors="replace").split("\n"):
            if any(k in line for k in (
                "conversation popup", "option:", "plan answers", "conversation select",
                "menu '", "not on offer", "reembark", "MODERROR",
            )):
                print("  ", line.split(" thread=")[0])
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=300.0,
                        help="give the program this long before reporting")
    parser.add_argument("--watch", action="store_true",
                        help="truncate the mod's log first, then print its account of the run")
    args = parser.parse_args()
    program = sys.stdin.read().strip()
    if not program:
        print("no program on stdin", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(play(program, seconds=args.seconds, watch=args.watch)))


if __name__ == "__main__":
    main()
