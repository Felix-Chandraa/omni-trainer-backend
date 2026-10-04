# R2 DEV-006 rev4

Rev4 changes the installer safety check from text matching to Python AST
inspection. This avoids false positives caused by documentation/comments that
mention process cleanup.

The managed runtime contract itself remains:
- project `.venv` Python and `.venv/bin` are authoritative;
- standalone JSBSim directory follows `.venv/bin` on PATH;
- `sim_vehicle.py` is invoked directly;
- DEV-004 owns process-group lifecycle;
- no global cleanup, EEPROM wipe, model mutation, mission command, or flight
  command is performed.

DEV-006 live acceptance is engineering-only: managed startup, raw FGNetFDM,
read-only raw MAVLink heartbeat, short stability, and scoped cleanup.
