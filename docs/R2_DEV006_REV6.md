# R2 DEV-006 rev6 — Raw FG verifier contract fix

DEV-006 rev5 reached a live managed worker and received a valid FGNetFDM pose,
then the verifier raised AttributeError because it accessed
`first_pose.heading_deg`.

The actual FGPose contract exposes `roll_deg`, `pitch_deg`, and `yaw_deg`.
It does not expose `heading_deg`.

Rev6 changes only the evidence serialization to `yaw_deg`, adds regression
tests, and reruns the same managed live trial. Launcher behavior, worker
ownership, ArduPilot/JSBSim configuration, port policy, and cleanup are
unchanged.

A live pass is engineering evidence only:
- managed ArduPilot + JSBSim startup;
- direct raw FGNetFDM;
- read-only raw MAVLink heartbeat;
- short stability window;
- scoped worker shutdown.
