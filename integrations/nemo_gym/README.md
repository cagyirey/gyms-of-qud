# Native NeMo Gym integration

**Mock backend only.** This adapter is not a live Caves of Qud backend and a
successful rollout is not a Qud evaluation.

The runtime contract is pinned to
[`NVIDIA-NeMo/Gym@1c826108`](https://github.com/NVIDIA-NeMo/Gym/tree/1c8261080bdc881b3e9b7f870e6418f160516991)
(NeMo Gym `0.7.0rc0`, Python `3.13.14+`). The NeMo Gym dependency is isolated
from QudGym's Python 3.11+ foundation package.

## Architecture

```text
existing Responses/Chat model endpoint
  -> NeMo Gym vllm_model
  -> upstream responses_api_agents/gymnasium_agent
  -> QudGym resources_servers/qudgym
  -> bounded QudGym mock backend
```

QudGym owns only the environment session, visible observation, candidate
validation, transition, reward, and explicit `is_mock=true` label. NeMo Gym
owns the episode loop, model calls, usage, rollout persistence, aggregate
metrics, reward profiling, model-call capture, and runtime telemetry. There is
no second policy loop, model gateway, tokenizer, trainer, or token accounting
layer in this repository.

`max_steps` in the agent config and `max_decisions` in a task row are separate
bounds. A zero-turn prompt still consumes one resolved decision.

## Stage the resource server

From this repository, stage once into the pinned NeMo Gym checkout:

```bash
python scripts/stage_nemo_adapter.py /path/to/NeMo-Gym
```

The helper is create-only and refuses a non-Git checkout, a commit other
than the pinned SHA, or tracked local modifications. Its generated local
`requirements.txt` installs both the pinned editable NeMo Gym checkout and
this QudGym checkout into the resource server's isolated environment. Absolute
`file://` paths exist only in that local generated file and are never committed.
Set `NEMO_GYM_ALLOW_COMMIT_DRIFT=1` only for an explicitly reviewed API update.

NeMo Gym's `--search-dir`/external-root mechanism is the eventual plugin
packaging path once QudGym is published as a wheel or internal package. Until
then, staging is the reproducible local path for this unpublished dependency;
do not copy generated local requirements into a Platform FileSet.

The included config is self-contained:

- resources server instance: `qudgym_resources_server`;
- Gymnasium agent instance: `qudgym_agent`;
- model server instance: `policy_model`;
- task budget: 16 decisions / at most 16 model calls;
- closed-session replay cache: 256 sessions / 300 seconds by default;
- compatibility declaration: `allowed_agents: [gymnasium_agent]`;
- verification state: `verified: false`.

Validate before starting services:

```bash
/path/to/NeMo-Gym/.venv/bin/gym env validate \
  --resources-server qudgym \
  --model-type vllm_model \
  --model contract-smoke \
  --model-url http://127.0.0.1:8000/v1 \
  --model-api-key dummy
```

Validation does not contact the model or prove a rollout.

## Local quantized model path

Start and manage vLLM separately, then use NeMo Gym's `vllm_model` adapter. The
wrapper converts NeMo Gym's Responses API requests to the vLLM Chat Completions
endpoint and converts responses back. Quantization belongs to the vLLM launch
or checkpoint; it is not a QudGym or Gym model-server setting.

For example, a prequantized local checkpoint can be served with the exact vLLM
options supported by that checkpoint:

```bash
vllm serve /path/to/quantized-checkpoint \
  --served-model-name qudgym-policy \
  --host 127.0.0.1 \
  --port 8000
```

Confirm the served name before running Gym:

```bash
curl -fsS http://127.0.0.1:8000/v1/models
```

Use `local_vllm_model` only when NeMo Gym should launch and manage the vLLM
engine itself; the wrapper intentionally does not use that mode because it is
for an already-running external endpoint. Do not point `local_vllm_model` at a
separately managed server.

This first path is an evaluation/behavior-cloning artifact. It does not turn
ATOF or Gym JSONL into token-level PPO/GRPO data. A future NeMo RL run must use
the native training model-server configuration that preserves the provider's
prompt/generation token IDs and log probabilities, then validate that lineage
separately.

## Bounded native rollout

`scripts/run_nemo_gym_mock.sh` is a thin lifecycle wrapper around the official
`gym` commands. It does not parse observations, select actions, perform model
inference, calculate rewards, or create training examples.

```bash
NEMO_GYM_ROOT=/path/to/NeMo-Gym \
NEMO_GYM_MODEL=qudgym-policy \
NEMO_GYM_MODEL_URL=http://127.0.0.1:8000/v1 \
NEMO_GYM_MODEL_API_KEY=dummy \
NEMO_GYM_REPEATS=5 \
NEMO_GYM_CONCURRENCY=1 \
scripts/run_nemo_gym_mock.sh local/nemo-gym-qwen
```

The output directory is create-only. The wrapper:

1. verifies the pinned NeMo Gym commit;
2. verifies the staged resource server and model `/models` endpoint;
3. starts `gym env start` and uses NVIDIA's readiness script;
4. runs `gym eval run --no-serve` with temperature 0, top-p 1, and a 256-token
   output limit;
5. runs native `gym eval profile`;
6. disables ambient W&B/MLflow rollout export with NeMo Gym's
   `upload_rollouts=false` safety default;
7. validates that every row is explicitly mock and routed through
   `qudgym_agent`;
8. writes a compact `summary.json` that references native metrics and
   artifacts;
9. gracefully interrupts the Gym process group.

Set `NEMO_GYM_ALLOW_COMMIT_DRIFT=1` only after reviewing an intentional NeMo Gym
API update. Set `NEMO_GYM_USES_REASONING_PARSER=true` only when the external
vLLM server was launched with the matching reasoning parser. The optional
`NEMO_GYM_MODEL_TYPE=openai_model` mode is only for an endpoint that genuinely
implements the Responses API; it is not a Chat Completions substitute.

The output includes:

- `rollouts.jsonl` — authoritative NeMo Gym rollout records;
- `rollouts_materialized_inputs.jsonl` — exact task/rollout expansion;
- `rollouts_aggregate_metrics.json` — native aggregate metrics;
- `rollouts_reward_profiling.jsonl` and related repeat metrics;
- `quality_summary.json` and `rollout_verdicts.jsonl` — native rollout-health
  verdicts (the current agent may legitimately report checks as unobserved
  when producer turn/tool evidence is unavailable);
- `model-calls/` — opt-in `ng_model_call_capture` source records;
- `summary.json` — mock-label and artifact readback, not a second metric owner;
- logs and the model `/models` response.

Do not point two concurrent jobs at one capture directory. NeMo Gym rollout IDs
do not include an evaluation-run ID. The wrapper explicitly sets
`upload_rollouts=false` so an ambient `.env.yaml` cannot silently send mock
observations or model responses to W&B/MLflow. If remote experiment export is
wanted, review the payload and re-enable it as a separate operator action.
Do not reinterpret native health checks as healthy when they are `unobserved`;
inspect the named coverage gaps and keep that limitation attached to the run.

## Model-call evidence and OpenTelemetry

Model-call capture and runtime OpenTelemetry are separate NeMo features. The
wrapper enables model-call capture. To emit application traces, configure
NeMo Gym's native telemetry before the wrapper starts the servers:

```bash
NEMO_GYM_OTEL_ENABLED=1 \
NEMO_GYM_OTEL_EXPORTER=console \
NEMO_GYM_MODEL_TYPE=vllm_model \
NEMO_GYM_ROOT=/path/to/NeMo-Gym \
NEMO_GYM_MODEL=qudgym-policy \
NEMO_GYM_MODEL_URL=http://127.0.0.1:8000/v1 \
NEMO_GYM_MODEL_API_KEY=dummy \
scripts/run_nemo_gym_mock.sh local/nemo-gym-traced
```

Use NeMo Gym's OTLP configuration only when an operator-owned collector is
available. QudGym's standalone ATOF/GenAI projection is not attached to the
Gym adapter; see [`docs/RECORDING.md`](../../docs/RECORDING.md) for the ownership
boundary.

## ATIF and NeMo Platform/Studio

Current NeMo Gym `gym eval export` is a strict `ng_trajectory` -> ATIF v1.7
converter. The current `gymnasium_agent` does not emit the producer turn and
invocation evidence required by that converter; a real QudGym rollout records
coverage gaps and is rejected. The wrapper therefore does not attempt ATIF
export as an acceptance gate.

Do not turn later environment observations into fake tool calls, flatten a
failed export, or call the result a complete trajectory. Native Gym JSONL and
reward profiling are the first mock artifacts.

NeMo Platform/Studio does not directly launch a NeMo Gym `GymnasiumServer`.
Studio is the web UI; the verified local execution path remains the NeMo Gym
CLI. A later Studio integration may ingest a complete ATIF trajectory through
NeMo Platform Intake, but it must first:

1. create an Experiment Group and Evaluation;
2. attach `evaluation_context` to each per-rollout ATIF payload;
3. POST to the documented `/apis/intake/v2/.../ingest/atif` endpoint;
4. verify `run_count` and evaluator scores before viewing them in Studio.

That path remains blocked on complete ATIF support for this Gymnasium agent.
Registering a model provider with NeMo Platform can make the same vLLM model
visible in Studio, but it does not turn QudGym into a Platform-managed agent.

## Validation status

A local contract smoke used the pinned NeMo Gym resource, agent, and model
servers with a deterministic test-only model. Both the direct Responses model
server and the intended external-server `vllm_model` bridge completed two
native repeats. Each rollout returned reward `+1`, `outcome=success`,
`is_mock=true`, four game turns, and five decisions; native trajectory
attachments and reward-profile files were created. `gym eval export` rejected
both rows because `ng_trajectory` contained coverage gaps.

This validates plumbing and the mock success contract only. The native CI
job repeats the staged lifecycle with the committed test-only model fixture. No
small quantized model inference, Caves of Qud process, Platform mutation,
Studio upload, live Qud API, save mutation, or NeMo RL training run has been
executed.
