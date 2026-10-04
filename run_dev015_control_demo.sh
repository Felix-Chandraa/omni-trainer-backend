#!/usr/bin/env bash
set -euo pipefail
BASE="/home/felix/omni-r2-dev001-hyAZEv"
SOURCE="/home/felix/Downloads/omni_flight-main"
FEATHER="$SOURCE/Feather-Flight-main"
PY="$SOURCE/.venv/bin/python"
PORT="${OMNI_CONTROL_PORT:-9120}"
HTTP_PORT="${OMNI_CONTROL_HTTP_PORT:-8016}"

RUN="$BASE/runtime/dev015-manual/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$RUN"
CREDS="$RUN/credentials.json"
SUMMARY="$RUN/summary.json"
LOG="$RUN/server.log"

cleanup() {
  if [[ -n "${HOTAS_PID:-}" ]] && kill -0 "$HOTAS_PID" 2>/dev/null; then
    kill -INT "$HOTAS_PID" 2>/dev/null || true
    wait "$HOTAS_PID" 2>/dev/null || true
  fi
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -INT "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$BASE"
env PYTHONPATH="$BASE${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m src.training_core.lan_control_server \
    --base "$BASE" \
    --source "$SOURCE" \
    --port "$PORT" \
    --credentials-file "$CREDS" \
    --summary-file "$SUMMARY" \
    >"$LOG" 2>&1 &
SERVER_PID=$!

echo "Starting DEV-015 real control server..."
for _ in $(seq 1 900); do
  [[ -s "$CREDS" ]] && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server exited before credentials were ready."
    tail -100 "$LOG"
    exit 1
  fi
  sleep 0.1
done
[[ -s "$CREDS" ]] || { echo "Credentials timeout"; tail -100 "$LOG"; exit 1; }

readarray -t C < <(python3 - "$CREDS" <<'PY'
import json,sys
x=json.load(open(sys.argv[1]))
print(x["server"]); print(x["student"]); print(x["token"]); print(x["attempt_id"])
PY
)
SERVER="${C[0]}"
STUDENT="${C[1]}"
TOKEN="${C[2]}"

echo
echo "=== DEV-015 REAL CONTROL ==="
echo "Attempt : ${C[3]}"
echo "Server  : $SERVER"
echo
echo "Feather opens with a Flight Control panel."
echo "Safe sequence: ARM -> ENABLE CONTROL -> W slowly -> RELEASE -> DISARM."
echo "Closing Feather finalizes evidence."
echo

if [[ -n "${OMNI_HOTAS_DEVICE:-}" ]]; then
  echo "Optional HOTAS: $OMNI_HOTAS_DEVICE"
  cd "$FEATHER"
  "$PY" hotas_client.py \
    --device "$OMNI_HOTAS_DEVICE" \
    --server "$SERVER" \
    --student "$STUDENT" \
    --token "$TOKEN" \
    ${OMNI_HOTAS_ARGS:-} \
    >"$RUN/hotas.log" 2>&1 &
  HOTAS_PID=$!
fi

cd "$FEATHER"
set +e
"$PY" remote_client.py \
  --server "$SERVER" \
  --student "$STUDENT" \
  --token "$TOKEN" \
  --http-port "$HTTP_PORT"
CLIENT_RC=$?
set -e

if [[ -n "${HOTAS_PID:-}" ]] && kill -0 "$HOTAS_PID" 2>/dev/null; then
  kill -INT "$HOTAS_PID" 2>/dev/null || true
  wait "$HOTAS_PID" 2>/dev/null || true
  HOTAS_PID=""
fi

kill -INT "$SERVER_PID" 2>/dev/null || true
set +e
wait "$SERVER_PID"
SERVER_RC=$?
set -e
SERVER_PID=""

echo
echo "=== DEV-015 SERVER RESULT ==="
tail -100 "$LOG"
[[ -s "$SUMMARY" ]] && { echo; cat "$SUMMARY"; }

if [[ $CLIENT_RC -ne 0 ]]; then exit "$CLIENT_RC"; fi
if [[ $SERVER_RC -ne 0 ]]; then exit "$SERVER_RC"; fi
echo
echo "PASS: DEV-015 manual control session finalized."
