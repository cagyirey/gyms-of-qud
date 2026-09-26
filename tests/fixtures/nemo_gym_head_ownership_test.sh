#!/usr/bin/env bash
# Isolated regression checks for the wrapper's head-ownership proof.
#
# The pinned NVIDIA-NeMo/Gym readiness helper only checks the launcher PID while
# its polls are failing, so a healthy head left behind by an earlier run can
# satisfy it even when the new launcher has already died. The wrapper must prove
# the responding head belongs to the process group it launched before it
# dispatches any evaluation.
#
# This harness extracts the production functions from the wrapper rather than
# reimplementing them, so the assertions below track the shipped logic.
# HEAD_PORT/GYM_PID/GYM_PGID are consumed by the sourced assert_head_owned
# function below, so shellcheck cannot see the use from this file.
# shellcheck disable=SC2034
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WRAPPER="$ROOT/scripts/run_nemo_gym_mock.sh"

if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof is required for the ownership checks" >&2
  exit 77
fi

WORK=$(mktemp -d)
FOREIGN_PID=""
LISTENER_PID=""
cleanup() {
  set +e
  [[ -n "$FOREIGN_PID" ]] && kill -KILL "$FOREIGN_PID" 2>/dev/null
  [[ -n "$LISTENER_PID" ]] && kill -KILL "$LISTENER_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# Extract group_alive + assert_head_owned from the wrapper under test.
# Each awk range already spans the function header through its closing brace.
{
  awk '/^group_alive\(\) \{/,/^\}/' "$WRAPPER"
  awk '/^assert_head_owned\(\) \{/,/^\}/' "$WRAPPER"
} >"$WORK/functions.sh"
# shellcheck source=/dev/null
source "$WORK/functions.sh"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

# Start a command in its own process group, the same way the wrapper launches
# each Gym stage (portable: macOS has no setsid(1) binary). Output is detached
# so the caller can capture the PID through a command substitution.
start_own_group() {
  python3 - "$@" >/dev/null 2>&1 <<'PY' &
import os
import sys

try:
    os.setsid()
except PermissionError:
    os.setpgid(0, 0)
os.execv(sys.argv[1], sys.argv[1:])
PY
  echo $!
}

# os.execv does not search PATH, so pass absolute paths exactly as the wrapper
# does for $GYM_BIN and the pinned readiness helper.
PYTHON_BIN=$(command -v python3)
SLEEP_BIN=$(command -v sleep)

# A live but unrelated process stands in for a launcher that never served.
FOREIGN_PID=$(start_own_group "$SLEEP_BIN" 300)
FOREIGN_PGID=$(ps -o pgid= -p "$FOREIGN_PID" | tr -d ' ')

# A listener in its own group stands in for a ready head server.
LISTENER_PID=$(start_own_group "$PYTHON_BIN" -c "
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 0))
s.listen(1)
open('$WORK/port', 'w').write(str(s.getsockname()[1]))
time.sleep(300)
")
for _ in $(seq 1 100); do
  [[ -s "$WORK/port" ]] && break
  sleep 0.1
done
[[ -s "$WORK/port" ]] || fail "listener did not start"
HEAD_PORT=$(cat "$WORK/port")
LISTENER_PGID=$(ps -o pgid= -p "$LISTENER_PID" | tr -d ' ')

# 1. A dead launcher is rejected even when a healthy head answers.
GYM_PID=999999
GYM_PGID="$FOREIGN_PGID"
out=$(assert_head_owned 2>&1) && fail "dead launcher was accepted"
grep -q "exited before the head became ready" <<<"$out" \
  || fail "unexpected dead-launcher message: $out"

# 2. A live launcher that does not own the listening head is rejected.
GYM_PID="$FOREIGN_PID"
GYM_PGID="$FOREIGN_PGID"
out=$(assert_head_owned 2>&1) && fail "foreign head was accepted"
grep -q "owned by an unexpected process group" <<<"$out" \
  || fail "unexpected foreign-head message: $out"

# 3. The real owner of the head is accepted.
GYM_PID="$LISTENER_PID"
GYM_PGID="$LISTENER_PGID"
assert_head_owned >/dev/null 2>&1 || fail "own head was rejected"

# 4. A free port with no listener is rejected rather than assumed ready.
kill -KILL "$LISTENER_PID" 2>/dev/null || true
wait "$LISTENER_PID" 2>/dev/null || true
LISTENER_PID=""
sleep 0.5
GYM_PID="$FOREIGN_PID"
GYM_PGID="$FOREIGN_PGID"
out=$(assert_head_owned 2>&1) && fail "missing listener was accepted"
grep -q "No process is listening" <<<"$out" \
  || fail "unexpected missing-listener message: $out"

echo "head ownership regression checks passed"
