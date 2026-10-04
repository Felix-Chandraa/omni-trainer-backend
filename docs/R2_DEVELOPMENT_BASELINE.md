# R2 — Development baseline 001 (engineering slice)

Status: **Implemented as offline core prototype; not a training release / G3 pass.**
Source: user-supplied `omni_flight-main.zip`, plus SDD and Rencana v4.0.
The ZIP is a snapshot. Reconcile it with the current checkout (especially gimbal and wrappers) before integration.

## Included, and what it actually does

- `src/training_core/models.py`: Attempt identity, role/scope and state types.
- `store.py`: SQLite metadata, one live attempt per exercise, append-only event rows and command-admission record; restart reads previous data. **Not** authoritative flight truth, AV evidence, or approved production storage.
- `session.py`: 1 instructor / 1 student Session → Exercise → Attempt; state transition checks, new Attempt ID and generation on restart, stale revision rejection, guarded readiness/recovery flags. Does **not** physically hold/restart JSBSim or verify real devices.
- `commands.py`: engineering **probe-only** admission with actor, attempt, aircraft, generation, authority epoch, state, revision, expiry, scope checks and idempotency. `accepted` explicitly means *admitted only, not applied*. Only `flight_probe`, `payload_probe`, `both_probe` with empty payload are admitted. No MAVLink dispatch, authentication gateway, approved capability, target-system ACK, or real Flight/Payload transfer.
- `worker.py`: standalone POSIX subprocess owner and scoped stop. Runs only an **explicit** supplied process. No PyQt import. **No ArduPilot/JSBSim launcher adapter, port assignment, MAVLink demux, FG receiver, health/ready reconciliation, or multi-aircraft isolation is claimed.**
- `demo.py`: temporary SQLite metadata demonstration; does not launch the simulator.
- `tests/test_training_core.py`: offline lifecycle, negative command and process-cleanup tests.

## Install these *new files* without replacing the current simulator

1. Preserve current checkout and identify its real project root (`main.py`, `src/`, `map.html`). Check whether `src/training_core/`, `tests/test_training_core.py`, or this document already exist. If so, compare before copying.
2. Extract this patch into that root **only if these paths are absent or reviewed**. Do not replace existing `src/main_window.py`, `src/mav_worker.py`, `src/telemetry_bus.py`, `run_demo.sh`, aircraft XML, or any gimbal file.
3. From that project root, use the already-installed Python 3 environment; no dependency install necessary for this package:

```bash
python3 -m unittest discover -s tests -p 'test_training_core.py' -v
python3 -m src.training_core.demo
python3 -m compileall -q src/training_core tests/test_training_core.py
```

Do **not** point `AircraftWorker` at the supplied `run_demo.sh` yet. Its `pkill -f "${AP_DIR}"` cleanup can affect unrelated instances from the same project. It also uses fixed ports and global `/tmp/omni_mission_load.log`. Refactor the launch ownership and allocate per-attempt ports/runtime/EEPROM before integration.

## Gaps and immediate next code work

1. R0: reconcile current checkout, gimbal source, exact dependencies, installed ArduPilot/JSBSim versions and asset hashes. This patch assumes none of these.
2. R1 gate: measure authoritative full pause, control transfer/epoch, synchronized AV, resource/workload. Demo flags `readiness_verified=True` are **fixtures**, not actual verifications.
3. R2 next: extract MAVLink+FG adapters from Qt `MavWorker`, single receive-loop/MAVLink ACK demultiplexing, worker-scoped ports, runtime/EEPROM ownership and healthy/failed transitions; maintain existing FG pose ownership, visual behavior and mission download. Add authenticated gateway and capability/release gating before any real controls. Attach append-only state snapshots before claiming core acceptance.
4. R4: real atomic flight/payload/Both authority, full pause and recovery; do not treat present state bookkeeping as authoritative pause.

No SITL, JSBSim, network, real HOTAS or GUI runtime test was performed in the provided environment. No user source archive or checkout was modified; this patch only adds new paths in a separate working copy.
