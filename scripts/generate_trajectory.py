#!/usr/bin/env python3
"""Record a trajectory through the action-space guard.

Every decision goes through ActionSpace, so an action the model invents is
rejected at the boundary with the candidate list and retried, never silently
substituted. A substitution would make a recorded episode look successful while
hiding the one fact worth recording, so exhausting the retries is a hard stop
rather than a fallback.

    scripts/generate_trajectory.py OUT.jsonl [--backend mock|live] [--steps N]
                                          [--agent NAME] [--misbehave]

--backend mock runs the stub policy offline. --backend live drives the running
game over its websocket and asks the real model through opencode's generate
route, reusing one session for the episode so opencode keeps its prompt cache
and the provider's session routing stays satisfied.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qudgym import QudEnv  # noqa: E402
from qudgym.guidance import IllegalAction, build_action_space  # noqa: E402
from qudgym.models import Observation  # noqa: E402
from qudgym.prompts import SYSTEM, build_messages  # noqa: E402
from qudgym.recording import RecordingConfig, SessionRecorder  # noqa: E402

CONTROL = pathlib.Path(
    pathlib.Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/QudGym-control.txt"
)


class StubPolicy:
    """Stand-in for the operating model, so the pipeline is testable offline.

    Answers in prose rather than a bare id on purpose, so the guard does real
    extraction work instead of passing a clean value through.
    """

    name = "stub-policy"

    def __init__(self, misbehave: bool = False) -> None:
        self.misbehave = misbehave
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        if self.misbehave:
            return "move:Z"
        ids = _legal_ids(prompt)
        answer = next((i for i in ids if i.startswith("answer:")), None)
        if answer:
            return f"action_id: {answer}"
        move = next((i for i in ids if i.startswith("move:")), None)
        if move:
            return f"action_id: {move}"
        return f"action_id: {ids[0]}"


class OpenCodePolicy:
    """The real operating model, through opencode's generate route."""

    def __init__(self, model) -> None:
        self.model = model
        self.name = f"{model.model_id}"

    @property
    def calls(self) -> int:
        return self.model.calls

    def complete(self, system: str, prompt: str) -> str:
        return self.model.complete(system, prompt)


def _legal_ids(prompt: str) -> list[str]:
    """Recover the offered ids from the rendered prompt, for the stub only."""
    tail = prompt.rsplit("Legal actions -> ", 1)[-1]
    out: list[str] = []
    for group in tail.split(";"):
        _, _, rest = group.partition(":")
        for candidate in rest.split(","):
            token = candidate.strip()
            if ":" in token:
                out.append(token)
    return out


def _decide(observation: Observation, recorder: SessionRecorder, policy,
            verbose: bool, max_attempts: int = 3) -> str:
    space = build_action_space(observation)
    messages = build_messages(observation, space)
    system = messages[0]["content"]
    prompt = messages[1]["content"]
    for attempt in range(1, max_attempts + 1):
        raw = policy.complete(system, prompt)
        chosen: str | None = None
        with recorder.llm_call(
            model_name=policy.name,
            provider_name=recorder.config.provider_name,
            input_messages=[{"role": "system", "content": system},
                            {"role": "user", "content": prompt}],
        ) as call:
            call.output_text = raw
            try:
                chosen = space.extract(raw)
            except IllegalAction:
                call.finish_reasons = ["rejected"]
            else:
                call.finish_reasons = ["stop"]
        if chosen is not None:
            if verbose:
                print(f"  {observation.decision_id} -> {chosen}  (attempt {attempt})")
            return chosen
        if verbose:
            print(f"  {observation.decision_id} attempt {attempt} REJECTED: {raw[:70]!r}")
        prompt = prompt + space.retry_message(raw)
    raise SystemExit(
        f"policy produced no legal action in {max_attempts} attempts; "
        "the rejections are recorded and no action was substituted"
    )


def _open_env(args):
    if args.backend == "mock":
        from qudgym.mock import MockBackend

        policy = StubPolicy(misbehave=args.misbehave)
        return QudEnv(MockBackend()), policy
    if args.control is None:
        raise SystemExit("--control is required for --backend live")
    url, token = args.control.read_text(encoding="utf-8").split()
    from qudgym.live import LiveBackend, OpenCodeModel, discover_opencode_service

    backend = LiveBackend(url, token=token)
    # One session per episode, so opencode reuses its prompt cache and the
    # provider's session routing requirement is met for every call.
    # The service URL and its local credential are discovered, not configured,
    # because the port changes on every service start.
    service_url, auth = discover_opencode_service()
    model = OpenCodeModel(
        service_url,
        auth=auth,
        session_id=f"qudgym-{os.urandom(6).hex()}",
        model=args.model,
        provider=args.provider,
    )
    caps = backend.capabilities()
    if caps.is_mock:
        raise SystemExit("refusing to record: the backend reports itself as a mock")
    print(f"live backend: game_build={caps.game_build} is_mock={caps.is_mock}")
    return QudEnv(backend), OpenCodePolicy(model)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--backend", default="mock", choices=("mock", "live"))
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--misbehave", action="store_true",
                        help="invent an illegal action, to show the guard rejecting it")
    parser.add_argument("--control", type=pathlib.Path, default=CONTROL)
    parser.add_argument("--model", default="space-bunny-free")
    parser.add_argument("--provider", default="opencode-go")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 2

    env, policy = _open_env(args)
    with env:
        recorder = SessionRecorder(
            env,
            args.output,
            config=RecordingConfig(
                agent_name=args.agent or policy.name,
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
                action = _decide(observation, recorder, policy, verbose=not args.quiet)
                transition = recorder.step(action)
                observation = Observation.model_validate(transition.observation.model_dump())
                if transition.terminated or transition.truncated:
                    break
        finally:
            recorder.close()

    print(f"events: {recorder.event_count}  bytes: {args.output.stat().st_size}"
          f"  model calls: {policy.calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
