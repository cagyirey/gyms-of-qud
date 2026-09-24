"""A tiny, partially observed test world. This is NOT a simulation of Qud.

It includes zero-turn prompts, hidden PRNG state, bounded snapshots, death,
time limits and an explicit objective so the integration can be tested on CPU.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import secrets
from dataclasses import asdict, dataclass

from .errors import QudGymError
from .models import (CandidateAction, Capabilities, Metrics, Observation, Player,
                     Prompt, SnapshotHandle, StateHash, Transition)

_MAP = ("#######", "#.....#", "#..+.G#", "#.....#", "#######")
_DIRECTIONS = (("N", 0, -1), ("E", 1, 0), ("S", 0, 1), ("W", -1, 0))


@dataclass
class _State:
    x: int = 1
    y: int = 2
    hp: int = 6
    turns: int = 0
    decisions: int = 0
    gate_open: bool = False
    pending_gate: bool = False
    outcome: str = "ongoing"
    messages: tuple[str, ...] = ()


class MockBackend:
    def __init__(self, *, max_decisions: int = 128, max_snapshots: int = 128,
                 allow_oracle: bool = False):
        if (type(max_decisions) is not int or type(max_snapshots) is not int
                or max_decisions < 1 or max_snapshots < 1):
            raise ValueError("limits must be positive")
        self.max_decisions = max_decisions
        self.max_snapshots = max_snapshots
        self.allow_oracle = allow_oracle
        self._rng = random.Random(0)
        self._state: _State | None = None
        self._episode = ""
        self._generation = 0
        self._snapshots: dict[str, tuple[_State, object]] = {}
        self._closed = False

    def capabilities(self) -> Capabilities:
        return Capabilities(backend="mock-corridor-v1", game_build="not-qud", is_mock=True,
                            snapshot=self.allow_oracle, deterministic_restore=self.allow_oracle,
                            full_state_hash=self.allow_oracle)

    def _require(self) -> _State:
        if self._closed:
            raise QudGymError("closed", "This environment has been closed")
        if self._state is None:
            raise QudGymError("reset_required", "Reset before using the environment")
        return self._state

    def _oracle(self) -> _State:
        state = self._require()
        if not self.allow_oracle:
            raise QudGymError("unsupported", "Oracle capabilities are disabled for this worker")
        return state

    @property
    def _decision(self) -> str:
        return f"{self._episode}:{self._generation}"

    def reset(self, *, seed: int = 0) -> Transition:
        if self._closed:
            raise QudGymError("closed", "This environment has been closed")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise QudGymError("invalid_seed", "seed must be a uint32")
        self._rng.seed(seed)
        self._state = _State()
        self._episode = secrets.token_hex(12)
        self._generation += 1
        self._snapshots.clear()
        return self._transition(0.0)

    def observe(self) -> Observation:
        s = self._require()
        terminal = s.outcome != "ongoing"
        actions: list[CandidateAction] = []
        if not terminal and s.pending_gate:
            actions = [CandidateAction(id="answer:open", kind="answer", label="Open the passage"),
                       CandidateAction(id="answer:cancel", kind="answer", label="Cancel")]
        elif not terminal:
            for name, dx, dy in _DIRECTIONS:
                if _MAP[s.y + dy][s.x + dx] != "#":
                    actions.append(CandidateAction(id=f"move:{name}", kind="move",
                                                   label=f"Move {name}",
                                                   arguments={"dx": dx, "dy": dy}))
            actions.append(CandidateAction(id="wait", kind="wait", label="Wait one turn"))
        # Only reveal radius-two terrain. Candidate eligibility uses adjacent visible walls.
        tiles = tuple("".join(("." if c == "+" and s.gate_open else c)
                              if abs(x-s.x) + abs(y-s.y) <= 2 else "?"
                              for x, c in enumerate(row)) for y, row in enumerate(_MAP))
        return Observation(episode_id=self._episode, decision_id=self._decision, turn=s.turns,
                           phase="terminal" if terminal else "prompt" if s.pending_gate else "command",
                           player=Player(x=s.x, y=s.y, hp=s.hp, max_hp=6), tiles=tiles,
                           messages=s.messages,
                           prompt=Prompt(kind="choice", text="Open the passage?")
                           if s.pending_gate and not terminal else None,
                           actions=tuple(actions))

    def step(self, action_id: str, *, decision_id: str) -> Transition:
        s = self._require()
        if decision_id != self._decision:
            raise QudGymError("stale_decision", "Re-observe: the decision boundary has changed")
        if s.outcome != "ongoing":
            raise QudGymError("episode_finished", "Reset after a terminal or truncated result")
        candidates = {a.id: a for a in self.observe().actions}
        if action_id not in candidates:
            raise QudGymError("invalid_action", "Action is not a current candidate")
        a = candidates[action_id]
        s.messages = ()
        elapsed = 0
        if action_id == "answer:cancel":
            s.pending_gate = False
        elif action_id == "answer:open":
            s.pending_gate = False
            s.gate_open = True
            s.x, s.y = 3, 2
            elapsed = 1
        elif action_id == "wait":
            elapsed = 1
        elif a.kind == "move":
            x, y = s.x + int(a.arguments["dx"]), s.y + int(a.arguments["dy"])
            if (x, y) == (3, 2) and not s.gate_open:
                s.pending_gate = True
            else:
                s.x, s.y = x, y
                elapsed = 1
        if elapsed:
            noise = self._rng.randrange(3)  # every committed turn advances the hidden PRNG
            if (s.x, s.y) == (4, 2):
                s.hp = max(0, s.hp - noise)
                s.messages = (f"A test hazard deals {noise} damage.",)
        s.turns += elapsed
        s.decisions += 1
        self._generation += 1
        if s.hp == 0:
            s.outcome = "death"
        elif (s.x, s.y) == (5, 2):
            s.outcome = "success"
        elif s.decisions >= self.max_decisions:
            s.outcome = "time_limit"
        reward = 1.0 if s.outcome == "success" else -1.0 if s.outcome == "death" else 0.0
        return self._transition(reward)

    def _transition(self, reward: float) -> Transition:
        s = self._require()
        return Transition(observation=self.observe(), reward=reward,
                          terminated=s.outcome in ("success", "death"), truncated=s.outcome == "time_limit",
                          metrics=Metrics(task_id="mock-reach-exit-v1", objective_version="sparse-v1",
                                          outcome=s.outcome, turns_elapsed=s.turns,
                                          decisions_elapsed=s.decisions))

    def snapshot(self) -> SnapshotHandle:
        s = self._oracle()
        if len(self._snapshots) >= self.max_snapshots:
            raise QudGymError("snapshot_limit", "Release an unused snapshot before taking another")
        handle = secrets.token_hex(16)
        self._snapshots[handle] = (copy.deepcopy(s), self._rng.getstate())
        return SnapshotHandle(handle=handle)

    def restore(self, handle: str) -> Transition:
        self._oracle()
        if handle not in self._snapshots:
            raise QudGymError("unknown_snapshot", "Snapshot is unknown, released, or from a previous reset")
        s, rng = self._snapshots[handle]
        self._state = copy.deepcopy(s)
        self._rng.setstate(rng)
        # Restore physical state, NEVER rewind the action cursor (avoids ABA/stale actions).
        self._generation += 1
        return self._transition(0.0)

    def release(self, handle: str) -> None:
        self._oracle()
        if handle not in self._snapshots:
            raise QudGymError("unknown_snapshot", "Snapshot is not retained")
        del self._snapshots[handle]

    def state_hash(self) -> StateHash:
        s = self._oracle()
        payload = {"backend": "mock-corridor-v1", "state": asdict(s),
                   "rng": self._rng.getstate(), "max_decisions": self.max_decisions,
                   "python_rng_format": 3}
        # Excludes transport IDs and snapshot table, includes every transition-relevant field.
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return StateHash(digest=hashlib.sha256(raw.encode()).hexdigest())

    def close(self) -> None:
        self._snapshots.clear()
        self._state = None
        self._closed = True
