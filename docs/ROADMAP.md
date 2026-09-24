# First implementation backlog

These are issue-ready work items, not remotely created GitHub issues.

## 1. Establish the installed-game compatibility target

Input: reviewed install manifest, exact build, enabled mods/load order. Validate the redacted manifest/report with the typed compatibility contract, then add a minimal local diagnostic mod and a dedicated test profile. Acceptance: it loads and reports build, candidate hook availability and hook thread IDs without save modifications. Record API evidence; do not infer support from Raves' older build.

## 2. Implement read-only player-observation projection

Expose stable player/visible entity IDs, perceived tiles, messages, prompts and the supported input candidates. Acceptance: unknown items remain unidentified; unseen actors/hidden stats do not affect features or candidate filtering; projection runs on the correct thread; no Unity rendering calls in the worker.

## 3. Connect primitive actions to resolved boundaries

Implement move/wait/prompt-choice and the actual C# wire host. Keep the transport host outside the game-turn state machine; do not embed a hand-written web server in the F# game library or wait on unbounded client tasks from the turn thread. Acceptance: one accepted request maps to one consumed input; zero-turn actions get fresh cursors; nested prompts, failed moves, focus loss, queued cancellations and lost replies do not double-step or deadlock. Validate the transport against the committed JSON schemas. Port the reference deduplication semantics; BoundaryQueue alone is not a complete RPC server.

## 4. Add safe scenario reset and trajectory capture

Use isolated test profiles or copied seed fixtures, never an original personal save. Record build/mod/task/objective versions. Acceptance: reset failures are infrastructure failures, not policy losses; original files remain unchanged; trajectories label their observation privilege level and task identity.

## 5. Audit snapshots and determinism before enabling TAS privileges

Inventory RNG and scheduler/world state. Add opaque, bounded snapshot handles and a canonical full-state fingerprint. Acceptance: at least 100 tested action branches including random damage and prompts replay identically after restore on the supported build; old action cursors remain invalid. State evidence limits explicitly. Failure keeps live `deterministic_restore=false`; no imitation of success with an observation-only hash.

## 6. Validate the native NeMo rollout path

Stage the adapter in the pinned NeMo Gym checkout, run scripted and model-driven mock episodes, validate cleanup and error reporting, and record an actual rollout artifact. Replace the mock session factory with live worker leases only once 1–4 pass. Keep inference/training framework adapters thin. Pointer-head training and policy-guided search follow valid trajectories and snapshot evidence, not the reverse.
