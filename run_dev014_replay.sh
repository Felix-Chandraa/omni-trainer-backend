#!/usr/bin/env bash
set -euo pipefail
BASE="/home/felix/omni-r2-dev001-hyAZEv"
SOURCE="/home/felix/Downloads/omni_flight-main"
FEATHER="$SOURCE/Feather-Flight-main"
PY="$SOURCE/.venv/bin/python"
PORT="${OMNI_REPLAY_PORT:-9200}"
HTTP_PORT="${OMNI_REPLAY_HTTP_PORT:-8015}"
SPEED="${OMNI_REPLAY_SPEED:-1}"

if [[ $# -gt 0 ]]; then
  EVIDENCE="$1"
else
  EVIDENCE="$(python3 - "$BASE" <<'PY'
from pathlib import Path
import sys
b=Path(sys.argv[1])
patterns=[
 "runtime/dev014-feather-live/*/evidence/*/index.json",
 "runtime/dev013-live/*/evidence/*/index.json",
 "runtime/dev012/*/evidence/*/index.json",
]
c=[]
for pat in patterns: c.extend(b.glob(pat))
if c: print(max(c,key=lambda p:p.stat().st_mtime).parent)
PY
)"
fi

[[ -n "$EVIDENCE" && -f "$EVIDENCE/index.json" ]] || {
  echo "Evidence package not found: $EVIDENCE"
  exit 1
}

RUN="$BASE/runtime/dev014-replay-view/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$RUN"
CREDS="$RUN/credentials.json"
LOG="$RUN/replay.log"

cleanup() {
  if [[ -n "${REPLAY_PID:-}" ]] && kill -0 "$REPLAY_PID" 2>/dev/null; then
    kill -INT "$REPLAY_PID" 2>/dev/null || true
    wait "$REPLAY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$BASE"
env PYTHONPATH="$BASE${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m src.training_core.replay_gateway \
    --evidence "$EVIDENCE" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --speed "$SPEED" \
    --loop \
    --credentials-file "$CREDS" \
    >"$LOG" 2>&1 &
REPLAY_PID=$!

for _ in $(seq 1 200); do
  [[ -s "$CREDS" ]] && break
  if ! kill -0 "$REPLAY_PID" 2>/dev/null; then
    echo "Replay server failed."
    cat "$LOG"
    exit 1
  fi
  sleep 0.1
done

[[ -s "$CREDS" ]] || {
  echo "Replay credentials timeout."
  cat "$LOG"
  exit 1
}

readarray -t C < <(python3 - "$CREDS" <<'PY'
import json,sys
x=json.load(open(sys.argv[1]))
print(x["server"])
print(x["student"])
print(x["token"])
print(x["attempt_id"])
print(x["duration_s"])
print(x["frames"])
PY
)

echo
echo "=== DEV-014 CESIUM REPLAY ==="
echo "Evidence : $EVIDENCE"
echo "Attempt  : ${C[3]}"
echo "Frames   : ${C[5]}"
echo "Duration : ${C[4]} s"
echo "Speed    : ${SPEED}x"
echo
echo "This is stored trajectory replay."
echo "ArduPilot and JSBSim are NOT started."
echo "Close Feather when finished."
echo

cd "$FEATHER"
"$PY" remote_client.py \
  --server "${C[0]}" \
  --student "${C[1]}" \
  --token "${C[2]}" \
  --http-port "$HTTP_PORT" || RC=$?
RC="${RC:-0}"

kill -INT "$REPLAY_PID" 2>/dev/null || true
wait "$REPLAY_PID" 2>/dev/null || true
REPLAY_PID=""

echo
cat "$LOG"
exit "$RC"
