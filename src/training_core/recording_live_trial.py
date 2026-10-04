"""DEV-012 one-worker live Attempt evidence proof.

This trial sends no flight command. It records:
- real FG authoritative trajectory/state;
- real raw MAVLink frames into server/aircraft.tlog;
- real lifecycle transitions;
- a server marker event;
- synthetic *recording fixtures* for two client-experience events, clearly
  labelled engineering_fixture=true. These prove attribution/schema only;
  they are not claimed as real HOTAS capture.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import time
import uuid

from .active_start import AuthorizedActiveStart
from .actor_identity import actor_identifier
from .evidence_recorder import AttemptEvidenceRecorder, AttemptIdentity
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
from .omni_launcher_adapter import choose_jsbsim, source_from_root
from .readiness import TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator

KNOT_TO_MPS = 0.514444


def _pose_dict(pose) -> dict:
    return {
        "lat_deg": pose.lat_deg,
        "lon_deg": pose.lon_deg,
        "alt_msl_m": pose.alt_msl_m,
        "agl_m": pose.agl_raw_m,
        "roll_deg": pose.roll_deg,
        "pitch_deg": pose.pitch_deg,
        "yaw_deg": pose.yaw_deg,
        "vcas_mps": (
            None if pose.vcas_kt is None else float(pose.vcas_kt) * KNOT_TO_MPS
        ),
        "climb_mps": pose.climb_mps,
        "frame": "FGNetFDM",
    }


def run_trial(base: Path, source_root: Path, live_seconds: float) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())
    proven_log = find_latest_working_worker_log(base)
    template = extract_arduplane_template(proven_log)
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev012" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    runtime.mkdir(parents=True, exist_ok=False)
    template_json = runtime / "arduplane-template.json"
    template_json.write_text(json.dumps({
        "source_worker_log": str(proven_log),
        "arduplane_argv": list(template),
    }, indent=2, sort_keys=True))

    store = Store(runtime / "training.sqlite")
    sessions = SessionManager(store)
    workers = AttemptWorkerCoordinator(store, runtime / "workers")
    readiness = TrustedReadinessCoordinator(store, sessions, workers)
    active_gate = AuthorizedActiveStart(store, sessions, workers)
    slots = RuntimeSlotAllocator(runtime / "slot-leases", max_slots=3)
    instructor = Actor("dev012-instructor", Role.INSTRUCTOR)

    recorder = None
    attempt = None
    lease = None
    worker_started = False
    accepted = False

    try:
        sid = sessions.create_session(instructor, "student-1")
        eid = sessions.create_exercise(instructor, sid, "dev012-recorder-v1")
        attempt = sessions.create_attempt(instructor, eid, "omni-1")
        lease = slots.claim(
            attempt_id=attempt.id,
            session_id=sid,
            aircraft_id=attempt.aircraft_id,
            preferred_slot=1,
        )
        if not slot_ports_free(lease):
            raise CoreError("slot 1 ports busy before DEV-012")

        identity = AttemptIdentity(
            session_id=sid,
            exercise_id=eid,
            attempt_id=attempt.id,
            aircraft_id=attempt.aircraft_id,
            generation=attempt.generation,
        )
        recorder = AttemptEvidenceRecorder(
            runtime / "evidence",
            identity,
            configuration={
                "dev": "DEV-012",
                "flight_commands_sent": False,
                "source_root": str(source_root),
                "slot": lease.slot.slot,
                "fg_udp": lease.slot.fg_udp,
                "mavlink_tlog_udp": lease.slot.mav_client_udp,
                "mavlink_readiness_udp": lease.slot.mav_monitor_udp,
                "client_event_fixture_only": True,
            },
        )
        recorder.record_lifecycle("Setup", revision=attempt.revision)
        recorder.start_mavlink_tlog(
            bind_host="127.0.0.1",
            port=lease.slot.mav_client_udp,
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
            print(
                f"worker started pid={health.pid} "
                f"FG={lease.slot.fg_udp} "
                f"TLOG_UDP={lease.slot.mav_client_udp} "
                f"READINESS_MAV={lease.slot.mav_monitor_udp}",
                flush=True,
            )

            ev = evidence(
                source, base, fg, workers, instructor, attempt, lease, 60.0
            )
            ready, _ = readiness.mark_ready(
                instructor,
                attempt.id,
                ev,
                expected_revision=attempt.revision,
            )
            recorder.record_lifecycle("Ready", revision=ready.revision)

            ev2 = evidence(
                source, base, fg, workers, instructor, attempt, lease, 15.0
            )
            active, _ = active_gate.start_active(
                instructor,
                attempt.id,
                ev2,
                expected_revision=ready.revision,
            )
            recorder.record_lifecycle("Active", revision=active.revision)
            recorder.record_event(
                "dev012_live_recording_started",
                {
                    "worker_pid": health.pid,
                    "slot": lease.slot.slot,
                    "engineering_trial": True,
                },
                actor_id=actor_identifier(instructor),
                station_id="server",
            )

            # Establish a deterministic engineering clock-mapping fixture.
            # Real LAN clients will provide these timestamps during handshake.
            c0 = time.monotonic_ns()
            s_recv = time.monotonic_ns()
            s_send = s_recv + 100_000
            c1 = c0 + 2_000_000
            recorder.register_client_clock_sample(
                station_id="student-station-1",
                client_send_mono_ns=c0,
                server_recv_mono_ns=s_recv,
                server_send_mono_ns=s_send,
                client_recv_mono_ns=c1,
                server_recv_utc_ns=time.time_ns(),
            )

            deadline = time.monotonic() + max(3.0, live_seconds)
            samples = 0
            fixture_done = False
            while time.monotonic() < deadline:
                pose = fg.drain()
                if pose is not None:
                    ok = recorder.record_state(
                        _pose_dict(pose),
                        source="FGNetFDM",
                        validity="valid",
                    )
                    if ok:
                        samples += 1

                if not fixture_done and samples >= 10:
                    now_client = time.monotonic_ns()
                    recorder.record_client_received_state(
                        station_id="student-station-1",
                        actor_id="student-1",
                        state={
                            "engineering_fixture": True,
                            "note": "represents state delivered to client; LAN hook follows in integration",
                        },
                        source_sequence=samples,
                        client_mono_ns=now_client,
                        client_utc_ns=time.time_ns(),
                    )
                    recorder.record_client_event(
                        station_id="student-station-1",
                        actor_id="student-1",
                        role="FLIGHT",
                        scope="flight",
                        action="input_sample",
                        data={
                            "engineering_fixture": True,
                            "roll": 0.12,
                            "pitch": -0.03,
                            "yaw": 0.00,
                            "throttle": 0.55,
                            "note": "attribution/schema fixture only; not sent to aircraft",
                        },
                        client_mono_ns=now_client,
                        client_utc_ns=time.time_ns(),
                    )
                    recorder.record_client_event(
                        station_id="student-station-1",
                        actor_id="student-1",
                        role="FLIGHT",
                        scope="ui",
                        action="view_changed",
                        data={
                            "engineering_fixture": True,
                            "view": "FOLLOW",
                            "note": "client-experience fixture only",
                        },
                        client_mono_ns=now_client + 1_000_000,
                        client_utc_ns=time.time_ns(),
                    )
                    fixture_done = True
                time.sleep(1.0 / 60.0)

            if samples < 20:
                raise CoreError(f"insufficient recorded FG states: {samples}")

            current = store.attempt(attempt.id)
            ended = sessions.transition(
                instructor,
                attempt.id,
                State.ENDED,
                expected_revision=current.revision,
                reason="DEV-012 live recording proof complete",
            )
            recorder.record_lifecycle(
                "Ended",
                revision=ended.revision,
                reason="DEV-012 live recording proof complete",
            )

        stop = workers.stop(instructor, attempt.id, timeout=15.0)
        worker_started = False
        wait_tcp_cleanup(lease, timeout=12.0)
        slots.release(attempt.id)

        index_path = recorder.finalize(status="complete")
        index = json.loads(index_path.read_text())
        package = index_path.parent

        state_stats = index["channels"].get("server/state", {})
        client_stats = index["channels"].get(
            "clients/student-station-1/events", {}
        )
        tlog = index.get("mavlink_tlog") or {}

        if state_stats.get("records_written", 0) < 20:
            raise CoreError("recorded state channel too small")
        if client_stats.get("records_written", 0) < 2:
            raise CoreError("client attribution fixture not recorded")
        if tlog.get("frames", 0) < 10:
            raise CoreError(f"MAVLink tlog frame count too small: {tlog}")
        if not (package / "server" / "aircraft.tlog").is_file():
            raise CoreError("aircraft.tlog missing")
        if not index.get("overall_complete"):
            raise CoreError("evidence package marked incomplete")
        if slots.snapshot():
            raise CoreError("slot lease remains after cleanup")
        if not slot_ports_free(lease):
            raise CoreError("slot 1 not reusable after recorder/worker cleanup")

        print(
            "RECORDED:",
            f"state={state_stats.get('records_written')}",
            f"tlog_frames={tlog.get('frames')}",
            f"client_events={client_stats.get('records_written')}",
            flush=True,
        )
        print("EVIDENCE PACKAGE:", package)
        print("INDEX:", index_path)
        print("STATUS: DEV-012 ACCEPTED (engineering)")
        print("Proof: real FG trajectory + real MAVLink .tlog + lifecycle + client attribution schema")
        print("No flight command was sent.")
        accepted = True
        return 0

    except Exception as exc:
        print(f"STATUS: DEV-012 FAILED: {type(exc).__name__}: {exc}")
        return 120
    finally:
        if worker_started and attempt is not None:
            try:
                workers.stop(instructor, attempt.id, timeout=15.0)
            except Exception as exc:
                print("cleanup worker warning:", exc)
        if attempt is not None:
            try:
                slots.release(attempt.id)
            except Exception:
                pass
        if recorder is not None and not recorder._finalized:
            try:
                recorder.abort(
                    "DEV-012 accepted" if accepted else "DEV-012 live trial aborted"
                )
            except Exception as exc:
                print("recorder finalize warning:", exc)
        store.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--live-seconds", type=float, default=6.0)
    a = p.parse_args(argv)
    return run_trial(
        Path(a.base).expanduser().resolve(),
        Path(a.source).expanduser().resolve(),
        a.live_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
