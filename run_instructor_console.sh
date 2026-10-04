#!/usr/bin/env bash
set -euo pipefail
BASE="${OMNI_R2_ROOT:-$HOME/omni-r2-dev001-hyAZEv}"
SOURCE="${OMNI_SOURCE_ROOT:-$HOME/Downloads/omni_flight-main}"
PY="${OMNI_PYTHON:-$SOURCE/.venv/bin/python3}"
# DEV018_REV4_LAN_STUDENT_BIND
BIND_HOST="${OMNI_INSTRUCTOR_BIND_HOST:-0.0.0.0}"
ACCESS_HOST="${OMNI_INSTRUCTOR_ACCESS_HOST:-127.0.0.1}"
PORT="${OMNI_INSTRUCTOR_PORT:-8020}"
[[ -x "$PY" ]] || PY=python3
WEB="$BASE/web/instructor_console_v1"
cd "$BASE"
export PYTHONPATH="$BASE${PYTHONPATH:+:$PYTHONPATH}"

"$PY" -m src.training_core.instructor_console_r4a \
  --base "$BASE" --source "$SOURCE" --host "$BIND_HOST" --port "$PORT" --web-root "$WEB" &
PID=$!
cleanup(){
  if kill -0 "$PID" 2>/dev/null; then kill -INT "$PID" 2>/dev/null || true; fi
  wait "$PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

URL="http://$ACCESS_HOST:$PORT/"
for _ in $(seq 1 100); do
  if command -v curl >/dev/null 2>&1 && curl -fsS "$URL/api/status" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$PID" 2>/dev/null; then wait "$PID"; exit $?; fi
  sleep 0.1
done

echo "OMNI Instructor Console: $URL"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true; fi
wait "$PID"
