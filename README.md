# OMNI Trainer R2 Backend

Canonical backend/runtime for the OMNI Trainer fixed-wing training simulator.

Status: current R2 engineering baseline. Implementation presence does not automatically mean formal QA PASS or training acceptance.

---

## 1. Repository Scope

This repository contains the OMNI Trainer R2 backend and Instructor Console runtime.

R2 is authoritative for:

- Session -> Exercise -> Attempt lifecycle
- simulator worker ownership
- ArduPilot SITL / JSBSim orchestration
- worker readiness and runtime state
- Flight authority
- Instructor takeover and Student handback
- interruption and recovery
- telemetry and evidence handling
- stored-state historical Review

The legacy omni_flight implementation is not the canonical backend.

---

## 2. Delivery Configuration

Current delivery baseline:

- 1 Instructor
- 1 Student

The software architecture is designed to support expansion toward:

- 1 Instructor
- up to 3 Student workers

The current delivered baseline remains 1+1.

The 1+3 configuration is an expansion capability and is not the current formal acceptance target.

---

## 3. Architecture

    Instructor Console / Student Client
                    |
                    v
            OMNI Trainer R2
            Canonical Backend
                    |
            Worker Management
                    |
                    v
              ArduPlane SITL
                    |
                    v
                 JSBSim
                    |
                    v
             Aircraft/FDM State

R2 owns simulator worker lifecycle and the resources allocated to each worker.

Normal R2 Instructor operation should not require manually launching independent ArduPlane, JSBSim, or MAVProxy processes before starting the training workflow.

---

## 4. R2 Compared With Legacy OMNI

| Area | Legacy OMNI | R2 |
|---|---|---|
| Orchestration | Launcher / sim_vehicle.py oriented | Backend-managed workers |
| Backend authority | Distributed across legacy components | R2 canonical authority |
| Simulator lifecycle | Launcher/user coupled | Worker-owned |
| Ports | Primarily single-instance assumptions | Isolated per-worker resources |
| Training lifecycle | Limited runtime state | Session -> Exercise -> Attempt |
| Instructor authority | Legacy control behavior | Explicit authority contract |
| Review | Runtime/log oriented | Stored-state historical Review |

R2 is the canonical backend direction.

Legacy TelemetryBus, launcher, and GUI-side logic must not become a second competing backend.

---

## 5. Current Transitional Dependency

R2 is currently a transitional engineering baseline and is not yet a fully standalone distribution.

The current installation still consumes simulation/runtime assets from the existing OMNI source tree.

Typical development environment:

    export OMNI_R2_ROOT="$HOME/omni-r2-dev001-hyAZEv"
    export OMNI_SOURCE_ROOT="$HOME/Downloads/omni_flight-main"
    export OMNI_PYTHON="$OMNI_SOURCE_ROOT/.venv/bin/python3"

The integration source currently supplies items such as:

- project Python environment
- ArduPilot runtime/build
- JSBSim integration
- aircraft models
- simulation assets

---

## 6. Running the Backend

From the repository root:

    cd ~/omni-r2-dev001-hyAZEv
    ./run_instructor_console.sh

run_instructor_console.sh is the canonical runtime launcher tracked in the public backend repository.

Development-only run_dev*.sh helpers are intentionally excluded from GitHub.

For the normal R2 workflow, do not manually start JSBSim or ArduPlane before launching the Instructor Console.

The backend manages the simulator worker when the training lifecycle requires it.

---

## 7. Runtime Ownership

The R2 backend is responsible for:

1. creating the training runtime context;
2. preparing an Attempt;
3. allocating worker-specific resources;
4. starting the simulator worker;
5. determining readiness;
6. exposing authoritative runtime state;
7. managing Flight authority;
8. collecting runtime evidence;
9. shutting down resources owned by that worker.

Cleanup must remain scoped to resources owned by the relevant R2 worker.

The backend must not indiscriminately terminate unrelated simulator processes.

---

## 8. Session and Attempt Model

The canonical lifecycle is:

    Session
       |
       v
    Exercise
       |
       v
    Attempt
       |
       +--> worker preparation
       +--> readiness
       +--> live execution
       +--> evidence
       +--> completion/interruption
       +--> historical Review

Attempt identity is important because runtime state, authority, evidence, recovery, and Review must remain associated with the correct training execution.

