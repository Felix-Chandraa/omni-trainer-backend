"""DEV-008 live proof: trusted readiness -> authorized Active start."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import uuid

from .active_start import AuthorizedActiveStart
from .fg_adapter import FGObserver
from .models import Actor, CoreError, Role, State
from .omni_launcher_adapter import build_initial_plan, choose_jsbsim, source_from_root
from .omni_live_trial import raw_mav_probe, tcp_port_free, udp_port_free
from .readiness import ReadinessEvidence, TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator

FG_PORT = 5503
MAV_RX_PORT = 14555
TCP_SITL_PORTS = (5760, 5762)


def wait_fg(fg, workers, actor, attempt_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = workers.health(actor, attempt_id)
        if not health.healthy:
            raise CoreError(f"worker unhealthy during evidence collection: {health.exit_code}")
        pose = fg.drain()
        if pose is not None:
            return pose, time.monotonic()
        time.sleep(0.05)
    raise CoreError("FG evidence timeout")


def evidence_bundle(source, base, fg, workers, actor, attempt, timeout):
    pose, fg_seen = wait_fg(fg, workers, actor, attempt.id, timeout)
    mav = raw_mav_probe(
        source.venv_python,
        base / "src/training_core/raw_mav_probe.py",
        10.0,
    )
    if not mav.get("ok"):
        raise CoreError("MAV evidence failed: " + str(mav.get("error")))

    # Refresh FG after the blocking MAV heartbeat wait.
    latest = fg.drain()
    if latest is not None:
        pose = latest
        fg_seen = time.monotonic()

    health = workers.health(actor, attempt.id)
    now = time.monotonic()
    return ReadinessEvidence(
        attempt_id=attempt.id,
        aircraft_id=attempt.aircraft_id,
        generation=attempt.generation,
        worker_pid=health.pid,
        fg_seen_monotonic=fg_seen,
        fg_rx_total=fg.rx_total,
        fg_bad_total=fg.bad_total,
        fg_lat_deg=pose.lat_deg,
        fg_lon_deg=pose.lon_deg,
        fg_alt_msl_m=pose.alt_msl_m,
        mav_seen_monotonic=now,
        mav_system_id=int(mav["system_id"]),
        mav_component_id=int(mav["component_id"]),
        mav_armed=bool(mav["armed"]),
        mav_custom_mode=int(mav["custom_mode"]),
        created_monotonic=now,
    ), mav, health


def wait_cleanup(timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        leftover = [p for p in TCP_SITL_PORTS if not tcp_port_free("127.0.0.1", p)]
        if not leftover:
            return True, []
        time.sleep(0.1)
    leftover = [p for p in TCP_SITL_PORTS if not tcp_port_free("127.0.0.1", p)]
    return not leftover, leftover


def run_trial(base: Path, source_root: Path, startup_timeout: float) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())

    busy_tcp = [p for p in TCP_SITL_PORTS if not tcp_port_free("127.0.0.1", p)]
    if busy_tcp:
        print("STATUS: DEV-008 BLOCKED: busy TCP", busy_tcp)
        return 51
    if not udp_port_free("127.0.0.1", FG_PORT):
        print("STATUS: DEV-008 BLOCKED: UDP 5503 busy")
        return 52
    if not udp_port_free("127.0.0.1", MAV_RX_PORT):
        print("STATUS: DEV-008 BLOCKED: UDP 14555 busy")
        return 53

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev008" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    runtime.mkdir(parents=True, exist_ok=False)

    store = Store(runtime / "training.sqlite")
    sessions = SessionManager(store)
    workers = AttemptWorkerCoordinator(store, runtime / "workers")
    readiness = TrustedReadinessCoordinator(store, sessions, workers)
    active_gate = AuthorizedActiveStart(store, sessions, workers)

    instructor = Actor("dev008-instructor", Role.INSTRUCTOR)
    session_id = sessions.create_session(instructor, "dev008-student")
    exercise_id = sessions.create_exercise(instructor, session_id, "dev008-start-v1")
    attempt = sessions.create_attempt(instructor, exercise_id, "omni-1")

    plan = build_initial_plan(
        source,
        entry_script=base / "src/training_core/omni_managed_entry.py",
        jsbsim=jsbsim,
    )

    print("Attempt:", attempt.id, "generation=", attempt.generation, "state=", attempt.state.value)
    print("Contract: Setup→Ready requires trusted evidence; Ready→Active requires fresh revalidation")
    print("NO worker restart, NO flight command, NO automatic Active")

    started = False
    active_ok = False
    cleanup_ok = False
    result = {"attempt_id": attempt.id, "generation": attempt.generation}

    try:
        with FGObserver(FG_PORT) as fg:
            health0 = workers.start(
                instructor, attempt.id, argv=plan.entry_argv, cwd=plan.cwd
            )
            started = True
            print("worker started pid=", health0.pid)

            ready_ev, ready_mav, ready_health = evidence_bundle(
                source, base, fg, workers, instructor, attempt,
                min(startup_timeout, 60.0),
            )
            ready, ready_decision = readiness.mark_ready(
                instructor,
                attempt.id,
                ready_ev,
                expected_revision=attempt.revision,
            )
            print(
                "READY ACCEPTED:",
                f"Setup→{ready.state.value}",
                f"revision={ready.revision}",
                f"pid={ready_health.pid}",
                f"evidence={ready_decision.evidence_sha256[:12]}...",
            )

            # Revalidate after Ready. We intentionally collect a new bundle,
            # rather than blindly reusing the bundle that created Ready.
            start_ev, start_mav, start_health = evidence_bundle(
                source, base, fg, workers, instructor, attempt, 10.0
            )
            if start_health.pid != ready_health.pid:
                raise CoreError("worker PID changed between Ready and Active")
            if start_ev.generation != ready_ev.generation:
                raise CoreError("generation changed between Ready and Active")

            active, start_decision = active_gate.start_active(
                instructor,
                attempt.id,
                start_ev,
                expected_revision=ready.revision,
            )
            active_ok = True
            print(
                "ACTIVE ACCEPTED:",
                f"Ready→{active.state.value}",
                f"revision={active.revision}",
                f"same_pid={start_health.pid}",
                f"fresh_evidence={start_decision.evidence_sha256[:12]}...",
            )

            # Short continuity observation: Active state must not create a new worker.
            deadline = time.monotonic() + 2.0
            fg_before = fg.rx_total
            while time.monotonic() < deadline:
                current = workers.health(instructor, attempt.id)
                if not current.healthy or current.pid != start_health.pid:
                    raise CoreError("worker continuity lost after Active")
                fg.drain()
                time.sleep(0.05)
            print(
                "ACTIVE CONTINUITY:",
                f"pid={start_health.pid}",
                f"FG_delta={fg.rx_total - fg_before}",
                "worker_restart=NO",
            )

            ended = sessions.transition(
                instructor,
                attempt.id,
                State.ENDED,
                expected_revision=active.revision,
                reason="DEV-008 engineering trial complete",
            )
            result.update({
                "ready_revision": ready.revision,
                "active_revision": active.revision,
                "ended_revision": ended.revision,
                "worker_pid_ready": ready_health.pid,
                "worker_pid_active": start_health.pid,
                "active_evidence_sha256": start_decision.evidence_sha256,
            })

    except Exception as exc:
        print("STATUS: DEV-008 FAILED:", f"{type(exc).__name__}: {exc}")
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if started:
            try:
                stopped = workers.stop(instructor, attempt.id, timeout=5.0)
                print("worker stopped rc=", stopped.exit_code)
            except Exception as exc:
                print("worker stop error:", f"{type(exc).__name__}: {exc}")
                result["stop_error"] = f"{type(exc).__name__}: {exc}"

        cleanup_ok, leftover = wait_cleanup()
        result["cleanup_ok"] = cleanup_ok
        result["leftover_tcp_ports"] = leftover
        if cleanup_ok:
            print("cleanup verified: TCP 5760/5762 free")
        else:
            print("cleanup incomplete:", leftover)

        events = store.events(attempt.id)
        names = [e["event_type"] for e in events]
        result["events"] = names
        result["active_start_checked"] = "active_start.checked" in names
        result["active_start_accepted"] = "active_start.accepted" in names
        (runtime / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        store.close()

    event_ok = result.get("active_start_checked") and result.get("active_start_accepted")
    if active_ok and cleanup_ok and event_ok:
        print("STATUS: DEV-008 ACCEPTED (engineering)")
        print("Proof: authorized Ready→Active revalidated same worker/generation; cleanup verified")
        return 0

    print("STATUS: DEV-008 NOT ACCEPTED")
    return 54


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--startup-timeout", type=float, default=60.0)
    a = p.parse_args(argv)
    return run_trial(
        Path(a.base).expanduser().resolve(),
        Path(a.source).expanduser().resolve(),
        a.startup_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
