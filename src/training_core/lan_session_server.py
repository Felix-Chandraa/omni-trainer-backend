"""DEV-011 persistent LAN demo server for 1..3 Student clients."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import socket
import threading
import time
import uuid

from .active_start import AuthorizedActiveStart
from .fg_adapter import FGObserver
from .lan_protocol import Assignment, LanTelemetryGateway
from .models import Actor, CoreError, Role, State
from .multi_live_trial import evidence, slot_ports_free, worker_argv
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


def guess_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "SERVER_IP"
    finally:
        sock.close()


def pose_payload(pose, home: dict | None) -> dict:
    agl = pose.agl_raw_m
    airspeed = None
    if pose.vcas_kt is not None:
        airspeed = float(pose.vcas_kt) * KNOT_TO_MPS
    payload = {
        "lat": pose.lat_deg,
        "lon": pose.lon_deg,
        "alt": agl,
        "alt_msl": pose.alt_msl_m,
        "agl": agl,
        "agl_raw": agl,
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
        "sat": None,
        "gps_fix_type": None,
        "bat": None,
        "bat_pct": None,
        "fuel_pct": None,
        "throttle": None,
        "srv1": None,
        "srv2": None,
        "srv4": None,
    }
    if home:
        payload.update({
            "home_lat": home["lat"],
            "home_lon": home["lon"],
            "home_alt": home["alt_msl"],
            "home_source": "dev011_first_fg",
        })
    return payload


def run_server(*, base: Path, source_root: Path, students: int,
               bind: str, port: int, startup_timeout: float) -> int:
    if students not in (1, 2, 3):
        raise CoreError("students must be 1, 2, or 3")

    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())
    proven_log = find_latest_working_worker_log(base)
    template = extract_arduplane_template(proven_log)
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev011" / f"{stamp}-{uuid.uuid4().hex[:8]}"
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
    instructor = Actor("dev011-instructor", Role.INSTRUCTOR)
    gateway = LanTelemetryGateway(bind, port)

    records: list[dict] = []
    started: list[str] = []
    stop_event = threading.Event()
    return_code = 0

    def request_stop(*_args):
        stop_event.set()

    old_sigint = signal.signal(signal.SIGINT, request_stop)
    old_sigterm = signal.signal(signal.SIGTERM, request_stop)

    result: dict = {
        "students": students,
        "bind": bind,
        "port": port,
        "protocol": "omni.training.v1",
    }

    try:
        for idx in range(1, students + 1):
            student_id = f"student-{idx}"
            sid = sessions.create_session(instructor, student_id)
            eid = sessions.create_exercise(instructor, sid, "dev011-lan-cesium-v1")
            attempt = sessions.create_attempt(instructor, eid, f"omni-{idx}")
            lease = slots.claim(
                attempt_id=attempt.id,
                session_id=sid,
                aircraft_id=attempt.aircraft_id,
                preferred_slot=idx,
            )
            if not slot_ports_free(lease):
                raise CoreError(f"slot {idx} ports busy before launch")
            records.append({
                "student_id": student_id,
                "session_id": sid,
                "exercise_id": eid,
                "attempt": attempt,
                "lease": lease,
                "home": None,
            })

        with ExitStack() as stack:
            fg = {
                r["attempt"].id: stack.enter_context(FGObserver(r["lease"].slot.fg_udp))
                for r in records
            }

            for r in records:
                attempt = r["attempt"]
                lease = r["lease"]
                health = workers.start(
                    instructor,
                    attempt.id,
                    argv=worker_argv(
                        base, source, jsbsim, template_json, lease,
                        runtime / "direct" / attempt.id,
                    ),
                    cwd=base,
                )
                started.append(attempt.id)
                r["health"] = health
                print(
                    f"{r['student_id']} worker started",
                    f"pid={health.pid}",
                    f"instance={lease.slot.instance}",
                    flush=True,
                )

                ev = evidence(
                    source, base, fg[attempt.id], workers, instructor,
                    attempt, lease, min(startup_timeout, 60.0),
                )
                ready, _ = readiness.mark_ready(
                    instructor, attempt.id, ev,
                    expected_revision=attempt.revision,
                )
                r["ready"] = ready

                ev_active = evidence(
                    source, base, fg[attempt.id], workers, instructor,
                    attempt, lease, 15.0,
                )
                active, _ = active_gate.start_active(
                    instructor, attempt.id, ev_active,
                    expected_revision=ready.revision,
                )
                r["active"] = active
                assignment = gateway.register_assignment(
                    student_id=r["student_id"],
                    session_id=r["session_id"],
                    attempt_id=attempt.id,
                    aircraft_id=attempt.aircraft_id,
                    generation=attempt.generation,
                )
                r["assignment"] = assignment
                print(
                    f"{r['student_id']} ACTIVE",
                    f"aircraft={attempt.aircraft_id}",
                    f"slot={lease.slot.slot}",
                    flush=True,
                )

            gateway.start()
            server_ip = guess_lan_ip()
            print()
            print("=== OMNI DEV-011 LAN GATEWAY READY ===")
            print(f"WebSocket bind : ws://{bind}:{port}")
            print(f"Suggested LAN  : ws://{server_ip}:{port}")
            print("Protocol       : omni.training.v1")
            print("Remote commands: DISABLED (read-only)")
            print()
            print("COPY THESE VALUES TO EACH STUDENT CLIENT:")
            for r in records:
                a: Assignment = r["assignment"]
                print(
                    f"  {a.student_id}: token={a.token}",
                    f"aircraft={a.aircraft_id}",
                    f"attempt={a.attempt_id}",
                )
            print()
            print("Keep this terminal open. Ctrl+C stops DEV-011 cleanly.")
            print()

            result["assignments"] = [r["assignment"].public() for r in records]
            (runtime / "server.json").write_text(
                json.dumps(result, indent=2, sort_keys=True)
            )

            while not stop_event.is_set():
                for r in records:
                    attempt = r["attempt"]
                    health = workers.health(instructor, attempt.id)
                    if not health.healthy:
                        raise CoreError(
                            f"{r['student_id']} worker unhealthy rc={health.exit_code}"
                        )
                    pose = fg[attempt.id].drain()
                    if pose is None:
                        continue
                    if r["home"] is None:
                        r["home"] = {
                            "lat": pose.lat_deg,
                            "lon": pose.lon_deg,
                            "alt_msl": pose.alt_msl_m,
                        }
                    gateway.publish_telemetry(
                        r["assignment"],
                        pose_payload(pose, r["home"]),
                        quality={
                            "pose_source": "fg",
                            "fg_rx_total": fg[attempt.id].rx_total,
                            "fg_bad_total": fg[attempt.id].bad_total,
                        },
                    )
                time.sleep(1.0 / 30.0)

    except Exception as exc:
        print("STATUS: DEV-011 SERVER FAILED:", f"{type(exc).__name__}: {exc}")
        result["error"] = f"{type(exc).__name__}: {exc}"
        return_code = 111
    finally:
        gateway.stop()

        for r in reversed(records):
            attempt = r["attempt"]
            if attempt.id in started:
                try:
                    current = store.attempt(attempt.id)
                    if current.state != State.ENDED:
                        sessions.transition(
                            instructor,
                            attempt.id,
                            State.ENDED,
                            expected_revision=current.revision,
                            reason="DEV-011 LAN server shutdown",
                        )
                except Exception as exc:
                    print("end transition warning:", r["student_id"], exc)
                try:
                    stopped = workers.stop(instructor, attempt.id, timeout=15.0)
                    print(
                        f"{r['student_id']} stopped rc={stopped.exit_code}",
                        flush=True,
                    )
                except Exception as exc:
                    print("worker stop warning:", r["student_id"], exc)
                started.remove(attempt.id)
            slots.release(attempt.id)

        result["remaining_leases"] = [x.attempt_id for x in slots.snapshot()]
        result["stopped_unix"] = time.time()
        try:
            (runtime / "server-result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True)
            )
        except Exception:
            pass
        store.close()
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        print("DEV-011 LAN server shutdown complete.")

    return return_code


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--students", type=int, default=3, choices=(1, 2, 3))
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--startup-timeout", type=float, default=75.0)
    a = p.parse_args(argv)
    return run_server(
        base=Path(a.base).expanduser().resolve(),
        source_root=Path(a.source).expanduser().resolve(),
        students=a.students,
        bind=a.bind,
        port=a.port,
        startup_timeout=a.startup_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
