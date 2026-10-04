#!/usr/bin/env bash
set -euo pipefail
cd "/home/felix/omni-r2-dev001-hyAZEv"
exec env PYTHONPATH="/home/felix/omni-r2-dev001-hyAZEv${PYTHONPATH:+:$PYTHONPATH}"   python3 -m src.training_core.lan_session_server     --base "/home/felix/omni-r2-dev001-hyAZEv"     --source "/home/felix/Downloads/omni_flight-main"     --students "${OMNI_STUDENTS:-3}"     --bind "${OMNI_BIND:-0.0.0.0}"     --port "${OMNI_LAN_PORT:-9100}"
