# Native NeMo Gym integration

**Source-reviewed, not runtime-validated yet. Mock backend only.** Reference upstream commit: `NVIDIA-NeMo/Gym@1c8261080bdc881b3e9b7f870e6418f160516991`.

The adapter extends `resources_servers.gymnasium.base.GymnasiumServer` and uses the existing `responses_api_agents/gymnasium_agent`. It does not implement a replacement trainer, model proxy, tokenizer or policy-loss pipeline.

## Shape

NeMo model server → `gymnasium_agent` → QudGym `GymnasiumServer` → native backend.

`reset` consumes the task seed and decision budget from dataset metadata. `step` receives the model's `NeMoGymResponse`; a small baseline parser accepts exactly `{"action_id":"...","decision_id":"..."}` from response text. Each selection is checked against the authoritative session and current cursor. The observation is serialized JSON retaining the full candidate metadata, not an ASCII-only flattened feature hack.

The only reward source is the backend transition: +1 success, -1 death, zero otherwise for this mock task. Invalid policy JSON/actions get zero, preserve game state and consume the **agent-attempt** budget; they are not infrastructure failures. Missing sessions/capacity failures raise HTTP errors rather than reporting a valid zero-score episode. The upstream agent is responsible for surfacing such failed rollouts to the evaluator/trainer; validate its behavior in the pinned release before training.

Sessions are bounded and isolated, with cleanup on terminal results, explicit `/close`, and expiry. `/reset` advertises `supports_explicit_close: true`, supported by the inspected upstream agent. Neither snapshots nor oracle hashes are exposed to the agent. The reference session manager is single-process and serializes its short mock operations; replace it with a worker lease layer before running long blocking live-game operations.

## Stage into a local pinned checkout

```bash
python scripts/stage_nemo_adapter.py /path/to/NeMo-Gym
```

This writes `resources_servers/qudgym` only if it does not exist. The generated `requirements.txt` references this local project using a file URI so NeMo's isolated resource environment can install it; no public `qudgym` package is assumed. Do not commit that generated local absolute path into a shared repo. Build a wheel/internal package reference for cluster deployment later.

The included `configs/qudgym.yaml` uses the upstream `gymnasium_agent`, references your existing `policy_model`, and includes a small example dataset. Merge it with your pinned NeMo model-server configuration using that release's normal launcher. We have intentionally not guessed a deployment-specific NeMo CLI command, NIM endpoint, model name, image tag or GPU topology.

## Acceptance before declaring support

Run one scripted five-decision mock trajectory through the actual NeMo server/agent, then a model-driven rollout. Check session cookies, invalid-action behavior, cleanup on model failure/horizon/normal completion, and infrastructure-error reporting. Inspect the resulting rollout artifacts to confirm preserved model outputs/token accounting. Then run one real Qud task only after the live adapter exists.

NeMo Gym is the environment/rollout seam. NeMo RL is a later trainer consumer; NeMo Evaluator/microservices can orchestrate reporting once the task/verifier contract is validated. Platform product versions, NeMo Gym versions and NeMo RL versions are separate things—do not conflate them.

A candidate-pointer head is **not automatically** a supported NIM model or a token-policy GRPO/PPO objective. The core `CandidateScorer` interface permits it, but model serving and training integration require a separate verified implementation. The JSON-action baseline is just a useful initial NeMo-compatible harness.
