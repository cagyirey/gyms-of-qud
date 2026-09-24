# Agent-eye validation — 2026-09-24

## Executed on the rebased branch

- Python 3.13.5, Pydantic 2.13.4. Installed editable with local dependencies.
- `python -m pytest -q`: **127 passed** (68 foundation + 59 agent-eye tests).
- All three synthetic arena demos terminate after disabling the practice target.
- `python -m compileall -q src scripts integrations`: passed.
- `qudgym smoke`, `python examples/branch_and_replay.py`, and the .NET 10
  `BridgeCore` smoke test passed.
- The browser smoke test was not rerun locally because this checkout does not
  have Playwright/Chromium installed. The pre-rebase browser run passed, but
  the rebased PR must rely on a fresh CI browser job for confirmation.

## What the new tests establish

Fixture-level noninterference for unobserved location/property changes (including
candidates); no live updates to remembered contacts; location-vs-identity sensory
disclosure; new handles on contact reacquisition; known-zero-vs-unknown validation;
provenance/timestamp checks; bounded memory/events and explicit evictions; no
hypothesis promotion to observed facts; idempotence, episode separation and rewind
rejection; reference integrity; entity/candidate permutation and diagnostic scorer
handle-renaming invariance; zero-time prompts preserving cooldowns/resources;
trace JSONL round trips, unsupported versions and invalid recorded actions;
HTML/script-breakout escaping, text-only DOM sinks and blocked external requests;
lossless canonical text/structured views; per-session presenter isolation; build
library shape checks; CLI and JSON Schema export.

## Not established

No live Qud integration, official builds, game-rule fidelity, game legality checks,
learned policy, learned belief updater, world model, Qud snapshot/fork, GPU or NIM
inference, or native NeMo rollout was executed. Generic schema validation does not
prove that a future game adapter sources its facts fairly. These tests constrain
the synthetic producer and the shared evidence/presentation machinery.

The baseline foundation PR's previously completed GitHub Actions run was observed
as successful. New branch CI is independent and must be checked after publication;
this file does not predeclare it green. C# code was not changed in this milestone.
