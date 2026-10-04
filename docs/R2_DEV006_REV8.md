# R2 DEV-006 rev8 — Headless stdin keepalive

AircraftWorker previously launched the owned child with stdin connected to
DEVNULL. The managed child becomes sim_vehicle.py and launches MAVProxy.
EOF-sensitive subprocesses can interpret that stdin EOF as termination.

Rev8 replaces DEVNULL with an owned subprocess PIPE. The parent keeps the pipe
open but never writes to it. This is not a command channel; it only prevents
premature EOF.

Safety invariants remain:
- start_new_session=True;
- stop signals only the owned process group;
- no global cleanup;
- no stdin command writes;
- no flight/MAVLink/mission/parameter/arm/mode/RC command added.
