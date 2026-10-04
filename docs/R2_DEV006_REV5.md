# R2 DEV-006 rev5 — Explicit venv identity

The project venv was healthy, but the previous managed wrapper resolved the
venv Python symlink to the system interpreter. This erased virtual-environment
identity and produced `VIRTUAL_ENV=/usr`.

Rev5 makes `<project>/.venv/bin/python3` an explicit launch-contract argument
and never dereferences that path when deriving VIRTUAL_ENV/PATH.

Worker safety invariants remain unchanged: loopback-only outputs, explicit
standalone JSBSim, DEV-004 process-group ownership, and no global cleanup,
EEPROM wipe, source mutation, mission command, or flight command.
