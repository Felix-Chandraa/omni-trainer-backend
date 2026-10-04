#!/usr/bin/env bash
set -euo pipefail
R2="${OMNI_R2_ROOT:-$HOME/omni-r2-dev001-hyAZEv}"
SOURCE="${OMNI_SOURCE_ROOT:-$HOME/Downloads/omni_flight-main}"
PY="${OMNI_PYTHON:-$SOURCE/.venv/bin/python3}"
[[ -x "$PY" ]] || PY=python3
PKG="${1:-}"
if [[ -z "$PKG" ]]; then
  PKG="$(find "$R2/runtime/dev015-control" -type f -name index.json -path '*/evidence/*/index.json' -printf '%T@ %h\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
fi
[[ -n "$PKG" && -d "$PKG" ]] || { echo "ERROR: evidence package required"; exit 60; }
OVERLAY_ROOT="$R2/runtime/dev017-replay-overlay"
mkdir -p "$OVERLAY_ROOT"
echo "=== DEV-017 Actuated Replay ==="
echo "Source evidence : $PKG"
echo "Replay speed    : ${OMNI_REPLAY_SPEED:-1}"
echo "Building immutable runtime overlay from stored tlog..."
OUT="$(cd "$R2" && "$PY" -m src.training_core.dev017_replay_overlay "$PKG" --runtime-root "$OVERLAY_ROOT")"
OVERLAY="$(printf '%s\n' "$OUT" | tail -1)"
[[ -d "$OVERLAY" ]] || { printf '%s\n' "$OUT"; echo "ERROR: overlay creation failed"; exit 61; }
echo "Overlay         : $OVERLAY"
echo "Original package: UNCHANGED"
echo "Starting existing DEV-014 trajectory replay with tlog actuator enrichment..."
exec "$R2/run_dev014_replay.sh" "$OVERLAY"
