#!/usr/bin/env bash
# Delegate a bounded mock rollout entirely to a pinned NeMo Gym checkout.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: run_nemo_gym_mock.sh OUTPUT_DIR

Required environment:
  NEMO_GYM_ROOT             Pinned NVIDIA-NeMo/Gym checkout
  NEMO_GYM_MODEL            Served model name returned by /v1/models
  NEMO_GYM_MODEL_URL        Existing model base URL, including /v1
  NEMO_GYM_MODEL_API_KEY    Key matching the model endpoint

Optional environment:
  NEMO_GYM_MODEL_TYPE       Defaults to vllm_model
  NEMO_GYM_BIN              Defaults to $NEMO_GYM_ROOT/.venv/bin/gym
  NEMO_GYM_REPEATS          Defaults to 2; must be at least 2 for profiling
  NEMO_GYM_CONCURRENCY      Defaults to 1
  NEMO_GYM_HEAD_PORT        Defaults to 11000
  NEMO_GYM_INPUT            Defaults to the staged example.jsonl
  NEMO_GYM_ALLOW_COMMIT_DRIFT  Set to 1 only for an explicitly reviewed checkout
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

: "${NEMO_GYM_ROOT:?NEMO_GYM_ROOT is required}"
: "${NEMO_GYM_MODEL:?NEMO_GYM_MODEL is required}"
: "${NEMO_GYM_MODEL_URL:?NEMO_GYM_MODEL_URL is required}"
: "${NEMO_GYM_MODEL_API_KEY:?NEMO_GYM_MODEL_API_KEY is required}"
if [[ "$NEMO_GYM_MODEL_API_KEY" == *$'\n'* || "$NEMO_GYM_MODEL_API_KEY" == *$'\r'* ]]; then
  echo "NEMO_GYM_MODEL_API_KEY must not contain a newline" >&2
  exit 2
fi

EXPECTED_COMMIT="1c8261080bdc881b3e9b7f870e6418f160516991"
NEMO_GYM_ROOT=$(cd "$NEMO_GYM_ROOT" && pwd -P)
GYM_BIN=${NEMO_GYM_BIN:-"$NEMO_GYM_ROOT/.venv/bin/gym"}
WAIT_SCRIPT="$NEMO_GYM_ROOT/scripts/wait_for_servers.sh"
MODEL_TYPE=${NEMO_GYM_MODEL_TYPE:-vllm_model}
if [[ "$MODEL_TYPE" != "vllm_model" && "$MODEL_TYPE" != "openai_model" ]]; then
  echo "NEMO_GYM_MODEL_TYPE must be vllm_model (or openai_model for a contract fixture)" >&2
  exit 2
fi
REPEATS=${NEMO_GYM_REPEATS:-2}
CONCURRENCY=${NEMO_GYM_CONCURRENCY:-1}
HEAD_PORT=${NEMO_GYM_HEAD_PORT:-11000}
MODEL_URL=${NEMO_GYM_MODEL_URL%/}
OUTPUT_DIR=$1
GYM_PID=""
GYM_PGID=""

if [[ ! "$REPEATS" =~ ^[0-9]+$ ]] || (( REPEATS < 2 )); then
  echo "NEMO_GYM_REPEATS must be an integer of at least 2" >&2
  exit 2
fi
if [[ ! "$CONCURRENCY" =~ ^[0-9]+$ ]] || (( CONCURRENCY < 1 )); then
  echo "NEMO_GYM_CONCURRENCY must be a positive integer" >&2
  exit 2
fi
if [[ ! "$HEAD_PORT" =~ ^[0-9]+$ ]] || (( HEAD_PORT < 1 || HEAD_PORT > 65535 )); then
  echo "NEMO_GYM_HEAD_PORT must be in [1, 65535]" >&2
  exit 2
fi
if [[ ! -x "$GYM_BIN" ]]; then
  echo "NeMo Gym CLI is not executable: $GYM_BIN" >&2
  exit 2
fi
if [[ ! -f "$WAIT_SCRIPT" ]]; then
  echo "Missing NeMo Gym readiness script: $WAIT_SCRIPT" >&2
  exit 2
