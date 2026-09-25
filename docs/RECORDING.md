# Session recording and observability

QudGym records **sanitized environment/session events**, not privileged Qud
state. The current implementation works with the mock environment and exposes
an adapter-friendly recording boundary; it is not a live Qud transport or a
claim that live gameplay is supported.

## Source format: ATOF 0.1

`SessionRecorder` writes one JSON object per line using the Agent Trajectory
Observability Format (ATOF) 0.1 envelope:

- one root `agent` scope for the session;
- a `function` scope for each resolved decision;
- a child `tool` scope named `qudgym.step` for the environment mutation;
- marks for reset, reconciliation, and caller-supplied boundaries;
- no hidden entities, blueprints, PRNG state, snapshots, save data, or
  credentials.

`record_mark()` and the model-call context manager are explicit caller
boundaries: pass already-redacted data. The recorder does not make an
arbitrary caller payload safe by inspecting game state.

The recorder is create-only, flushed after each event, and bounded by both
`max_events` and `max_bytes`. A transport-uncertain mutation is recorded as an
error; this layer never retries it with a new request ID. Observations are
the already-allowlisted `QudEnv` values.

Record the deterministic smoke session as ATOF:

```bash
mkdir -p local
qudgym smoke --atof local/mock-session.atof.jsonl
qudgym atof-validate local/mock-session.atof.jsonl
```

The path is intentionally create-only and belongs under ignored `local/` or
another reviewed local output directory. The ATOF stream is the durable
interchange boundary; it is not itself ATIF or OTLP.

## ATIF export

ATIF conversion is delegated to NVIDIA NeMo Agent Toolkit's native
`nat.atof.scripts.atof_to_atif_converter`; QudGym does not maintain a second
ATIF schema or a second trainer/evaluator. Install the optional dependency and
convert a stream with:

```bash
python -m pip install 'qudgym[atif]'
qudgym export-atif local/mock-session.atof.jsonl \
  --output local/mock-session.atif.json
```

The converter currently targets ATIF v1.7 in the NeMo 1.8 API (Python
3.11–3.13). The exact output must be reviewed before sharing: prompts,
observations, and action arguments can be sensitive even though the QudGym
observation itself is
sanitized. This command does not claim that a Qud session occurred; the mock
backend is labeled as mock in its transition data.

## GenAI OpenTelemetry projection

For a non-NeMo runner, `export_genai_otel()` reads the ATOF stream and emits
GenAI semantic-convention spans through the host's OpenTelemetry API provider:

- root agent → `invoke_agent`;
- decision orchestration → `invoke_workflow`;
- `qudgym.step` → `execute_tool`.

The function does not configure a global provider, choose an OTLP endpoint,
send network data, or calculate token/cost metrics. The host owns the provider
and exporter. Install the API/SDK pair for a standalone runner (or keep the
API-only dependency when the host already provides the SDK):

```bash
python -m pip install 'qudgym[telemetry]'
```

If a NeMo/Relay runtime is already present, prefer its native ATIF and
OpenTelemetry exporters so the model/tool event stream has one owner. QudGym
supplies the environment ATOF stream and correlation metadata rather than
intercepting model prompts or implementing token accounting.

For a runner that owns the model call, the recorder also exposes a typed
context manager. It records the model/provider identity and provider-reported
usage without calculating tokens or costs:

```python
with recorder.llm_call(
    model_name="provider/model",
    provider_name="provider",
    input_messages=[{"role": "user", "content": "..."}],
) as call:
    response = model.invoke(...)
    call.output_text = response.text
    call.response_id = response.id
    call.usage = response.usage
```

Set `RecordingConfig(capture_content=True)` only when the session is approved
for content capture. The default keeps prompts and completions out of both the
ATOF LLM payload and direct OTel attributes; model/provider metadata and
provider-supplied usage remain available for correlation.

## Live boundary

Neither the diagnostic mod nor the recorder authorizes live Qud control.
Before connecting a live adapter, the project still needs evidence-backed
read-only observation, explicit turn-thread ownership, cancellation/fault
handling, and a dedicated-profile save-integrity check. Snapshot/restore and
full-state-hash capabilities remain disabled for live Qud.
