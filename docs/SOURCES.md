# Source inspection record

Inspected on 2026-09-23 and revalidated on 2026-09-25. These are primary-source references, not claims that every referenced repository is compatible with the user's installed game.

## NVIDIA NeMo Gym

Reference HEAD: `1c8261080bdc881b3e9b7f870e6418f160516991` (`0.7.0rc0`).

- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/resources_servers/gymnasium/base.py — native reset/step tuple contract, session middleware, terminal cleanup.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/responses_api_agents/gymnasium_agent/app.py — max_steps, preserved session cookies, usage accumulation, optional explicit `/close` on completion/failure.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/nemo_gym/base_resources_server.py — resource-server interfaces and distinction between invalid measurements and valid rewards.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/nemo_gym/task_data.py — dependency-light task-row schema convention.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/nemo_gym/cli/main.py — unified `gym env`, `gym eval`, and Hydra override contracts.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/nemo_gym/rollout_collection.py — materialized inputs, rollout persistence, failure sidecars, capture projection, and aggregate metrics.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/nemo_gym/atif_export.py — strict native `ng_trajectory` to ATIF v1.7 boundary.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/responses_api_models/vllm_model/app.py — Responses-to-Chat-Completions bridge for an externally served vLLM endpoint.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/responses_api_models/local_vllm_model/app.py — Gym-managed vLLM lifecycle, distinct from the external-server wrapper.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/.agents/skills/nemo-gym-reward-profiling/SKILL.md — NVIDIA's `env start` -> `eval run --no-serve` -> `eval profile` workflow.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/.agents/skills/nemo-gym-debugging/SKILL.md — rollout, model-serving, verifier, cache, and artifact failure classification.
- https://docs.nvidia.com/nemo/gym/about/ — project documentation.
- https://github.com/NVIDIA-NeMo/RL — separate reinforcement-learning training project.

The adapter consumes these public interfaces; no NeMo implementation source is
vendored. The current native Gym trajectory has coverage gaps, so the strict
Gym-to-ATIF exporter is not an acceptance gate for this environment.

## NVIDIA NeMo Platform and NeMo Agent Toolkit

Reference local Platform checkout: `763f10c0c9def51e5d94fed00a5fb2284a5214fb`.

- `packages/nemo_platform_ext/src/nemo_platform_ext/skills/nemo-skill-selection/SKILL.md` — CLI-first platform routing and Studio boundary.
- `packages/nemo_platform_ext/src/nemo_platform_ext/skills/inference/SKILL.md` — provider, VirtualModel, Inference Gateway, and served-model entity conventions.
- `packages/nemo_platform_ext/src/nemo_platform_ext/skills/nemo-experiments-upload/SKILL.md` — Experiment Group/Evaluation creation and ATIF/chat/OTLP ingestion into Studio.
- `packages/nemo_platform_ext/src/nemo_platform_ext/skills/nemo-experiments-upload/references/ingest-formats.md` — strict intake payload and identity fields.
- `skills/nat-telemetry/SKILL.md` and `skills/nat-evaluation/references/evaluation-surfaces.md` — NAT-native telemetry and ATIF evaluation ownership.

Platform has no first-class `GymnasiumServer` lifecycle in this checkout.
Studio is a web UI; NeMo Gym remains the rollout owner. Platform Intake is a
later display/ingest surface and requires complete ATIF or another documented
producer payload.

## Raves of Qud

Reference HEAD: `9f82ad4548b4c41b55700082cd0f1d589d8343e1`.

- https://github.com/TheMutantFactory/raves-of-qud/blob/9f82ad4548b4c41b55700082cd0f1d589d8343e1/docs/architecture.md — game-turn/render-thread distinction, prompts, per-turn streaming and lack of per-command acknowledgments.
- https://github.com/TheMutantFactory/raves-of-qud/blob/9f82ad4548b4c41b55700082cd0f1d589d8343e1/mod/BridgePart.cs — EndTurn/BeginTakeAction/BeforeRender event hooks; the source also has an older summary comment calling it the main thread, so the actual thread must be validated locally.

No Raves implementation, game code, binaries or assets are copied into this repository. A rendering-state bridge is evidence of feasibility, not proof of a deterministic, legally action-complete RL interface.

## Gymnasium

- https://gymnasium.farama.org/api/env/ — terminated versus truncated and reset/step semantics. The core package is intentionally native/structured rather than claiming gymnasium.Env conformance without defining correct spaces.
