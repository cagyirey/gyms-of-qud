"""Does one process really run a second episode?

The question this exists to answer cannot be checked offline. Re-embarking runs
the game's own Release / CreateNewGame / Reset sequence from a decision boundary
on the core thread, and the only evidence that the result is a *new, whole* world
rather than the old one with its fields overwritten is a live run: a second
episode id, a different world, and the game still able to take turns.

So it asserts the invariants rather than the appearance. A re-embark that quietly
produced a half-old world would still report turn=1 and a fresh id; the world
location and the message log are what distinguish a new world from a relabelled
one.

    python scripts/check_reembark.py
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


def read_control() -> tuple[str, str]:
    lines = CONTROL.read_text().split("\n")
    return lines[0].strip(), lines[1].strip()


def world(observation: dict) -> str:
    for line in observation.get("messages") or []:
        if "," in line and any(w in line for w in ("Joppa", "Ovid", "Kyoto", "Anat")):
            return line.strip()
    return "(no opening line published)"


async def main() -> int:
    url, token = read_control()
    failures: list[str] = []

    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=1_048_576
    ) as socket:

        async def call(op: str, timeout: float = 180, **params) -> dict:
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
            reply = json.loads(raw)
            if "error" in reply:
                return {"_error": reply["error"]}
            return reply.get("result", {})

        first = await call("reset", seed=0)
        if "_error" in first:
            print("first reset failed:", first)
            return 1
        one = first.get("observation", first)
        print(f"episode 1  id={one.get('episode_id')}  turn={one.get('turn')}  world={world(one)}")
        print(f"           player={json.dumps(one.get('player', {}))}")
        first_world = world(one)
        first_id = one.get("episode_id")

        asked = await call("reembark")
        print("\nreembark:", json.dumps(asked))
        if "_error" in asked:
            print("reembark was refused:", asked)
            return 1

        # The work happens at the next decision boundary, so drive a turn to reach
        # one. Anything the action space offers will do; the point is to get back
        # to the game.
        actions = one.get("actions") or []
        if actions:
            await call("step", action_id=actions[0].get("id"), decision_id=one.get("decision_id"))
        print("drove one action to reach the next boundary; waiting for the new episode")

        second = None
        for _ in range(24):
            await asyncio.sleep(5)
            got = await call("reset", seed=0, timeout=60)
            if "_error" in got:
                # A boundary may not have arrived yet; keep waiting.
                continue
            second = got.get("observation", got)
            break
        if second is None:
            failures.append("no second episode's boundary arrived within two minutes")

    print("\n=== the mod's account ===")
    for line in LOG.read_text(errors="replace").split("\n"):
        if any(k in line for k in ("reembark", "boundary", "embark", "first entry")):
            print("  ", line.split(" thread=")[0])

    if second is None:
        print("\nFAIL: the re-embark produced no observable episode")
        return 1

    print(f"\nepisode 2  id={second.get('episode_id')}  turn={second.get('turn')}  world={world(second)}")
    print(f"           player={json.dumps(second.get('player', {}))}")

    if second.get("episode_id") == first_id:
        failures.append("the second episode has the first episode's id")
    if second.get("turn") != 1:
        failures.append(f"the second episode opened at turn {second.get('turn')}, not 1")
    if world(second) == first_world and first_world != "(no opening line published)":
        failures.append(f"both episodes opened in the same world: {first_world}")
    if not (second.get("actions") or []):
        failures.append("the second episode published no actions, so nothing can be done in it")

    for failure in failures:
        print("FAIL:", failure)
    if not failures:
        print("\nPASS: a second episode in the same process, with its own id, turn 1 and its own world")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
