# R2 DEV-006 rev9 — Cleanup is part of acceptance

REV8 proved:
- managed ArduPilot + JSBSim;
- raw FGNetFDM;
- raw MAVLink heartbeat;
- stability;
but also observed TCP 5760 still active after worker stop.

REV9 refines worker stop into a bounded three-stage shutdown:

1. close the worker-owned stdin pipe to provide EOF and allow trusted wrappers
   such as sim_vehicle/MAVProxy to run their own scoped teardown;
2. if still alive, SIGTERM only the worker-owned process group;
3. if still alive after the bounded timeout, SIGKILL only that process group.

DEV-006 acceptance now requires both runtime evidence and listener cleanup.
Runtime evidence with leftover SITL ports returns code 33 and is NOT accepted.

No global pkill or flight command is introduced.
