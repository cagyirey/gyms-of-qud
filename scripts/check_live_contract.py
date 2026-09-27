#!/usr/bin/env python3
"""Validate live observations against the repository's own wire contract.

The live projection is built by reflection inside the game, so nothing in the
test suite can hold it to src/qudgym/models.py. This drives a real session and
runs every observation it receives through Observation.model_validate, so a
schema drift between the live backend and the mock backend fails loudly instead
of quietly producing trajectories that nothing downstream can parse.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qudgym.models import Observation  # noqa: E402

CONTROL = pathlib.Path(
    pathlib.Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/QudGym-control.txt"
)


class Episode:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token, self.n = url, token, 0
        self.prefix = os.urandom(6).hex()
        self.seen = 0

    async def call(self, ws, op: str, **fields) -> dict:
        self.n += 1
        await ws.send(json.dumps({"request_id": f"{self.prefix}-{self.n}", "op": op, **fields}))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=120))

    async def run(self, turns: int) -> int:
        failures: list[str] = []
        async with websockets.connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            origin=None,
            open_timeout=20,
        ) as ws:
            await self.call(ws, "hello")
            await self.call(ws, "reset", seed=0)
            obs = (await self.call(ws, "observe"))["result"]
            for turn in range(turns):
                self.check(obs, turn, failures)
                ids = [a["id"] for a in obs["actions"]]
                choice = next((i for i in ids if str(i).startswith("move:")), None) or ids[0]
                stepped = await self.call(
                    ws, "step", decision_id=obs["decision_id"], action_id=choice
                )
                if "result" not in stepped:
                    print("  step ->", json.dumps(stepped.get("error", {}), sort_keys=True))
                    break
                obs = stepped["result"]["observation"]
        if failures:
            print(f"CONTRACT VIOLATIONS ({len(failures)}):")
            for f in failures[:8]:
                print("  -", f)
            return 1
        print(f"live contract ok: {self.seen} observation(s) validated")
        return 0

    def check(self, obs: dict, turn: int, failures: list[str]) -> None:
        self.seen += 1
        try:
            Observation.model_validate(obs)
        except Exception as exc:  # noqa: BLE001 - report whatever pydantic says
            first = str(exc).splitlines()
            detail = " | ".join(x.strip() for x in first[:4])
            failures.append(f"turn {turn}: {detail}")


def main() -> int:
    url, token = CONTROL.read_text(encoding="utf-8").split()
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    return asyncio.run(Episode(url, token).run(turns))


if __name__ == "__main__":
    raise SystemExit(main())
