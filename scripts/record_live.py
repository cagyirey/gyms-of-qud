"""Record a short live trajectory from a running QudGym control endpoint.

Start Caves of Qud headless with the QudGym mod first. Reset blocks until the
Artifex loadout is in Joppa and the game thread is waiting for a command.
The recorded seed is 0 because this process has already embarked; it is not
the world seed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from qudgym.client import WebSocketBackend
from qudgym.env import QudEnv
from qudgym.trajectory import TrajectoryRecorder


def control_file() -> Path:
    override = os.environ.get("QUDGYM_CONTROL")
    if override:
        return Path(override)
    return Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/QudGym-control.txt"


def main() -> None:
    lines = control_file().read_text(encoding="utf-8").splitlines()
    url, token = lines[0].strip(), lines[1].strip()
    backend = WebSocketBackend(url, token=token, timeout=180)
    out = Path("local/live-trajectory.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    env = QudEnv(backend)
    with TrajectoryRecorder(env, out) as recorder:
        first = recorder.reset(seed=0)
        recorder.step("wait")
        moved = recorder.step("move:E")
    print(json.dumps({
        "wrote": str(out),
        "backend": env.backend.capabilities().backend,
        "build": env.backend.capabilities().game_build,
        "start": [first.observation.player.x, first.observation.player.y],
        "after_move_east": [moved.observation.player.x, moved.observation.player.y],
        "turn": moved.observation.turn,
        "decisions": moved.metrics.decisions_elapsed,
    }))


if __name__ == "__main__":
    main()
