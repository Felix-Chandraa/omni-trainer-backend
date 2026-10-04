"""DEV-015 one-student real Feather/HOTAS control server."""
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
from .dev015_live_trial import (
    install_control_endpoint,
    state_dict,
    visual_dict,
)
from .evidence_recorder import AttemptEvidenceRecorder, AttemptIdentity
from .fg_adapter import FGObserver
from .flight_command_router import FlightCommandRouter
from .models import Actor, CoreError, Role, State
from .multi_live_trial import evidence, slot_ports_free, wait_tcp_cleanup, worker_argv
from .multi_runtime import (
    RuntimeSlotAllocator,
    extract_arduplane_template,
    find_latest_working_worker_log,
    validate_template,
)
from .omni_launcher_adapter import choose_jsbsim, source_from_root
from .readiness import TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator
from .evidence_extensions import attach_ardupilot_bins  # DEV016_BIN_ATTACHMENT
import struct


# DEV017_REV13_MANUAL_ACTUATORS
# Read-only observation of actual ArduPilot actuator outputs.
# No joystick inference, no extra MAVLink socket, no command-path change.
def _dev017_decode_actuator_frame(frame: bytes) -> dict:
    if not isinstance(frame, (bytes, bytearray)) or len(frame) < 8:
        return {}

    stx = frame[0]
    payload_len = frame[1]
    if stx == 0xFE:
        payload_start = 6
        msgid = frame[5]
    elif stx == 0xFD:
        if len(frame) < 12:
            return {}
        payload_start = 10
        msgid = frame[7] | (frame[8] << 8) | (frame[9] << 16)
    else:
        return {}

    payload_end = payload_start + payload_len
    if payload_end > len(frame):
        return {}
    payload = frame[payload_start:payload_end]

    # MAVLink common SERVO_OUTPUT_RAW = 36.
    if msgid == 36 and len(payload) >= 12:
        return {
            'srv1': int(struct.unpack_from('<H', payload, 4)[0]),
            'srv2': int(struct.unpack_from('<H', payload, 6)[0]),
            'srv3': int(struct.unpack_from('<H', payload, 8)[0]),
            'srv4': int(struct.unpack_from('<H', payload, 10)[0]),
        }

    # MAVLink common VFR_HUD = 74; throttle is uint16 at wire offset 18.
    if msgid == 74 and len(payload) >= 20:
        return {'throttle': float(struct.unpack_from('<H', payload, 18)[0])}

    return {}


def _dev017_install_actuator_observer(endpoint) -> None:
    if getattr(endpoint, '_dev017_actuator_observer_installed', False):
        return

    latest = {
        'srv1': None,
        'srv2': None,
        'srv3': None,
        'srv4': None,
        'throttle': None,
    }
    original = endpoint._observe_frame

    def observed(frame: bytes) -> None:
        original(frame)
        update = _dev017_decode_actuator_frame(frame)
        if update:
            with endpoint._state:
                latest.update(update)

    def snapshot() -> dict:
        with endpoint._state:
            return dict(latest)

    endpoint._observe_frame = observed
    endpoint.actuator_snapshot = snapshot
    endpoint._dev017_actuator_observer_installed = True

