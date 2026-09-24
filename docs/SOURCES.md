# Source inspection record

Inspected on 2026-09-23. These are primary-source references, not claims that every referenced repository is compatible with the user's installed game.

## NVIDIA NeMo Gym

Reference HEAD: `1c8261080bdc881b3e9b7f870e6418f160516991`.

- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/resources_servers/gymnasium/base.py — native reset/step tuple contract, session middleware, terminal cleanup.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/responses_api_agents/gymnasium_agent/app.py — max_steps, preserved session cookies, optional explicit /close on completion/failure.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/nemo_gym/base_resources_server.py — resource server interfaces and distinction between invalid measurements and valid rewards.
- https://github.com/NVIDIA-NeMo/Gym/blob/1c8261080bdc881b3e9b7f870e6418f160516991/resources_servers/example_multi_step/configs/example_multi_step.yaml — resource/agent/model server reference syntax.
- https://docs.nvidia.com/nemo/gym/about/ — project documentation.
- https://github.com/NVIDIA-NeMo/RL — separate reinforcement-learning training project.

The adapter consumes these public interfaces; no NeMo implementation source is vendored.

## Raves of Qud

Reference HEAD: `9f82ad4548b4c41b55700082cd0f1d589d8343e1`.

- https://github.com/TheMutantFactory/raves-of-qud/blob/9f82ad4548b4c41b55700082cd0f1d589d8343e1/docs/architecture.md — game-turn/render-thread distinction, prompts, per-turn streaming and lack of per-command acknowledgments.
- https://github.com/TheMutantFactory/raves-of-qud/blob/9f82ad4548b4c41b55700082cd0f1d589d8343e1/mod/BridgePart.cs — EndTurn/BeginTakeAction/BeforeRender event hooks; the source also has an older summary comment calling it the main thread, so the actual thread must be validated locally.

No Raves implementation, game code, binaries or assets are copied into this repository. A rendering-state bridge is evidence of feasibility, not proof of a deterministic, legally action-complete RL interface.

## Gymnasium

- https://gymnasium.farama.org/api/env/ — terminated versus truncated and reset/step semantics. The core package is intentionally native/structured rather than claiming gymnasium.Env conformance without defining correct spaces.