fi
if [[ ! -f "$NEMO_GYM_ROOT/resources_servers/qudgym/app.py" ]]; then
  echo "QudGym is not staged in $NEMO_GYM_ROOT; run scripts/stage_nemo_adapter.py first" >&2
  exit 2
fi

ACTUAL_COMMIT=$(git -C "$NEMO_GYM_ROOT" rev-parse HEAD)
if [[ "$ACTUAL_COMMIT" != "$EXPECTED_COMMIT" && "${NEMO_GYM_ALLOW_COMMIT_DRIFT:-0}" != "1" ]]; then
  echo "NeMo Gym commit mismatch: expected $EXPECTED_COMMIT, found $ACTUAL_COMMIT" >&2
  echo "Set NEMO_GYM_ALLOW_COMMIT_DRIFT=1 only after reviewing the API diff" >&2
  exit 2
fi
if [[ "${NEMO_GYM_ALLOW_COMMIT_DRIFT:-0}" != "1" ]] \
  && { ! git -C "$NEMO_GYM_ROOT" diff --quiet || ! git -C "$NEMO_GYM_ROOT" diff --cached --quiet; }; then
  echo "NeMo Gym checkout has tracked local changes; review them or set the explicit drift override" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUTPUT_DIR")"
mkdir "$OUTPUT_DIR"
OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd -P)
CAPTURE_DIR="$OUTPUT_DIR/model-calls"
mkdir "$CAPTURE_DIR"
ROLLOUTS="$OUTPUT_DIR/rollouts.jsonl"
MATERIALIZED="$OUTPUT_DIR/rollouts_materialized_inputs.jsonl"
AGGREGATE="$OUTPUT_DIR/rollouts_aggregate_metrics.json"
PROFILE="$OUTPUT_DIR/rollouts_reward_profiling.jsonl"
QUALITY="$OUTPUT_DIR/quality_summary.json"
SUMMARY="$OUTPUT_DIR/summary.json"

