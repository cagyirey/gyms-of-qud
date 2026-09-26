#!/usr/bin/env python3
"""Generate a recorded trajectory through the action-space guard.

Every decision goes through ActionSpace, so an action the model invents is
rejected at the boundary with the candidate list rather than being sent to the
game. Each decision is recorded as an LLM call alongside the environment
transition, which is what makes the resulting ATOF stream usable as training
data rather than just a log.

    scripts/generate_trajectory.py OUT.atof.jsonl [--backend mock|live]
                                          [--steps N] [--agent NAME]

The operating model is stubbed here: the point is to produce and inspect a real
trajectory with the guard in the loop. Swapping in a model call means replacing
_decide only.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qudgym import QudEnv  # noqa: E402
from qudgym.guidance import IllegalAction, build_action_space  # noqa: E402
from qudgym.models import Observation  # noqa: E402
from qudgym.prompts import build_messages  # noqa: E402
from qudgym.recording import RecordingConfig, SessionRecorder  # noqa: E402

CONTROL = pathlib.Path(
    pathlib.Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/QudGym-control.txt"
)


def _stub_policy(observation: Observation, misbehave: bool = False) -> tuple[str, str]:
    """Stand-in for the operating model. Returns (raw_text, reason).

    Deliberately returns free text rather than a bare id, so the guard is doing
    real extraction work instead of passing a clean value through.
    """
    if misbehave:
        return "move:Z", "stub is inventing an action on purpose"
    ids = [a.id for a in observation.actions]
    # At a prompt the only legal actions are answers, so answer first.
    answer = next((i for i in ids if i.startswith("answer:")), None)
    if answer is not None and observation.phase != "command":
        return f"action_id: {answer}", "stub answers a prompt"
    if "move:E" in ids:
        return "move:E", "stub prefers east"
    move = next((i for i in ids if i.startswith("move:")), None)
    if move:
        return f"I will go {move[5:]}.", "stub fallback"
    if "wait" in ids:
        return "wait", "stub waits"
    # Whatever is left, said in prose rather than as an id.
    return f"Let us {ids[0]}.", "stub fallback to the only candidate"


def _decide(observation: Observation, recorder: SessionRecorder, verbose: bool,
             misbehave: bool = False) -> str:
    # The observation comes from the transition the recorder already holds.
    # env.reconcile() would re-read the backend and clear env.current, which is
    # exactly the state the recorder needs to accept the next step.
    space = build_action_space(observation)
    raw, reason = _stub_policy(observation, misbehave)
    try:
        chosen = space.extract(raw)
    except IllegalAction:
        # Record the rejection rather than silently substituting a legal move.
        with recorder.llm_call(
            model_name="stub-policy",
            provider_name=recorder.config.provider_name,
            input_messages=build_messages(observation, space),
        ) as call:
            call.output_text = raw
            call.finish_reasons = ["rejected"]
        raise
    with recorder.llm_call(
        model_name="stub-policy",
        provider_name=recorder.config.provider_name,
        input_messages=build_messages(observation, space),
    ) as call:
        call.output_text = chosen
    if verbose:
        print(f"  {observation.decision_id} -> {chosen}  ({reason})")
    return chosen


def _render(observation: Observation, space) -> str:
    lines = [
        f"turn {observation.turn}  phase {observation.phase}",
        f"player hp {observation.player.hp}/{observation.player.max_hp} "
        f"at ({observation.player.x},{observation.player.y})",
    ]
    if observation.entities:
        lines.append("nearby: " + ", ".join(
            f"{e.name} at ({e.x},{e.y})" for e in observation.entities[:8]))
    if observation.messages:
        lines.append("recent: " + " / ".join(m.strip() for m in observation.messages[-3:]))
    if observation.prompt:
        lines.append(f"prompt: {observation.prompt.kind}: {observation.prompt.text}")
    lines.append("legal actions: " + ", ".join(space.action_ids))
    return "\n".join(lines)


def _open_env(backend: str):
    if backend == "live":
        from qudgym.client import HttpBackend  # noqa: F401 - documents the mismatch

        raise SystemExit(
            "The live backend is driven over a websocket with a bearer token, not the "
            "HTTP HttpBackend. Use scripts/live_step_probe.py for live sessions until "
            "the client speaks the websocket transport."
        )
    from qudgym.mock import MockBackend

    return QudEnv(MockBackend())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--backend", default="mock", choices=("mock", "live"))
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--agent", default="stub-policy")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--misbehave", action="store_true",
                        help="invent an illegal action, to show the guard rejecting it")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 2

    with _open_env(args.backend) as env:
        recorder = SessionRecorder(
            env,
            args.output,
            config=RecordingConfig(
                agent_name=args.agent,
                agent_version="0.1.0",
                provider_name=args.backend,
                capture_content=True,
            ),
        )
        try:
            transition = recorder.reset(seed=args.seed)
            observation = Observation.model_validate(transition.observation.model_dump())
            if not args.quiet:
                print(f"recording {args.output}")
            for _ in range(args.steps):
                action = _decide(observation, recorder, verbose=not args.quiet,
                                  misbehave=args.misbehave)
                transition = recorder.step(action)
                observation = Observation.model_validate(transition.observation.model_dump())
                if transition.terminated or transition.truncated:
                    break
        finally:
            recorder.close()

    print(f"events: {recorder.event_count}  bytes: {args.output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
