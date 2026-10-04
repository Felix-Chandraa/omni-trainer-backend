OMNI DEV-011 Remote Student Client
==================================

1. Copy/extract this folder on the Student PC.
2. Install Python venv support if needed:
     sudo apt install -y python3-venv
3. In this folder:
     python3 -m venv .venv
     .venv/bin/pip install -r requirements-remote.txt
4. Start DEV-011 server on the OMNI Server PC and copy this student's token.
5. Run, for example:
     .venv/bin/python remote_client.py \
       --server ws://192.168.10.10:9100 \
       --student student-1 \
       --token 'TOKEN_FROM_SERVER'

Current Cesium JS/World Terrain still use Internet resources. OMNI telemetry
itself travels only over LAN. DEV-011 is telemetry/Cesium only; no HOTAS
command path exists yet.
