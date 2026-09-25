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
than the pinned SHA, or any tracked/untracked local changes outside the exact
staging tree. The committed `source_manifest.json` verifies every executable
adapter/config/data file before staging and again before a run; the generated
resource-server virtual environment and interpreter caches are treated as
runtime artifacts. Its generated local `requirements.txt` installs both the
pinned editable NeMo Gym checkout and this QudGym checkout into the resource
server's isolated environment. Absolute `file://` paths exist only in that
local generated file and are never committed. Set
`NEMO_GYM_ALLOW_COMMIT_DRIFT=1` only for an explicitly reviewed API update.
After changing adapter source, regenerate and review the manifest with
`python scripts/verify_nemo_adapter.py --write-source-manifest`.

NeMo Gym's `--search-dir`/external-root mechanism is the eventual plugin
packaging path once QudGym is published as a wheel or internal package. Until
then, staging is the reproducible local path for this unpublished dependency;
do not copy generated local requirements into a Platform FileSet.

The included config is self-contained:

- resources server instance: `qudgym_resources_server`;
- Gymnasium agent instance: `qudgym_agent`;
- model server instance: `policy_model`;
- task budget: 16 decisions / at most 16 model calls;
- resource-server workers: exactly 1 (session/replay state is process-local);
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
# Keep the key in the operator's secret/environment manager.
export POLICY_ENDPOINT_KEY=...  # existing Studio/NeMo credential, not committed
NEMO_GYM_ROOT=/path/to/NeMo-Gym \
NEMO_GYM_MODEL=qudgym-policy \
NEMO_GYM_MODEL_URL=http://127.0.0.1:8000/v1 \
NEMO_GYM_MODEL_API_KEY_ENV=POLICY_ENDPOINT_KEY \
NEMO_GYM_REPEATS=5 \
NEMO_GYM_CONCURRENCY=1 \
scripts/run_nemo_gym_mock.sh local/nemo-gym-qwen
```

For an unauthenticated local fixture, `NEMO_GYM_MODEL_API_KEY=dummy` is also
accepted. The wrapper resolves the key through an environment-backed NeMo Gym
override; it does not place the raw value in the Gym command line or Hydra
override file.

The output directory is create-only. The wrapper:

1. verifies the pinned NeMo Gym commit and the committed adapter manifest;
2. verifies the staged resource server and model `/models` endpoint;
3. starts `gym env start` and uses NVIDIA's readiness script;
4. runs `gym eval run --no-serve` with temperature 0, top-p 1, and a 256-token
   output limit;
5. runs native `gym eval profile`;
6. disables W&B exporter setup and, by default, MLflow exporter setup and raw
   rollout export with explicit native config overrides, including
   `upload_rollouts=false`;
7. requires the configured repeat count, terminal mock success, four turns,
   five decisions, and complete reward-profile joins by default;
8. validates that every row is explicitly mock and routed through
   `qudgym_agent`;
9. writes a compact `summary.json` that references native metrics and
   artifacts;
10. gracefully interrupts the Gym process group.

Set `NEMO_GYM_ALLOW_COMMIT_DRIFT=1` only after reviewing an intentional NeMo Gym
API update. Set `NEMO_GYM_REQUIRE_SUCCESS=0` only for a deliberately different
mock task; the default success/profile assertions are part of this wrapper's
contract. Set `NEMO_GYM_USES_REASONING_PARSER=true` only when the external
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
do not include an evaluation-run ID. The wrapper rejects unexpected untracked
NeMo checkout paths and verifies the staged adapter manifest. By default it
explicitly nulls W&B and MLflow exporter availability fields in the lifecycle
commands and keeps `upload_rollouts=false`, so no remote experiment data is sent
by this local workflow. The wrapper uses `umask 077` for raw model-call captures
and local artifacts. Do not reinterpret native health checks as healthy when
they are `unobserved`; inspect the named coverage gaps and keep that limitation
attached to the run.

### Optional native MLflow tracking

For a local REST sink, this repository follows the Geodesic/Dennou modular
Compose shape. The tracking profile is opt-in and is not part of the default
mock lifecycle:

```bash
docker compose --profile tracking up -d --wait mlflow
# Host-side wrapper (after the service reports healthy):
NEMO_GYM_MLFLOW_TRACKING_URI=http://127.0.0.1:5001
# Container-side client on the Compose network:
# NEMO_GYM_MLFLOW_TRACKING_URI=http://mlflow:8080
```

The Compose service uses a pinned MLflow image, a named local volume, a loopback
port binding (host port `5001` by default to avoid the common macOS port-5000
conflict), and a healthcheck. Set `QUDGYM_MLFLOW_PORT` to choose another host
port. It contains no credentials; use an operator-owned proxy or secret
reference if the tracking endpoint needs auth.

The tracking URI must not contain userinfo, query parameters, or a fragment;
put credentials in the operator-owned token environment instead. MLflow is
enabled only when the operator sets all of the following environment variables:

```bash
# Populate MLFLOW_TRACKING_TOKEN through the operator's secret manager if needed.
NEMO_GYM_MLFLOW_ENABLED=1 \
NEMO_GYM_MLFLOW_TRACKING_URI=http://127.0.0.1:5001 \
NEMO_GYM_MLFLOW_EXPERIMENT_NAME=qudgym-mock \
NEMO_GYM_MLFLOW_RUN_NAME=operator-approved-name \
NEMO_GYM_MLFLOW_TOKEN_ENV=MLFLOW_TRACKING_TOKEN \
NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS=0 \
scripts/run_nemo_gym_mock.sh local/nemo-gym-mlflow
```

`NEMO_GYM_MLFLOW_TOKEN_ENV` is optional for an unauthenticated local server. When
present, it is an environment-variable **name**, not a token; the wrapper keeps
the value out of command arguments and Hydra override files, then removes the
source variable from the lifecycle environment. Only the `gym eval run`
subshell receives the scoped copy. The wrapper applies the same source-secret
scrubbing to the model-key source selected by `NEMO_GYM_MODEL_API_KEY` or
`NEMO_GYM_MODEL_API_KEY_ENV`. The native NeMo
Gym MLflow exporter records the run configuration and aggregate metrics during
`gym eval run`; that config artifact can contain local paths and endpoint
metadata, so keep the sink local or review it before using a remote tracker.
The wrapper summary records the MLflow request and rollout-upload request, not
an independent exporter-success readback; inspect the native sink and logs for
that confirmation. The `gym env start` process deliberately keeps MLflow disabled
so it does not create an empty setup run. The separate `gym eval profile`
command remains a local artifact check; it does not create a second MLflow run.
W&B remains disabled.

`NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS=0` is the safe default even when MLflow is
enabled. Set it to `1` only after reviewing the native rollout export payload
and the operator's retention/access policy; that upload is a separate data
disclosure decision, not part of metrics tracking. The exporter's rollout view
is upstream NeMo Gym's sanitized rollout record, not a new QudGym/ATIF format.
A local MLflow sink can be used for validation without sending mock data to a
remote service.

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
both rows because `ng_trajectory` contained coverage gaps. The full pinned
NeMo environment suite also passes, including the real imported adapter tests.

This validates plumbing and the mock success contract only. The native CI
job repeats the staged lifecycle with the committed test-only model fixture. No
small quantized model inference, Caves of Qud process, Platform mutation,
Studio upload, live Qud API, save mutation, or NeMo RL training run has been
executed.
