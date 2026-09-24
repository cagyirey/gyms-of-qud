"""Research JSONL, not a fabricated NeMo on-policy token/logprob dataset."""
import json
from pathlib import Path
from .env import QudEnv


class TrajectoryRecorder:
    def __init__(self, env: QudEnv, path: str | Path):
        self.env = env
        self._file = Path(path).open("x", encoding="utf-8")

    def _write(self, value):
        self._file.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        self._file.flush()

    def reset(self, *, seed: int = 0):
        result = self.env.reset(seed=seed)
        self._write({"record_version": "0.1", "kind": "reset",
                     "capabilities": self.env.backend.capabilities().model_dump(mode="json"),
                     "control_metadata": {"seed": seed},
                     "transition": result.model_dump(mode="json")})
        return result

    def step(self, action_id: str):
        if self.env.current is None:
            raise ValueError("Reset before recording steps")
        before = self.env.current.observation
        result = self.env.step(action_id)
        self._write({"record_version": "0.1", "kind": "step", "action_id": action_id,
                     "before": before.model_dump(mode="json"),
                     "transition": result.model_dump(mode="json")})
        return result

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