---

## 9. Flight Authority

The current authority model includes Student and Instructor ownership of Flight control.

Runtime-proven R4-A behavior includes:

- Student Flight authority
- Instructor Flight takeover
- Student flight input no longer being authoritative during Instructor takeover
- explicit Student handback
- restoration of Student Flight authority after handback

Authority state must remain consistent between backend state and the user interfaces.

---

## 10. Historical Review

Historical Review operates from stored Attempt state/evidence.

Review must not:

- launch a new ArduPilot worker
- launch a new JSBSim process
- rerun the original FDM execution
- modify the historical Attempt

Review is therefore fundamentally different from a live simulation session.

---

## 11. Current Engineering Baseline

Current implemented/runtime-proven work includes:

- Session / Exercise / Attempt lifecycle
- managed simulator worker lifecycle
- isolated worker resource ownership
- readiness handling
- Student live-flight path
- Instructor Flight takeover
- explicit Student handback
- interruption and recovery handling
- telemetry/evidence flow
- stored-state historical Review
- Review without rerunning the FDM
- Instructor Console web runtime

These statements describe the engineering baseline and do not automatically represent formal acceptance.

---

## 12. Not Yet Claimed Complete

The following are not claimed as formally complete:

- R4-B Instructor actual flight command input
- R4-C Payload authority
- R4-C atomic Both authority
- R4-D physical HOTAS / Instructor panel
- complete EO/IR/ranging/capture workflow
- complete voice/webcam/media workflow
- complete competency/release workflow
- final reporting/PDF/retention/backup closure
- formal 1+3 deployment acceptance
- formal end-to-end training acceptance

Fixed or implemented is not equivalent to formally verified PASS.

---

## 13. Mentor QA Checklist

### Backend Startup

Verify:

- run_instructor_console.sh starts the backend
- Instructor Console becomes reachable
- normal startup does not require manually starting JSBSim
- simulator lifecycle is backend-owned
- worker readiness is based on actual runtime state

### Session / Exercise / Attempt

Verify:

- Session can be created
- Exercise can be prepared
- Attempt can be created
- worker preparation starts correctly
- readiness transition is correct
- Attempt termination is clean

### Student Flight

Verify:

- Student receives live simulator state
- telemetry updates continuously
- Student control reaches the authoritative worker

### Instructor Flight Authority

Verify:

- Student initially owns Flight where applicable
- Instructor takeover changes the authoritative owner
- Student input is no longer authoritative during takeover
- explicit handback restores Student authority
- backend and UI authority displays remain consistent

### Recovery

Verify:

- interruption state is represented explicitly
- worker-owned resources are cleaned up
- unrelated simulator processes remain untouched
- recovery does not silently corrupt Attempt state

### Historical Review

Verify:

- a completed Attempt can be opened
- stored state/evidence is reproduced
- Review does not create a new live simulator worker
- Review does not rerun ArduPilot/JSBSim/FDM

---

## 14. QA Evidence

For each verification execution, record at minimum:

- test ID
- backend commit hash
- relevant configuration
- Session / Exercise / Attempt identifier
- procedure
- expected result
- actual result
- PASS / FAIL
- evidence path or reference
- evidence hash where applicable
- defect/reference if applicable
- reviewer
- verification date

Do not mark a requirement PASS merely because implementation code exists.

---

## 15. Repository Hygiene

The public backend repository excludes local/generated artifacts such as:

- Python virtual environments
- Python cache files
- runtime logs
- telemetry logs
- temporary runtime state
- local runtime databases
- local backups
- development-only launch scripts

The canonical tracked shell launcher is:

    run_instructor_console.sh

Development launchers named run_dev*.sh remain local development tools and are not part of the mentor-facing public repository.

---

## 16. Source-of-Truth Rule

When investigating behavior or defects, use this precedence:

1. latest runtime evidence
2. current source
3. current Working Context
4. current Correction Ledger
5. Master SDD
6. historical changelog and older material

Static/source-level success is not sufficient to claim runtime success.

---

## 17. Repository Role

This repository is the OMNI Trainer R2 canonical backend engineering baseline.

Current product delivery scope is 1 Instructor + 1 Student.

The architecture preserves the path toward multi-worker expansion without requiring the current delivery to operate as a 1+3 deployment.
