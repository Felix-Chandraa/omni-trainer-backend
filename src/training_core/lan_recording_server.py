"""One-PC real Feather/Cesium evidence proof server for DEV-014."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import threading
import time
import uuid

from .active_start import AuthorizedActiveStart
from .client_evidence_gateway import EvidenceLanTelemetryGateway
from .evidence_recorder import (
    AttemptEvidenceRecorder,
    AttemptIdentity,
)
from .fg_adapter import FGObserver
from .models import Actor, CoreError, Role, State
from .multi_live_trial import (
    evidence,
    slot_ports_free,
    wait_tcp_cleanup,
    worker_argv,
)
from .multi_runtime import (
    RuntimeSlotAllocator,
    extract_arduplane_template,
    find_latest_working_worker_log,
    validate_template,
)
from .omni_launcher_adapter import (
    choose_jsbsim,
    source_from_root,
)
from .readiness import TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator

KNOT_TO_MPS = 0.514444


def _server_state(pose) -> dict:
    return {
        "lat_deg": pose.lat_deg,
        "lon_deg": pose.lon_deg,
        "alt_msl_m": pose.alt_msl_m,
        "agl_m": pose.agl_raw_m,
        "roll_deg": pose.roll_deg,
        "pitch_deg": pose.pitch_deg,
        "yaw_deg": pose.yaw_deg,
        "vcas_mps": (
            None
            if pose.vcas_kt is None
            else float(pose.vcas_kt) * KNOT_TO_MPS
        ),
        "climb_mps": pose.climb_mps,
        "frame": "FGNetFDM",
    }


def _client_telemetry(pose) -> dict:
    airspeed = (
        None
        if pose.vcas_kt is None
        else float(pose.vcas_kt) * KNOT_TO_MPS
    )
    return {
        "lat": pose.lat_deg,
        "lon": pose.lon_deg,
        "alt": pose.agl_raw_m,
        "alt_msl": pose.alt_msl_m,
        "agl": pose.agl_raw_m,
        "agl_raw": pose.agl_raw_m,
        "roll": pose.roll_deg,
        "pitch": pose.pitch_deg,
        "yaw": pose.yaw_deg,
        "hdg": pose.yaw_deg % 360.0,
        "as": airspeed,
        "gs": None,
        "pose_source": "fg",
        "vehicle_type": 1,
        "armed": False,
        "mode": "UNKNOWN",
    }


def run_server(
    *,
    base: Path,
    source_root: Path,
    port: int,
    credentials_file: Path,
    summary_file: Path,
) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(
        source, home=Path.home()
    )
    proven_log = find_latest_working_worker_log(base)
    template = extract_arduplane_template(
        proven_log
    )
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = (
        base
        / "runtime"
        / "dev014-feather-live"
        / f"{stamp}-{uuid.uuid4().hex[:8]}"
    )
    runtime.mkdir(parents=True, exist_ok=False)
    template_json = runtime / "arduplane-template.json"
    template_json.write_text(
        json.dumps(
            {
                "source_worker_log": str(proven_log),
                "arduplane_argv": list(template),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    store = Store(runtime / "training.sqlite")
    sessions = SessionManager(store)
    workers = AttemptWorkerCoordinator(
        store, runtime / "workers"
    )
    readiness = TrustedReadinessCoordinator(
        store, sessions, workers
    )
    active_gate = AuthorizedActiveStart(
        store, sessions, workers
    )
    slots = RuntimeSlotAllocator(
        runtime / "slot-leases", max_slots=3
    )
    instructor = Actor(
        "dev014-instructor", Role.INSTRUCTOR
    )

    stop_event = threading.Event()
    old_int = signal.signal(
        signal.SIGINT,
        lambda *_: stop_event.set(),
    )
    old_term = signal.signal(
        signal.SIGTERM,
        lambda *_: stop_event.set(),
    )

    attempt = None
    lease = None
    recorder = None
    gateway = None
    worker_started = False
    rc = 0
    evidence_package = None

    try:
        sid = sessions.create_session(
            instructor, "student-1"
        )
        eid = sessions.create_exercise(
            instructor,
            sid,
            "dev014-real-feather-proof-v1",
        )
        attempt = sessions.create_attempt(
            instructor, eid, "omni-1"
        )
        lease = slots.claim(
            attempt_id=attempt.id,
            session_id=sid,
            aircraft_id=attempt.aircraft_id,
            preferred_slot=1,
        )
        if not slot_ports_free(lease):
            raise CoreError(
                "slot 1 ports busy before DEV-014"
            )

        recorder = AttemptEvidenceRecorder(
            runtime / "evidence",
            AttemptIdentity(
                session_id=sid,
                exercise_id=eid,
                attempt_id=attempt.id,
                aircraft_id=attempt.aircraft_id,
                generation=attempt.generation,
            ),
            configuration={
                "dev": "DEV-014",
                "proof": "real_feather_client",
                "flight_commands_enabled": False,
                "slot": 1,
            },
        )
        recorder.record_lifecycle(
            "Setup", revision=attempt.revision
        )
        recorder.start_mavlink_tlog(
            bind_host="127.0.0.1",
            port=lease.slot.mav_client_udp,
        )

        gateway = EvidenceLanTelemetryGateway(
            "127.0.0.1", port
        )
        assignment = gateway.register_assignment(
            student_id="student-1",
            session_id=sid,
            attempt_id=attempt.id,
            aircraft_id=attempt.aircraft_id,
            generation=attempt.generation,
        )
        gateway.register_evidence_principal(
            assignment,
            recorder,
            station_id="student-station-1",
            training_role="FLIGHT",
        )

        with FGObserver(lease.slot.fg_udp) as fg:
            health = workers.start(
                instructor,
                attempt.id,
                argv=worker_argv(
                    base,
                    source,
                    jsbsim,
                    template_json,
                    lease,
                    runtime / "direct" / attempt.id,
                ),
                cwd=base,
            )
            worker_started = True

            ev = evidence(
                source,
                base,
                fg,
                workers,
                instructor,
                attempt,
                lease,
                60.0,
            )
            ready, _ = readiness.mark_ready(
                instructor,
                attempt.id,
                ev,
                expected_revision=attempt.revision,
            )
            recorder.record_lifecycle(
                "Ready", revision=ready.revision
            )

            ev2 = evidence(
                source,
                base,
                fg,
                workers,
                instructor,
                attempt,
                lease,
                15.0,
            )
            active, _ = active_gate.start_active(
                instructor,
                attempt.id,
                ev2,
                expected_revision=ready.revision,
            )
            recorder.record_lifecycle(
                "Active", revision=active.revision
            )

            gateway.start()
            credentials_file.parent.mkdir(
                parents=True, exist_ok=True
            )
            credentials_file.write_text(
                json.dumps(
                    {
                        "server": (
                            f"ws://127.0.0.1:{port}"
                        ),
                        "student": "student-1",
                        "token": assignment.token,
                        "station": "student-station-1",
                        "role": "FLIGHT",
                        "attempt_id": attempt.id,
                        "aircraft_id": attempt.aircraft_id,
                    },
                    indent=2,
                )
                + "\n"
            )
            print(
                "DEV-014 FEATHER SERVER READY:",
                f"pid={health.pid}",
                f"ws=127.0.0.1:{port}",
                f"attempt={attempt.id}",
                flush=True,
            )

            while not stop_event.is_set():
                health_now = workers.health(
                    instructor, attempt.id
                )
                if not health_now.healthy:
                    raise CoreError(
                        "worker became unhealthy "
                        f"rc={health_now.exit_code}"
                    )
                pose = fg.drain()
                if pose is not None:
                    recorder.record_state(
                        _server_state(pose),
                        source="FGNetFDM",
                        validity="valid",
                    )
                    gateway.publish_telemetry(
                        assignment,
                        _client_telemetry(pose),
                        quality={
                            "pose_source": "fg",
                            "dev": "DEV-014",
                        },
                    )
                time.sleep(1.0 / 30.0)

            current = store.attempt(attempt.id)
            ended = sessions.transition(
                instructor,
                attempt.id,
                State.ENDED,
                expected_revision=current.revision,
                reason=(
                    "DEV-014 real Feather proof complete"
                ),
            )
            recorder.record_lifecycle(
                "Ended",
                revision=ended.revision,
                reason=(
                    "DEV-014 real Feather proof complete"
                ),
            )

        gateway.stop()
        gateway = None
        workers.stop(
            instructor, attempt.id, timeout=15.0
        )
        worker_started = False
        wait_tcp_cleanup(lease, timeout=12.0)
        slots.release(attempt.id)

        index_path = recorder.finalize(
            status="complete"
        )
        evidence_package = index_path.parent
        index = json.loads(index_path.read_text())
        channels = index.get("channels") or {}
        received = channels.get(
            "clients/student-station-1/received_state",
            {},
        )
        events = channels.get(
            "clients/student-station-1/events", {}
        )
        clock = channels.get(
            "clients/student-station-1/clock", {}
        )
        summary = {
            "accepted": (
                received.get("records_written", 0) > 0
                and events.get("records_written", 0) > 0
            ),
            "evidence_package": str(
                evidence_package
            ),
            "attempt_id": attempt.id,
            "server_states": (
                channels.get(
                    "server/state", {}
                ).get("records_written", 0)
            ),
            "received_states": (
                received.get("records_written", 0)
            ),
            "client_events": (
                events.get("records_written", 0)
            ),
            "clock_samples": (
                clock.get("records_written", 0)
            ),
            "tlog_frames": (
                (index.get("mavlink_tlog") or {}).get(
                    "frames", 0
                )
            ),
            "overall_complete": index.get(
                "overall_complete"
            ),
        }
        if not summary["accepted"]:
            rc = 141
        summary_file.parent.mkdir(
            parents=True, exist_ok=True
        )
        summary_file.write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        print(
            "REAL FEATHER EVIDENCE:",
            f"received_states={summary['received_states']}",
            f"client_events={summary['client_events']}",
            f"clock_samples={summary['clock_samples']}",
            flush=True,
        )
        print(
            "EVIDENCE PACKAGE:",
            evidence_package,
            flush=True,
        )
        if summary["accepted"]:
            print(
                "STATUS: DEV-014 FEATHER CLIENT "
                "EVIDENCE VERIFIED",
                flush=True,
            )
        else:
            print(
                "STATUS: DEV-014 FEATHER CLIENT "
                "EVIDENCE NOT VERIFIED",
                flush=True,
            )

    except Exception as exc:
        rc = 142
        print(
            "STATUS: DEV-014 FEATHER SERVER FAILED:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
    finally:
        if gateway is not None:
            try:
                gateway.stop()
            except Exception:
                pass
        if worker_started and attempt is not None:
            try:
                workers.stop(
                    instructor,
                    attempt.id,
                    timeout=15.0,
                )
            except Exception:
                pass
        if attempt is not None:
            try:
                slots.release(attempt.id)
            except Exception:
                pass
        if (
            recorder is not None
            and not recorder._finalized
        ):
            try:
                index_path = recorder.abort(
                    "DEV-014 Feather proof aborted"
                )
                evidence_package = (
                    index_path.parent
                )
            except Exception:
                pass
        store.close()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--port", type=int, default=9110)
    p.add_argument(
        "--credentials-file", required=True
    )
    p.add_argument(
        "--summary-file", required=True
    )
    a = p.parse_args(argv)
    return run_server(
        base=Path(a.base).expanduser().resolve(),
        source_root=Path(
            a.source
        ).expanduser().resolve(),
        port=a.port,
        credentials_file=Path(
            a.credentials_file
        ).expanduser().resolve(),
        summary_file=Path(
            a.summary_file
        ).expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
