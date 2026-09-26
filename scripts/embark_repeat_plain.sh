#!/usr/bin/env bash
# Repeat the programmatic embark with the game launched as a plain background
# process, with no GUI driver, accessibility client, or window management
# involved. Compares against scripts/embark_repeat.sh, which launches every run
# through cua-driver.
#
#   scripts/embark_repeat_plain.sh [runs]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UD="$HOME/Library/Application Support/com.FreeholdGames.CavesOfQud"
DIAG="$UD/QudGym-diagnostic.txt"
BIN="$HOME/Library/Application Support/Steam/steamapps/common/Caves of Qud/CoQ.app/Contents/MacOS/CoQ"
RUNS="${1:-3}"
LOGDIR="$ROOT/local/live-evidence"

[ -x "$BIN" ] || { echo "game binary not found: $BIN" >&2; exit 2; }
mkdir -p "$LOGDIR"

HOP_OK=0; HOP_TIMEOUT=0; REACHED=0; NO_EMBARK=0

for n in $(seq 1 "$RUNS"); do
  pkill -f "CoQ.app/Contents/MacOS/CoQ" 2>/dev/null
  sleep 3
  : >"$DIAG"

  # Plain launch. No -batchmode (the game tears itself down), no cua-driver.
  "$BIN" -logFile "/tmp/qud-plain-$n.log" >"/tmp/qud-plain-$n.out" 2>&1 &

  outcome="no_embark"
  for _ in $(seq 1 30); do
    if grep -q "embark prepared" "$DIAG" 2>/dev/null; then outcome="hop_ok"; break; fi
    if grep -q "embark prepare failed" "$DIAG" 2>/dev/null; then outcome="hop_timeout"; break; fi
    sleep 4
  done
  if [ "$outcome" = "hop_ok" ]; then
    for _ in $(seq 1 25); do
      grep -q "rungame" "$DIAG" 2>/dev/null && break
      sleep 2
    done
  fi

  reached="no"
  grep -q "rungame" "$DIAG" 2>/dev/null && reached="yes"
  case "$outcome" in
    hop_ok) HOP_OK=$((HOP_OK + 1)) ;;
    hop_timeout) HOP_TIMEOUT=$((HOP_TIMEOUT + 1)) ;;
    *) NO_EMBARK=$((NO_EMBARK + 1)) ;;
  esac
  [ "$reached" = "yes" ] && REACHED=$((REACHED + 1))

  printf 'run %d: %-11s reached_rungame=%s\n' "$n" "$outcome" "$reached"
  cp "$DIAG" "$LOGDIR/embark-plain-$(printf '%02d' "$n").txt" 2>/dev/null
done

pkill -f "CoQ.app/Contents/MacOS/CoQ" 2>/dev/null

echo
echo "=== tally (plain launch, no GUI driver) ==="
echo "hop completed  : $HOP_OK / $RUNS"
echo "hop timed out  : $HOP_TIMEOUT / $RUNS"
echo "no embark      : $NO_EMBARK / $RUNS"
echo "reached rungame: $REACHED / $RUNS"
