#!/usr/bin/env bash
set -euo pipefail
BASE="/home/felix/omni-r2-dev001-hyAZEv"
SOURCE="/home/felix/Downloads/omni_flight-main"
FEATHER="$SOURCE/Feather-Flight-main"
PY="$SOURCE/.venv/bin/python"
PORT="${OMNI_LAN_PORT:-9110}"
HTTP_PORT="${OMNI_HTTP_PORT:-8014}"

RUN="$BASE/runtime/dev014-manual/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$RUN"
CREDS="$RUN/credentials.json"
SUMMARY="$RUN/summary.json"
LOG="$RUN/server.log"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -INT "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$BASE"
env PYTHONPATH="$BASE${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m src.training_core.lan_recording_server \
    --base "$BASE" \
    --source "$SOURCE" \
    --port "$PORT" \
    --credentials-file "$CREDS" \
    --summary-file "$SUMMARY" \
    >"$LOG" 2>&1 &
SERVER_PID=$!

echo "Starting real worker + recorded LAN server..."
for _ in $(seq 1 900); do
  [[ -s "$CREDS" ]] && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server exited before credentials were ready."
    tail -80 "$LOG"
    exit 1
  fi
  sleep 0.1
done

if [[ ! -s "$CREDS" ]]; then
  echo "Timed out waiting for server credentials."
  tail -80 "$LOG"
  exit 1
fi

readarray -t C < <(python3 - "$CREDS" <<'PY'
import json,sys
x=json.load(open(sys.argv[1]))
print(x["server"])
print(x["student"])
print(x["token"])
print(x["attempt_id"])
PY
)
SERVER="${C[0]}"
STUDENT="${C[1]}"
TOKEN="${C[2]}"
ATTEMPT="${C[3]}"

echo
echo "=== REAL FEATHER/CESIUM PROOF ==="
echo "Attempt : $ATTEMPT"
echo "Server  : $SERVER"
echo
echo "Feather will open now."
echo "Move around the UI / change a view if desired."
echo "Close the Feather window to finalize the evidence package."
echo

cd "$FEATHER"
"$PY" remote_client.py \
  --server "$SERVER" \
  --student "$STUDENT" \
  --token "$TOKEN" \
  --http-port "$HTTP_PORT" || CLIENT_RC=$?
CLIENT_RC="${CLIENT_RC:-0}"

kill -INT "$SERVER_PID" 2>/dev/null || true
set +e
wait "$SERVER_PID"
SERVER_RC=$?
set -e
SERVER_PID=""

echo
echo "=== SERVER RESULT ==="
tail -60 "$LOG"
if [[ -s "$SUMMARY" ]]; then
  echo
  cat "$SUMMARY"
fi

if [[ $CLIENT_RC -ne 0 ]]; then
  echo "Feather client exited with code $CLIENT_RC"
  exit "$CLIENT_RC"
fi
if [[ $SERVER_RC -ne 0 ]]; then
  echo "Recorded server proof exited with code $SERVER_RC"
  exit "$SERVER_RC"
fi
echo
echo "PASS: real Feather/Cesium evidence proof finalized."
