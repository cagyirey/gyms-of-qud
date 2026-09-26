#!/usr/bin/env bash
# Delegate a bounded mock rollout entirely to a pinned NeMo Gym checkout.
set -euo pipefail
umask 077

usage() {
  cat >&2 <<'EOF'
Usage: run_nemo_gym_mock.sh OUTPUT_DIR

Host requirements:
  bash, python3, curl, lsof   lsof proves the ready head belongs to this run

Required environment:
  NEMO_GYM_ROOT             Pinned NVIDIA-NeMo/Gym checkout
  NEMO_GYM_MODEL            Served model name returned by /v1/models
  NEMO_GYM_MODEL_URL        Existing model base URL, including /v1
  NEMO_GYM_MODEL_API_KEY    Key matching the model endpoint (or use the *_ENV form)
  NEMO_GYM_MODEL_API_KEY_ENV  Environment variable name holding the model key

Optional environment:
  NEMO_GYM_MODEL_TYPE       Defaults to vllm_model
  NEMO_GYM_BIN              Defaults to $NEMO_GYM_ROOT/.venv/bin/gym
  NEMO_GYM_REPEATS          Defaults to 2; must be at least 2 for profiling
  NEMO_GYM_CONCURRENCY      Defaults to 1
  NEMO_GYM_HEAD_PORT        Defaults to 11000; must be free and owned by this run
  NEMO_GYM_INPUT            Defaults to the staged example.jsonl
  NEMO_GYM_REQUIRE_SUCCESS  Defaults to 1; set 0 for real-model evaluation or alternate tasks
  NEMO_GYM_MLFLOW_ENABLED  Defaults to 0; set 1 for native MLflow metrics/config
  NEMO_GYM_MLFLOW_TRACKING_URI  MLflow tracking URI when enabled; no userinfo/query/fragment
  NEMO_GYM_MLFLOW_EXPERIMENT_NAME  MLflow experiment name when enabled
  NEMO_GYM_MLFLOW_RUN_NAME  MLflow run name when enabled
  NEMO_GYM_MLFLOW_TOKEN_ENV  Optional env var name holding an MLflow token
  NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS  Defaults to 0; raw rows require explicit opt-in
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
API_KEY_ENV=${NEMO_GYM_MODEL_API_KEY_ENV:-}
if [[ -n "$API_KEY_ENV" ]]; then
  if [[ ! "$API_KEY_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "NEMO_GYM_MODEL_API_KEY_ENV must be an environment variable name" >&2
    exit 2
  fi
  API_KEY_VALUE=$(printenv "$API_KEY_ENV" || true)
  if [[ -z "$API_KEY_VALUE" ]]; then
    echo "The environment variable named by NEMO_GYM_MODEL_API_KEY_ENV is unset or empty" >&2
    exit 2
  fi
else
  : "${NEMO_GYM_MODEL_API_KEY:?NEMO_GYM_MODEL_API_KEY or NEMO_GYM_MODEL_API_KEY_ENV is required}"
  API_KEY_VALUE=$NEMO_GYM_MODEL_API_KEY
fi
if [[ "$API_KEY_VALUE" == *$'\n'* || "$API_KEY_VALUE" == *$'\r'* ]]; then
  echo "The model API key must not contain a newline" >&2
  exit 2
fi
# Do not let the caller's source secret remain inherited by unrelated Gym child
# processes. The wrapper-owned environment variable below is the only copy
# needed by the native config parser.
if [[ -n "$API_KEY_ENV" ]]; then
  unset "$API_KEY_ENV"
fi
unset NEMO_GYM_MODEL_API_KEY
# Keep the raw value out of process arguments and Hydra override files. NeMo Gym
# resolves this environment-backed value in its native config parser.
export NEMO_GYM_WRAPPER_POLICY_KEY="$API_KEY_VALUE"
API_KEY_START_ARGS=("++policy_api_key=\${oc.env:NEMO_GYM_WRAPPER_POLICY_KEY}")
API_KEY_EVAL_ARGS=("${API_KEY_START_ARGS[@]}")

EXPECTED_COMMIT="1c8261080bdc881b3e9b7f870e6418f160516991"
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
NEMO_GYM_ROOT=$(cd "$NEMO_GYM_ROOT" && pwd -P)
STAGED_ADAPTER="$NEMO_GYM_ROOT/resources_servers/qudgym"
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
REQUIRE_SUCCESS=${NEMO_GYM_REQUIRE_SUCCESS:-1}
MLFLOW_ENABLED=${NEMO_GYM_MLFLOW_ENABLED:-0}
MLFLOW_UPLOAD_ROLLOUTS=${NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS:-0}
MLFLOW_TOKEN_ENV=${NEMO_GYM_MLFLOW_TOKEN_ENV:-}
MLFLOW_TOKEN_VALUE=""
if [[ "$MLFLOW_ENABLED" != "0" && "$MLFLOW_ENABLED" != "1" ]]; then
  echo "NEMO_GYM_MLFLOW_ENABLED must be 0 or 1" >&2
  exit 2
fi
if [[ "$MLFLOW_UPLOAD_ROLLOUTS" != "0" && "$MLFLOW_UPLOAD_ROLLOUTS" != "1" ]]; then
  echo "NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS must be 0 or 1" >&2
  exit 2
fi
if [[ "$MLFLOW_ENABLED" == "0" && "$MLFLOW_UPLOAD_ROLLOUTS" == "1" ]]; then
  echo "NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS=1 requires NEMO_GYM_MLFLOW_ENABLED=1" >&2
  exit 2
fi
if [[ "$MLFLOW_ENABLED" == "1" ]]; then
  if [[ -z "${NEMO_GYM_MLFLOW_TRACKING_URI:-}" ]]; then
    echo "NEMO_GYM_MLFLOW_TRACKING_URI is required when MLflow is enabled" >&2
    exit 2
  fi
  if [[ -z "${NEMO_GYM_MLFLOW_EXPERIMENT_NAME:-}" ]]; then
    echo "NEMO_GYM_MLFLOW_EXPERIMENT_NAME is required when MLflow is enabled" >&2
    exit 2
  fi
  if [[ -z "${NEMO_GYM_MLFLOW_RUN_NAME:-}" ]]; then
    echo "NEMO_GYM_MLFLOW_RUN_NAME is required when MLflow is enabled" >&2
    exit 2
  fi
  for value in "$NEMO_GYM_MLFLOW_TRACKING_URI" "$NEMO_GYM_MLFLOW_EXPERIMENT_NAME" "$NEMO_GYM_MLFLOW_RUN_NAME"; do
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
      echo "MLflow configuration values must not contain newlines" >&2
      exit 2
    fi
  done
  if ! python3 -c '
import sys
from urllib.parse import urlsplit

uri = sys.stdin.read()
parsed = urlsplit(uri)
if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
    raise SystemExit(1)
' <<< "$NEMO_GYM_MLFLOW_TRACKING_URI"; then
    echo "NEMO_GYM_MLFLOW_TRACKING_URI must not contain userinfo, query, or fragment credentials" >&2
    exit 2
  fi
  export NEMO_GYM_WRAPPER_MLFLOW_TRACKING_URI="$NEMO_GYM_MLFLOW_TRACKING_URI"
  export NEMO_GYM_WRAPPER_MLFLOW_EXPERIMENT_NAME="$NEMO_GYM_MLFLOW_EXPERIMENT_NAME"
  export NEMO_GYM_WRAPPER_MLFLOW_RUN_NAME="$NEMO_GYM_MLFLOW_RUN_NAME"
  if [[ -n "$MLFLOW_TOKEN_ENV" ]]; then
    if [[ ! "$MLFLOW_TOKEN_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "NEMO_GYM_MLFLOW_TOKEN_ENV must be an environment variable name" >&2
      exit 2
    fi
    MLFLOW_TOKEN_VALUE=$(printenv "$MLFLOW_TOKEN_ENV" || true)
    if [[ -z "$MLFLOW_TOKEN_VALUE" ]]; then
      echo "The environment variable named by NEMO_GYM_MLFLOW_TOKEN_ENV is unset or empty" >&2
      exit 2
    fi
    if [[ "$MLFLOW_TOKEN_VALUE" == *$'\n'* || "$MLFLOW_TOKEN_VALUE" == *$'\r'* ]]; then
      echo "The MLflow token must not contain a newline" >&2
      exit 2
    fi
    # Scope the secret to the eval subshell below; do not leave the caller's
    # source variable inherited by env-start, profile, or model children.
    unset "$MLFLOW_TOKEN_ENV"
    MLFLOW_TOKEN_OVERRIDE=("++mlflow_tracking_token=\${oc.env:NEMO_GYM_WRAPPER_MLFLOW_TOKEN}")
  else
    MLFLOW_TOKEN_OVERRIDE=("++mlflow_tracking_token=null")
  fi
else
  MLFLOW_UPLOAD_ROLLOUTS=0
  MLFLOW_TOKEN_OVERRIDE=("++mlflow_tracking_token=null")
fi
MODEL_URL=${NEMO_GYM_MODEL_URL%/}
OUTPUT_DIR=$1
GYM_PID=""
GYM_PGID=""
ACTIVE_PID=""
ACTIVE_PGID=""

if [[ ! "$REPEATS" =~ ^[0-9]+$ ]] || (( REPEATS < 2 )); then
  echo "NEMO_GYM_REPEATS must be an integer of at least 2" >&2
  exit 2
fi
if [[ ! "$CONCURRENCY" =~ ^[0-9]+$ ]] || (( CONCURRENCY < 1 )); then
  echo "NEMO_GYM_CONCURRENCY must be a positive integer" >&2
  exit 2
fi
if [[ "$REQUIRE_SUCCESS" != "0" && "$REQUIRE_SUCCESS" != "1" ]]; then
  echo "NEMO_GYM_REQUIRE_SUCCESS must be 0 or 1" >&2
  exit 2
fi
if [[ ! "$HEAD_PORT" =~ ^[0-9]+$ ]] || (( HEAD_PORT < 1 || HEAD_PORT > 65535 )); then
  echo "NEMO_GYM_HEAD_PORT must be in [1, 65535]" >&2
  exit 2
fi
if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof is required to verify NEMO_GYM_HEAD_PORT ownership" >&2
  exit 2
fi
head_port_available() {
  python3 - "$HEAD_PORT" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}
if ! head_port_available; then
  echo "NEMO_GYM_HEAD_PORT $HEAD_PORT is already in use" >&2
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
if [[ ! -f "$STAGED_ADAPTER/app.py" ]]; then
  echo "QudGym is not staged in $NEMO_GYM_ROOT; run scripts/stage_nemo_adapter.py first" >&2
  exit 2
fi
python3 "$PROJECT_ROOT/scripts/verify_nemo_adapter.py" \
  --project-root "$PROJECT_ROOT" \
  --check-staged \
  --clean-caches \
  --nemo-root "$NEMO_GYM_ROOT"

ACTUAL_COMMIT=$(git -C "$NEMO_GYM_ROOT" rev-parse HEAD)
if [[ "$ACTUAL_COMMIT" != "$EXPECTED_COMMIT" && "${NEMO_GYM_ALLOW_COMMIT_DRIFT:-0}" != "1" ]]; then
  echo "NeMo Gym commit mismatch: expected $EXPECTED_COMMIT, found $ACTUAL_COMMIT" >&2
  echo "Set NEMO_GYM_ALLOW_COMMIT_DRIFT=1 only after reviewing the API diff" >&2
  exit 2
fi
if [[ "${NEMO_GYM_ALLOW_COMMIT_DRIFT:-0}" != "1" ]]; then
  if ! git -C "$NEMO_GYM_ROOT" diff --quiet || ! git -C "$NEMO_GYM_ROOT" diff --cached --quiet; then
    echo "NeMo Gym checkout has tracked local changes; review them or set the explicit drift override" >&2
    exit 2
  fi
  while IFS= read -r status_line; do
    [[ -z "$status_line" ]] && continue
    status_path=$status_line
    case "$status_path" in
      resources_servers/qudgym|resources_servers/qudgym/*) ;;
      *)
        echo "NeMo Gym checkout has an unexpected untracked path: $status_path" >&2
        echo "Only the verified resources_servers/qudgym staging tree is allowed" >&2
        exit 2
        ;;
    esac
  done < <(git -C "$NEMO_GYM_ROOT" status --porcelain --untracked-files=all | awk '$1 == "??" {print substr($0, 4)}')
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

export NEMO_GYM_WRAPPER_MODEL_KEY="$API_KEY_VALUE"
python3 - "$MODEL_URL/models" >"$OUTPUT_DIR/model-models.json" <<'PY'
import os
import sys
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

request = urllib.request.Request(
    sys.argv[1],
    headers={"Authorization": f"Bearer {os.environ['NEMO_GYM_WRAPPER_MODEL_KEY']}"},
)
opener = urllib.request.build_opener(NoRedirect)
with opener.open(request, timeout=15) as response:
    sys.stdout.buffer.write(response.read())
PY
unset NEMO_GYM_WRAPPER_MODEL_KEY

terminate_group() {
  local signal=$1
  local pid=$2
  local pgid=${3:-}
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

group_alive() {
  local pgid=${1:-}
  [[ -n "$pgid" ]] && kill -0 -- "-$pgid" 2>/dev/null
}

launch_owned_process() {
  local log_file=$1
  shift
  python3 - "$@" >"$log_file" 2>&1 <<'PY' &
import os
import sys

try:
    os.setsid()
except PermissionError:
    os.setpgid(0, 0)
os.execv(sys.argv[1], sys.argv[1:])
PY
  ACTIVE_PID=$!
  ACTIVE_PGID=$ACTIVE_PID
}

wait_owned_process() {
  local pid=$ACTIVE_PID
  local status
  if wait "$pid"; then
    status=0
  else
    status=$?
  fi
  ACTIVE_PID=""
  ACTIVE_PGID=""
  return "$status"
}

assert_head_owned() {
  if [[ -z "$GYM_PID" ]] || ! kill -0 "$GYM_PID" 2>/dev/null; then
    echo "Gym launcher exited before the head became ready" >&2
    return 1
  fi
  if ! group_alive "$GYM_PGID"; then
    echo "Gym launcher process group is not alive after readiness" >&2
    return 1
  fi
  local listener_pids
  listener_pids=$(lsof -nP -iTCP:"$HEAD_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
  if [[ -z "$listener_pids" ]]; then
    echo "No process is listening on NEMO_GYM_HEAD_PORT $HEAD_PORT" >&2
    return 1
  fi
  local listener_pid listener_pgid
  while IFS= read -r listener_pid; do
    [[ -n "$listener_pid" ]] || continue
    listener_pgid=$(ps -o pgid= -p "$listener_pid" 2>/dev/null | tr -d ' ')
    if [[ -z "$listener_pgid" || "$listener_pgid" != "$GYM_PGID" ]]; then
      echo "NEMO_GYM_HEAD_PORT $HEAD_PORT is owned by an unexpected process group" >&2
      return 1
    fi
  done <<< "$listener_pids"
}

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "${ACTIVE_PID:-}" ]]; then
    terminate_group INT "$ACTIVE_PID" "${ACTIVE_PGID:-}"
    wait "$ACTIVE_PID" 2>/dev/null || true
    ACTIVE_PID=""
    ACTIVE_PGID=""
  fi
  if [[ -n "$GYM_PID" ]] && { group_alive "$GYM_PGID" || kill -0 "$GYM_PID" 2>/dev/null; }; then
    terminate_group INT "$GYM_PID" "$GYM_PGID"
    for _ in $(seq 1 30); do
      if ! group_alive "$GYM_PGID" && ! kill -0 "$GYM_PID" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if group_alive "$GYM_PGID" || kill -0 "$GYM_PID" 2>/dev/null; then
      terminate_group TERM "$GYM_PID" "$GYM_PGID"
      sleep 2
    fi
    if group_alive "$GYM_PGID" || kill -0 "$GYM_PID" 2>/dev/null; then
      terminate_group KILL "$GYM_PID" "$GYM_PGID"
    fi
    wait "$GYM_PID" 2>/dev/null || true
  fi
}

cancel() {
  local signal=$1
  local status=$2
  trap - EXIT INT TERM
  if [[ -n "${ACTIVE_PID:-}" ]]; then
    terminate_group "$signal" "$ACTIVE_PID" "${ACTIVE_PGID:-}"
    wait "$ACTIVE_PID" 2>/dev/null || true
    ACTIVE_PID=""
    ACTIVE_PGID=""
  fi
  cleanup
  exit "$status"
}

trap cleanup EXIT
trap 'cancel INT 130' INT
trap 'cancel TERM 143' TERM

WANDB_OVERRIDES=(
  "++wandb_project=null"
  "++wandb_name=null"
  "++wandb_api_key=null"
)
MLFLOW_DISABLED_OVERRIDES=(
  "++mlflow_tracking_uri=null"
  "++mlflow_tracking_token=null"
  "++mlflow_experiment_name=null"
  "++mlflow_run_name=null"
)
if [[ "$MLFLOW_ENABLED" == "1" ]]; then
  MLFLOW_OVERRIDES=(
    "++mlflow_tracking_uri=\${oc.env:NEMO_GYM_WRAPPER_MLFLOW_TRACKING_URI}"
    "++mlflow_experiment_name=\${oc.env:NEMO_GYM_WRAPPER_MLFLOW_EXPERIMENT_NAME}"
    "++mlflow_run_name=\${oc.env:NEMO_GYM_WRAPPER_MLFLOW_RUN_NAME}"
    "${MLFLOW_TOKEN_OVERRIDE[@]}"
  )
else
  MLFLOW_OVERRIDES=("${MLFLOW_DISABLED_OVERRIDES[@]}")
fi
START_EXPORTER_OVERRIDES=("${WANDB_OVERRIDES[@]}" "${MLFLOW_DISABLED_OVERRIDES[@]}")
EVAL_EXPORTER_OVERRIDES=("${WANDB_OVERRIDES[@]}" "${MLFLOW_OVERRIDES[@]}")

START_ARGS=(
  env start
  --resources-server qudgym
  --model-type "$MODEL_TYPE"
  --model "$NEMO_GYM_MODEL"
  --model-url "$MODEL_URL"
  "${API_KEY_START_ARGS[@]}"
  "++head_server.port=$HEAD_PORT"
  "++observability_enabled=true"
  "++model_call_capture_dir=$CAPTURE_DIR"
  "++upload_rollouts=false"
  "${START_EXPORTER_OVERRIDES[@]}"
)
if [[ "$MODEL_TYPE" == "vllm_model" && "${NEMO_GYM_USES_REASONING_PARSER:-false}" != "true" ]]; then
  START_ARGS+=("++policy_model.responses_api_models.vllm_model.uses_reasoning_parser=false")
fi

launch_owned_process "$OUTPUT_DIR/gym-env-start.log" "$GYM_BIN" "${START_ARGS[@]}"
GYM_PID=$ACTIVE_PID
# The launcher calls setsid/setpgid before exec, so its PID is the dedicated PGID.
GYM_PGID=$ACTIVE_PGID
launch_owned_process "$OUTPUT_DIR/gym-readiness.log" "$WAIT_SCRIPT" "$GYM_PID" "$HEAD_PORT" "${NEMO_GYM_READY_TIMEOUT_SECONDS:-240}"
wait_owned_process
if ! assert_head_owned; then
  exit 2
fi

if [[ "$MLFLOW_ENABLED" == "1" && -n "$MLFLOW_TOKEN_ENV" ]]; then
  export NEMO_GYM_WRAPPER_MLFLOW_TOKEN="$MLFLOW_TOKEN_VALUE"
fi
launch_owned_process "$OUTPUT_DIR/gym-eval-run.log" "$GYM_BIN" eval run --no-serve \
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
  "++upload_rollouts=$MLFLOW_UPLOAD_ROLLOUTS" \
  "${API_KEY_EVAL_ARGS[@]}" \
  "${EVAL_EXPORTER_OVERRIDES[@]}"
EVAL_STATUS=0
wait_owned_process || EVAL_STATUS=$?
unset NEMO_GYM_WRAPPER_MLFLOW_TOKEN
if (( EVAL_STATUS != 0 )); then
  exit "$EVAL_STATUS"
fi

launch_owned_process "$OUTPUT_DIR/gym-eval-profile.log" "$GYM_BIN" eval profile \
  --inputs "$MATERIALIZED" \
  --rollouts "$ROLLOUTS" \
  "++upload_rollouts=false" \
  "${START_EXPORTER_OVERRIDES[@]}"
wait_owned_process

python3 - "$ROLLOUTS" "$AGGREGATE" "$PROFILE" "$QUALITY" "$SUMMARY" "$REPEATS" "$REQUIRE_SUCCESS" "$MLFLOW_ENABLED" "$MLFLOW_UPLOAD_ROLLOUTS" <<'PY'
import json
import pathlib
import sys

rollouts_path, aggregate_path, profile_path, quality_path, summary_path = map(pathlib.Path, sys.argv[1:6])
repeats = int(sys.argv[6])
require_success = sys.argv[7] == "1"
mlflow_requested = sys.argv[8] == "1"
mlflow_upload_rollouts_requested = sys.argv[9] == "1"
rollouts = [json.loads(line) for line in rollouts_path.read_text(encoding="utf-8").splitlines() if line]
aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
profile_rows = [json.loads(line) for line in profile_path.read_text(encoding="utf-8").splitlines() if line]
quality = json.loads(quality_path.read_text(encoding="utf-8"))
if len(rollouts) != repeats:
    raise SystemExit(f"expected {repeats} rollout rows, found {len(rollouts)}")
if not all(row.get("info", {}).get("is_mock") is True for row in rollouts):
    raise SystemExit("NeMo Gym rollout lost the explicit is_mock=true boundary")
if not all(row.get("agent_ref", {}).get("name") == "qudgym_agent" for row in rollouts):
    raise SystemExit("NeMo Gym rollout used an unexpected agent instance")
if len(profile_rows) != 1:
    raise SystemExit(f"expected one reward profile row, found {len(profile_rows)}")
for row in profile_rows:
    if row.get("num_rollouts") != repeats or row.get("expected_num_rollouts") != repeats:
        raise SystemExit("reward profile did not contain every requested repeat")
    if row.get("missing_num_rollouts") != 0 or row.get("reward_profile_completion_pct") != 100.0:
        raise SystemExit("reward profile reported incomplete repeats")
if require_success:
    for row in rollouts:
        info = row.get("info", {})
        if row.get("reward") != 1.0 or row.get("terminated") is not True or row.get("truncated") is not False:
            raise SystemExit("mock rollout did not terminate successfully")
        if info.get("outcome") != "success" or info.get("turns_elapsed") != 4 or info.get("decisions_elapsed") != 5:
            raise SystemExit("mock rollout did not match the bounded success contract")
    for row in profile_rows:
        if row.get("mean/reward") != 1.0 or row.get("mean/terminated") != 1.0 or row.get("mean/truncated") != 0.0:
            raise SystemExit("reward profile did not preserve the success contract")
native_key_metrics = {
    entry.get("agent_ref", {}).get("name", "unknown"): entry.get("key_metrics", {})
    for entry in aggregate
}
summary = {
    "is_mock": True,
    "live_qud": False,
    "wandb_disabled": True,
    "mlflow_requested": mlflow_requested,
    "mlflow_status": "requested_not_verified" if mlflow_requested else "not_requested",
    "mlflow_upload_rollouts_requested": mlflow_upload_rollouts_requested,
    "success_contract_enforced": require_success,
    "expected_rollout_count": repeats,
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
