#!/usr/bin/env python3
"""Drive live Qud decision boundaries over the mod's websocket RPC.

Speaks the endpoint the mod actually publishes (a websocket handshake with a
bearer token and no Origin header), not the HTTP form the mock uses. Read-only
apart from the actions it submits; prints each observation verbatim so the
interface shape is visible rather than asserted.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import websockets

CONTROL = pathlib.Path(
    pathlib.Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/QudGym-control.txt"
)


class Episode:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token, self.n = url, token, 0

    def rid(self) -> str:
        self.n += 1
        return f"r{self.n}"

    async def call(self, ws, op: str, **fields) -> dict:
        request = {"request_id": self.rid(), "op": op, **fields}
        await ws.send(json.dumps(request))
        raw = await asyncio.wait_for(ws.recv(), timeout=120)
        return json.loads(raw)

    async def run(self, turns: int) -> int:
        async with websockets.connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            origin=None,
            open_timeout=20,
        ) as ws:
            hello = await self.call(ws, "hello")
            print("hello:", json.dumps(hello, sort_keys=True)[:400])
            reset = await self.call(ws, "reset", seed=0)
            print("reset:", json.dumps(reset, sort_keys=True)[:400])

            for turn in range(1, turns + 1):
                obs = await self.call(ws, "observe")
                if "result" not in obs:
                    print(f"turn {turn}: observe -> {json.dumps(obs, sort_keys=True)}")
                    return 1
                body = obs["result"]
                print(f"\n===== turn {turn} =====")
                print(json.dumps(body, indent=2, sort_keys=True))
                actions = body.get("actions") or []
                ids = [a.get("id") for a in actions if isinstance(a, dict)]
                if not ids:
                    print("no candidate actions; stopping")
                    return 0
                move = next((i for i in ids if str(i).startswith("move:")), None)
                chosen = move or ids[0]
                print(f"--> step {chosen}")
                stepped = await self.call(
                    ws, "step", decision_id=body["decision_id"], action_id=chosen
                )
                if "result" not in stepped:
                    print("step ->", json.dumps(stepped, sort_keys=True))
                    return 1
                nxt = stepped["result"]
                print(f"    next decision {nxt.get('decision_id')} turn={nxt.get('turn')}")
        return 0


def main() -> int:
    url, token = CONTROL.read_text(encoding="utf-8").split()
    print(f"endpoint={url}")
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    return asyncio.run(Episode(url, token).run(turns))


if __name__ == "__main__":
    raise SystemExit(main())
