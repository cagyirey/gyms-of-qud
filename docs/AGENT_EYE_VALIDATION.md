# Agent-eye validation — 2026-09-24

## Executed on the rebased branch

- Python 3.13.5, Pydantic 2.13.4. Installed editable with local dependencies.
- `python -m pytest -q`: **139 passed** (68 foundation + 71 agent-eye tests).
- All three synthetic arena demos terminate after disabling the practice target.
- `python -m compileall -q src scripts integrations`: passed.
- `qudgym smoke`, `python examples/branch_and_replay.py`, and the .NET 10
  `BridgeCore` smoke test passed.
- Browser smoke was not rerun locally because this checkout does not have
  Playwright/Chromium installed; the fresh GitHub Actions browser job passed
  for the rebased commit.

## What the new tests establish

Fixture-level noninterference for unobserved location/property changes (including
candidates); no live updates to remembered contacts; location-vs-identity sensory
disclosure; new handles on contact reacquisition; known-zero-vs-unknown validation;
strict unknown-channel handling; provenance/timestamp checks; explicit branch and
parent lineage across same-turn prompts; bounded memory/events and explicit
evictions; atomic memory failure handling; observed/reported claim separation;
unknown relation/event omission; aggregate frame/view budgets; no hypothesis
promotion to observed facts; idempotence, episode separation and rewind rejection;
reference integrity; entity/candidate permutation and diagnostic scorer
handle-renaming invariance; zero-time prompts preserving cooldowns/resources;
bounded legacy conversion without inventing empty zones; trace JSONL round trips,
unsupported versions and invalid recorded actions; HTML/script-breakout escaping,
text-only DOM sinks and blocked external requests; lossless canonical text/structured
views; per-session presenter isolation; build library shape checks; CLI and JSON
Schema export. NeMo adapter tests also cover typed step-cache replay for policy
errors, presentation faults, and reset-cookie collisions.

## Not established

No live Qud integration, official builds, game-rule fidelity, game legality checks,
learned policy, learned belief updater, world model, Qud snapshot/fork, GPU or NIM
inference, or real local-model NeMo rollout was executed. A deterministic native
Gym contract smoke was later run for the mock adapter; it does not establish
model quality or live-game behavior. Generic schema validation does not prove that
a future game adapter sources its facts fairly. These tests constrain the
synthetic producer and the shared evidence/presentation machinery.

Fresh branch GitHub Actions passed all five jobs: Python 3.11/3.12/3.13,
BridgeCore on .NET 10, and the Chromium replay smoke test. C# source was not
changed in this milestone.