def run_server(
    *,
    base: Path,
    source_root: Path,
    port: int,
    credentials_file: Path,
    summary_file: Path,
) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())
    proven = find_latest_working_worker_log(base)
    template = extract_arduplane_template(proven)
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev015-control" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    runtime.mkdir(parents=True, exist_ok=False)
    template_json = runtime / "arduplane-template.json"
    template_json.write_text(
        json.dumps(
            {"source_worker_log": str(proven), "arduplane_argv": list(template)},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    store = Store(runtime / "training.sqlite")
    sessions = SessionManager(store)
    workers = AttemptWorkerCoordinator(store, runtime / "workers")
    readiness = TrustedReadinessCoordinator(store, sessions, workers)
    active_gate = AuthorizedActiveStart(store, sessions, workers)
    slots = RuntimeSlotAllocator(runtime / "slot-leases", max_slots=3)
    instructor = Actor("dev015-instructor", Role.INSTRUCTOR)

    stop_event = threading.Event()
    old_int = signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    old_term = signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    attempt = None
    lease = None
    recorder = None
    endpoint = None
    router = None
    gateway = None
    worker_started = False
    rc = 0

    try:
        sid = sessions.create_session(instructor, "student-1")
        eid = sessions.create_exercise(instructor, sid, "dev015-manual-flight-v1")
        attempt = sessions.create_attempt(instructor, eid, "omni-1")
        lease = slots.claim(
            attempt_id=attempt.id,
            session_id=sid,
            aircraft_id=attempt.aircraft_id,
            preferred_slot=1,
        )
        if not slot_ports_free(lease):
            raise CoreError("slot 1 busy")

        recorder = AttemptEvidenceRecorder(
            runtime / "evidence",
            AttemptIdentity(
                sid,
                eid,
                attempt.id,
                attempt.aircraft_id,
                attempt.generation,
            ),
            configuration={
                "dev": "DEV-015",
                "proof": "manual_feather_or_hotas_control",
                "deadman_ms": 350,
                "flight_commands_enabled": True,
            },
        )
        recorder.record_lifecycle("Setup", revision=attempt.revision)
        endpoint = install_control_endpoint(recorder, lease.slot.mav_client_udp)
        _dev017_install_actuator_observer(endpoint)

        router = FlightCommandRouter(store, deadman_ms=350)
        gateway = EvidenceLanTelemetryGateway("127.0.0.1", port)
        gateway.set_command_router(router)
        assignment = gateway.register_assignment(
            student_id="student-1",
            session_id=sid,
            attempt_id=attempt.id,
            aircraft_id=attempt.aircraft_id,
            generation=attempt.generation,
        )
        principal = gateway.register_evidence_principal(
            assignment,
            recorder,
            station_id="student-station-1",
            training_role="FLIGHT",
        )
        router.register_attempt(
            attempt_id=attempt.id,
            generation=attempt.generation,
            recorder=recorder,
            endpoint=endpoint,
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

            ev = evidence(source, base, fg, workers, instructor, attempt, lease, 60.0)
            ready, _ = readiness.mark_ready(
                instructor,
                attempt.id,
                ev,
                expected_revision=attempt.revision,
            )
            recorder.record_lifecycle("Ready", revision=ready.revision)

            ev2 = evidence(source, base, fg, workers, instructor, attempt, lease, 15.0)
            active, _ = active_gate.start_active(
                instructor,
                attempt.id,
                ev2,
                expected_revision=ready.revision,
            )
            recorder.record_lifecycle("Active", revision=active.revision)
            endpoint.wait_peer(timeout=5.0)

            lease_info = router.grant_principal(
                principal,
                reason="DEV-015 manual Flight station assignment",
            )
            gateway.start()

            credentials_file.parent.mkdir(parents=True, exist_ok=True)
            credentials_file.write_text(
                json.dumps(
                    {
                        "server": f"ws://127.0.0.1:{port}",
                        "student": "student-1",
                        "token": assignment.token,
                        "attempt_id": attempt.id,
                        "aircraft_id": attempt.aircraft_id,
                        "authority_epoch": lease_info["epoch"],
                        "generation": attempt.generation,
                        "deadman_ms": 350,
                    },
                    indent=2,
                )
                + "\n"
            )
            print(
                "DEV-015 CONTROL SERVER READY:",
                f"pid={health.pid}",
                f"ws=127.0.0.1:{port}",
                f"attempt={attempt.id}",
                f"authority_epoch={lease_info['epoch']}",
                flush=True,
            )

            while not stop_event.is_set():
                hb = workers.health(instructor, attempt.id)
                if not hb.healthy:
                    raise CoreError(f"worker unhealthy rc={hb.exit_code}")
                pose = fg.drain()
                if pose is not None:
                    recorder.record_state(state_dict(pose), source="FGNetFDM")
                    gateway.publish_telemetry(
                        assignment,
                        {**visual_dict(pose), **endpoint.actuator_snapshot()},
                        quality={"pose_source": "fg", "dev": "DEV-015"},
                    )
                time.sleep(1.0 / 30.0)

            # Safety shutdown order: release -> disarm -> Ended.
            router.release_attempt(attempt.id, reason="manual_server_shutdown")
            if endpoint.vehicle_status().armed:
                endpoint.send_arm_disarm(False)
                endpoint.wait_armed(False, timeout=3.0)

            current = store.attempt(attempt.id)
            ended = sessions.transition(
                instructor,
                attempt.id,
                State.ENDED,
                expected_revision=current.revision,
                reason="DEV-015 manual control session closed",
            )
            recorder.record_lifecycle("Ended", revision=ended.revision)

        gateway.stop()
        gateway = None
        router.shutdown()
        router = None
        workers.stop(instructor, attempt.id, timeout=15.0)
        worker_started = False
        wait_tcp_cleanup(lease, timeout=12.0)
        slots.release(attempt.id)

        # DEV016_BIN_ATTACHMENT: worker is stopped, so DataFlash is flushed.
        bin_evidence = attach_ardupilot_bins(recorder, runtime, attempt.id)
        print("ARDUPILOT BIN EVIDENCE:", len(bin_evidence), "file(s)")
        index_path = recorder.finalize(status="complete")
        index = json.loads(index_path.read_text())
        summary = {
            "accepted": bool(index.get("overall_complete")),
            "evidence_package": str(index_path.parent),
            "attempt_id": attempt.id,
            "commands": index["channels"].get("server/commands", {}).get("records_written", 0),
            "states": index["channels"].get("server/state", {}).get("records_written", 0),
            "client_events": index["channels"].get(
                "clients/student-station-1/events", {}
            ).get("records_written", 0),
            "received_states": index["channels"].get(
                "clients/student-station-1/received_state", {}
            ).get("records_written", 0),
            "tlog_frames": (index.get("mavlink_tlog") or {}).get("frames", 0),
            "final_armed": endpoint.vehicle_status().armed,
            "release_count": endpoint.release_count,
        }
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(json.dumps(summary, indent=2) + "\n")
        print("EVIDENCE PACKAGE:", index_path.parent)
        print("STATUS: DEV-015 MANUAL CONTROL SESSION FINALIZED")
        return 0

    except Exception as exc:
        rc = 152
        print("STATUS: DEV-015 CONTROL SERVER FAILED:", f"{type(exc).__name__}: {exc}")
        return rc
    finally:
        if router is not None:
            try:
                router.shutdown()
            except Exception:
                pass
        if gateway is not None:
            try:
                gateway.stop()
            except Exception:
                pass
        if endpoint is not None:
            try:
                endpoint.release_override()
            except Exception:
                pass
            try:
                if endpoint.vehicle_status().armed:
                    endpoint.send_arm_disarm(False)
                    endpoint.wait_armed(False, timeout=2.0)
            except Exception:
                pass
        if worker_started and attempt is not None:
            try:
                workers.stop(instructor, attempt.id, timeout=15.0)
            except Exception:
                pass
        if attempt is not None:
            try:
                slots.release(attempt.id)
            except Exception:
                pass
        if recorder is not None and not recorder._finalized:
            try:
                recorder.abort("DEV-015 manual server aborted")
            except Exception:
                pass
        store.close()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--port", type=int, default=9120)
    p.add_argument("--credentials-file", required=True)
    p.add_argument("--summary-file", required=True)
    a = p.parse_args(argv)
    return run_server(
        base=Path(a.base).expanduser().resolve(),
        source_root=Path(a.source).expanduser().resolve(),
        port=a.port,
        credentials_file=Path(a.credentials_file).expanduser().resolve(),
        summary_file=Path(a.summary_file).expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