if [[ "${NEMO_GYM_INPUT:-resources_servers/qudgym/data/example.jsonl}" = /* ]]; then
  INPUT=${NEMO_GYM_INPUT}
else
  INPUT="$NEMO_GYM_ROOT/${NEMO_GYM_INPUT:-resources_servers/qudgym/data/example.jsonl}"
fi
if [[ ! -f "$INPUT" ]]; then
  echo "NeMo Gym input does not exist: $INPUT" >&2
  exit 2
fi

curl -fsS --connect-timeout 5 --max-time 15 \
  -H "Authorization: Bearer $NEMO_GYM_MODEL_API_KEY" \
  "$MODEL_URL/models" >"$OUTPUT_DIR/model-models.json"

terminate_group() {
  local signal=$1
  local pid=$2
  local pgid="${GYM_PGID:-}"
  if [[ -z "$pid" ]]; then
    return
  fi
  if [[ -z "$pgid" ]] && kill -0 "$pid" 2>/dev/null; then
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
  fi
  if [[ -n "$pgid" && "$pgid" == "$pid" ]]; then
    kill "-$signal" -- "-$pgid" 2>/dev/null || true
  elif kill -0 "$pid" 2>/dev/null; then
    kill "-$signal" "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$GYM_PID" ]] && { [[ -n "$GYM_PGID" ]] || kill -0 "$GYM_PID" 2>/dev/null; }; then
    terminate_group INT "$GYM_PID"
    for _ in $(seq 1 30); do
      if ! kill -0 "$GYM_PID" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if kill -0 "$GYM_PID" 2>/dev/null; then
      terminate_group TERM "$GYM_PID"
      sleep 2
    fi
    if kill -0 "$GYM_PID" 2>/dev/null; then
      terminate_group KILL "$GYM_PID"
    fi
    wait "$GYM_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

START_ARGS=(
  env start
  --resources-server qudgym
  --model-type "$MODEL_TYPE"
  --model "$NEMO_GYM_MODEL"
  --model-url "$MODEL_URL"
  --model-api-key "$NEMO_GYM_MODEL_API_KEY"
  "++head_server.port=$HEAD_PORT"
  "++observability_enabled=true"
  "++model_call_capture_dir=$CAPTURE_DIR"
  "++upload_rollouts=false"
)
if [[ "$MODEL_TYPE" == "vllm_model" && "${NEMO_GYM_USES_REASONING_PARSER:-false}" != "true" ]]; then
  START_ARGS+=("++policy_model.responses_api_models.vllm_model.uses_reasoning_parser=false")
fi

python3 - "$GYM_BIN" "${START_ARGS[@]}" >"$OUTPUT_DIR/gym-env-start.log" 2>&1 <<'PY' &
import os
import sys

try:
    os.setsid()
except PermissionError:
    os.setpgid(0, 0)
os.execv(sys.argv[1], sys.argv[1:])
PY
GYM_PID=$!
GYM_PGID=$(ps -o pgid= -p "$GYM_PID" 2>/dev/null | tr -d ' ')
if [[ "$GYM_PGID" != "$GYM_PID" ]]; then
  GYM_PGID=""
fi
bash "$WAIT_SCRIPT" "$GYM_PID" "$HEAD_PORT" "${NEMO_GYM_READY_TIMEOUT_SECONDS:-240}"

"$GYM_BIN" eval run --no-serve \
  --agent qudgym_agent \
  --input "$INPUT" \
  --output "$ROLLOUTS" \
  --limit 1 \
  --num-repeats "$REPEATS" \
  --concurrency "$CONCURRENCY" \
  --temperature 0 \
  --top-p 1 \
  --max-output-tokens 256 \
  "++head_server.port=$HEAD_PORT" \
  "++observability_enabled=true" \
  "++model_call_capture_dir=$CAPTURE_DIR" \
  "++upload_rollouts=false" \
  >"$OUTPUT_DIR/gym-eval-run.log" 2>&1

"$GYM_BIN" eval profile \
  --inputs "$MATERIALIZED" \
  --rollouts "$ROLLOUTS" \
  >"$OUTPUT_DIR/gym-eval-profile.log" 2>&1

python3 - "$ROLLOUTS" "$AGGREGATE" "$PROFILE" "$QUALITY" "$SUMMARY" <<'PY'
import json
import pathlib
import sys

rollouts_path, aggregate_path, profile_path, quality_path, summary_path = map(pathlib.Path, sys.argv[1:])
rollouts = [json.loads(line) for line in rollouts_path.read_text(encoding="utf-8").splitlines() if line]
aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
profile_rows = [json.loads(line) for line in profile_path.read_text(encoding="utf-8").splitlines() if line]
quality = json.loads(quality_path.read_text(encoding="utf-8"))
if not rollouts:
    raise SystemExit("NeMo Gym produced no rollout rows")
if not all(row.get("info", {}).get("is_mock") is True for row in rollouts):
    raise SystemExit("NeMo Gym rollout lost the explicit is_mock=true boundary")
if not all(row.get("agent_ref", {}).get("name") == "qudgym_agent" for row in rollouts):
    raise SystemExit("NeMo Gym rollout used an unexpected agent instance")
native_key_metrics = {
    entry.get("agent_ref", {}).get("name", "unknown"): entry.get("key_metrics", {})
    for entry in aggregate
}
summary = {
    "is_mock": True,
    "live_qud": False,
    "upload_rollouts": False,
    "rollout_count": len(rollouts),
    "profile_row_count": len(profile_rows),
    "native_health_verdicts": quality.get("run", {}).get("verdicts"),
    "native_key_metrics": native_key_metrics,
    "artifacts": {
        "rollouts": rollouts_path.name,
        "materialized_inputs": "rollouts_materialized_inputs.jsonl",
        "aggregate_metrics": aggregate_path.name,
        "reward_profile": profile_path.name,
        "quality_summary": quality_path.name,
        "rollout_verdicts": "rollout_verdicts.jsonl",
        "model_call_capture_dir": "model-calls",
    },
    "atif_export": "not_attempted_current_gymnasium_agent_trajectory_has_coverage_gaps",
}
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({
    "is_mock": True,
    "rollout_count": len(rollouts),
    "profile_row_count": len(profile_rows),
    "summary": summary_path.name,
}, sort_keys=True))
PY
