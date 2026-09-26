#!/usr/bin/env python3
"""Drive live Qud decision boundaries over the mod's websocket RPC.

Two contract details that are easy to get wrong:

- The endpoint the mod publishes is a websocket handshake with a bearer token
  and no Origin header. The repo's HttpBackend posts HTTP and gets 405.
- request_id dedupe is process-global, not per-connection, so ids must be
  unique for the lifetime of the game process. Reusing r1 on a second
  connection against the same process is rejected with request_id_conflict.
  Every run therefore stamps a fresh prefix.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import websockets

CONTROL = pathlib.Path(
    pathlib.Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/QudGym-control.txt"
)
DIRECTIONS = {"move:E": (1, 0), "move:W": (-1, 0), "move:N": (0, -1), "move:S": (0, 1)}


class Episode:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token, self.n = url, token, 0
        # Unique per process, because the server's dedupe cache outlives a
        # connection and rejects a reused id with different content.
        self.prefix = os.urandom(6).hex()

    async def call(self, ws, op: str, **fields) -> dict:
        self.n += 1
        await ws.send(json.dumps({"request_id": f"{self.prefix}-{self.n}", "op": op, **fields}))
        raw = await asyncio.wait_for(ws.recv(), timeout=120)
        return json.loads(raw)

    async def run(self, turns: int, target: str | None) -> int:
        async with websockets.connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            origin=None,
            open_timeout=20,
        ) as ws:
            await self.call(ws, "hello")
            await self.call(ws, "reset", seed=0)
            obs = (await self.call(ws, "observe"))["result"]

            if target:
                obs = await self.walk_to(ws, obs, target)
                if obs is None:
                    return 1
                obs = await self.bump(ws, obs)
                if obs is None:
                    return 1

            for _ in range(turns):
                actions = obs.get("actions") or []
                ids = [a.get("id") for a in actions if isinstance(a, dict)]
                if not ids:
                    print("no candidate actions; stopping")
                    return 0
                choice = next((i for i in ids if str(i).startswith("move:")), None) or ids[0]
                print(f"  step {choice}  phase={obs.get('phase')}")
                if obs.get("phase") != "command":
                    print("  prompt:", json.dumps(obs.get("prompt")))
                    return 0
                stepped = await self.call(
                    ws, "step", decision_id=obs["decision_id"], action_id=choice
                )
                if "result" not in stepped:
                    print("  step ->", json.dumps(stepped.get("error", {}), sort_keys=True))
                    return 1
                obs = stepped["result"]["observation"]
        return 0

    async def walk_to(self, ws, obs: dict, name: str) -> dict | None:
        npc = next((e for e in obs.get("entities", []) if e.get("name") == name), None)
        if not npc:
            print(f"target {name!r} not visible:", [e.get("name") for e in obs.get("entities", [])])
            return None
        print(f"walking to {name} at ({npc['dx']},{npc['dy']})")
        dx, dy = npc["dx"], npc["dy"]
        for _ in range(20):
            if dx == 0 and dy == 0:
                break
            if dx:
                mv = "move:E" if dx > 0 else "move:W"
            else:
                mv = "move:S" if dy > 0 else "move:N"
            r = await self.call(ws, "step", decision_id=obs["decision_id"], action_id=mv)
            if "result" not in r:
                print("  walk stopped:", json.dumps(r.get("error", {}), sort_keys=True))
                return None
            obs = r["result"]["observation"]
            sx, sy = DIRECTIONS[mv]
            dx -= sx
            dy -= sy
        print("  adjacent:", [(e["name"], e["dx"], e["dy"]) for e in obs["entities"] if not e["is_self"]][:6])
        return obs

    async def bump(self, ws, obs: dict) -> dict | None:
        for mv in ("move:E", "move:S", "move:W", "move:N", "move:E", "move:S"):
            r = await self.call(ws, "step", decision_id=obs["decision_id"], action_id=mv)
            if "result" not in r:
                print("  bump", mv, "->", json.dumps(r.get("error", {}), sort_keys=True))
                return None
            obs = r["result"]["observation"]
            print("  bump", mv, "phase=", obs.get("phase"))
            if obs.get("phase") != "command":
                print("  PROMPT:", json.dumps(obs.get("prompt"))[:600])
                return obs
        return obs


def main() -> int:
    url, token = CONTROL.read_text(encoding="utf-8").split()
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    target = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    return asyncio.run(Episode(url, token).run(turns, target))


if __name__ == "__main__":
    raise SystemExit(main())
