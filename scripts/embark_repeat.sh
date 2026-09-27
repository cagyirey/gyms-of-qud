#!/usr/bin/env bash
# Repeat the programmatic embark N times and tally whether the onUi hop
# completes. Answers one question only: is the NewGame manifest tag causal,
# or did the single earlier success just happen?
#
#   scripts/embark_repeat.sh [runs] [tag]
#     runs  default 4
#     tag   "NewGame Script" (default), or "Script" to A/B against no tag
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UD="$HOME/Library/Application Support/com.FreeholdGames.CavesOfQud"
DIAG="$UD/QudGym-diagnostic.txt"
RUNS="${1:-4}"
TAG="${2-NewGame Script}"
BASE_TAG="Script"
[ "$TAG" = "none" ] && TAG="$BASE_TAG"

MANIFEST="$ROOT/mod/QudGym/manifest.json"
BACKUP="$(mktemp)"
cp "$MANIFEST" "$BACKUP"
restore() {
  set +e
  pkill -f "CoQ.app/Contents/MacOS/CoQ" 2>/dev/null
  cp "$BACKUP" "$MANIFEST"
  rm -f "$BACKUP"
}
trap restore EXIT INT TERM

apply_tag() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text())
d["Tags"] = sys.argv[2]
p.write_text(json.dumps(d, indent=2) + "\n")
PY
}

apply_tag "$TAG"
echo "tag under test: $TAG"
echo

HOP_OK=0
HOP_TIMEOUT=0
NO_EMBARK=0
REACHED_RUN=0

for n in $(seq 1 "$RUNS"); do
  pkill -f "CoQ.app/Contents/MacOS/CoQ" 2>/dev/null
  sleep 3
  : >"$DIAG"

  cua-driver launch_app \
    '{"bundle_id":"com.FreeholdGames.CavesOfQud","creates_new_application_instance":true}' \
    >/dev/null 2>&1

  outcome="no_embark"
  for _ in $(seq 1 30); do
    if grep -q "embark prepared" "$DIAG" 2>/dev/null; then outcome="hop_ok"; break; fi
    if grep -q "embark prepare failed" "$DIAG" 2>/dev/null; then outcome="hop_timeout"; break; fi
    if grep -qE "rungame" "$DIAG" 2>/dev/null; then outcome="hop_ok"; break; fi
    sleep 4
  done
  # Let a successful prepare get as far as it can.
  if [ "$outcome" = "hop_ok" ]; then
    for _ in $(seq 1 20); do
      grep -q "rungame" "$DIAG" 2>/dev/null && break
      sleep 2
    done
  fi

  reached="no"
  grep -q "rungame" "$DIAG" 2>/dev/null && reached="yes"
  aborts=$(grep -c "ThreadAbort" "$DIAG" 2>/dev/null || echo 0)

  case "$outcome" in
    hop_ok) HOP_OK=$((HOP_OK + 1)) ;;
    hop_timeout) HOP_TIMEOUT=$((HOP_TIMEOUT + 1)) ;;
    *) NO_EMBARK=$((NO_EMBARK + 1)) ;;
  esac
  [ "$reached" = "yes" ] && REACHED_RUN=$((REACHED_RUN + 1))

  printf 'run %d: %-11s reached_rungame=%-3s threadaborts=%s\n' \
    "$n" "$outcome" "$reached" "$aborts"
  cp "$DIAG" "$ROOT/local/live-evidence/embark-repeat-$(printf '%02d' "$n").txt" 2>/dev/null
done

echo
echo "=== tally (tag: $TAG) ==="
echo "hop completed : $HOP_OK / $RUNS"
echo "hop timed out : $HOP_TIMEOUT / $RUNS"
echo "no embark     : $NO_EMBARK / $RUNS"
echo "reached rungame: $REACHED_RUN / $RUNS"
