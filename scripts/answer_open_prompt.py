"""Answer whatever conversation is currently open, and report each prompt.

Separate from live_plan_run.py on purpose: that submits a program and then reads
how it ended, while this presses answers into a prompt that is already open. It
is the cheap way to see what a game publishes next -- a reboot is three minutes,
and the episode is still live until the conversation closes.

It answers the only option a prompt offers, and refuses to guess when there is
more than one. Nothing here decides anything: it reports the game's options and
takes the single one, which is what a "press space to continue" node is.

    python scripts/answer_open_prompt.py --times 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import secrets
import sys

import websockets

SUPPORT = pathlib.Path("~/Library/Application Support/com.FreeholdGames.CavesOfQud").expanduser()
CONTROL = SUPPORT / "QudGym-control.txt"
LOG = SUPPORT / "QudGym-diagnostic.txt"

MAX_MESSAGE_BYTES = 1_048_576


async def main(times: int, pause: float) -> None:
    if not CONTROL.is_file():
        raise SystemExit("no control file; the game has not finished booting")
    url, token = (CONTROL.read_text().split("\n") + ["", ""])[:2]

    async with websockets.connect(
        url.strip(),
        additional_headers={"Authorization": f"Bearer {token.strip()}"},
        max_size=MAX_MESSAGE_BYTES,
    ) as socket:

        async def call(op: str, timeout: float = 120, **params) -> dict:
            await socket.send(
                json.dumps(
                    {
                        "protocol_version": "0.1",
                        "request_id": secrets.token_hex(16),
                        "op": op,
                        **params,
                    }
                )
            )
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
            return json.loads(raw)

        for turn in range(1, times + 1):
            reply = await call("observe")
            if "error" in reply:
                print(f"observe: {reply['error']}")
                return
            # observe returns the observation bare; reset wraps it. Both are read
            # here, so either shape works.
            result = reply.get("result", {})
            observation = result.get("observation", result)
            if not observation:
                print("observe returned no observation")
                return
            prompt = observation.get("prompt")
            if not prompt:
                print(f"[{turn}] no prompt open; the conversation has closed")
                break
            options = prompt.get("options") or []
            unavailable = prompt.get("unavailable") or []
            print(f"[{turn}] {len(options)} option(s), refused: {unavailable or 'none'}")
            for i, option in enumerate(options, start=1):
                mark = "   [the game will not accept this]" if i in unavailable else ""
                print(f"      {i}. {option}{mark}")
            if len(options) != 1:
                print("      not a single-option node; stopping rather than guessing")
                break
            if 1 in unavailable:
                print("      the game will not accept it; stopping")
                break
            # answer:1 is the game's own index 0.
            answer = await call("answer", option=1)
            if "error" in answer:
                print(f"      answer refused: {answer['error']}")
                return
            print("      answered 1")
            await asyncio.sleep(pause)

    print("\n=== the mod's account of the answers ===")
    if LOG.is_file():
        for line in LOG.read_text(errors="replace").split("\n"):
            if any(k in line for k in ("conversation popup", "option:", "conversation select", "not on offer")):
                print("  ", line.split(" thread=")[0])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--times", type=int, default=8, help="how many prompts to answer")
    parser.add_argument("--pause", type=float, default=2.0, help="seconds between answers")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.times, args.pause))
    except KeyboardInterrupt:
        sys.exit(130)
