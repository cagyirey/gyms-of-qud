## Foundation

- Native typed decision-boundary environment and versioned JSON schemas.
- Mock world with partial observations, zero-turn prompts, sparse rewards, explicit death/truncation and hidden RNG.
- Opt-in oracle operations: bounded snapshots, deterministic mock replay, full-state hash and stale-cursor rejection.
- Authenticated loopback controller/client, serialized mutation processing, request correlation and bounded duplicate suppression.
- Candidate-scoring seam and research JSONL trajectories; no fabricated token/logprob training records.
- Native NeMo Gym GymnasiumServer adapter, gymnasium_agent recipe, example task and safe staging script.
- Engine-neutral C# turn-thread handoff and standalone smoke-test project.
- Local-install manifest collector, CI, source evidence, protocol invariants and next implementation issues.

## Validation and limitations

See docs/VALIDATION.md for executed checks. The core/reference HTTP path, C# BridgeCore smoke, and a deterministic native NeMo Gym contract smoke have local evidence. Real local-model inference, Platform/Studio mutation, and a loadable/live Qud mod remain unexecuted; deterministic snapshot claims apply only to the mock.

Game binaries/assets/saves are not included. No model is trained and no GPU is required for the mock tests. Follow-up live integration requires the exact installed Qud build and an isolated test profile.
