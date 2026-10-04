"""DEV-007 live engineering proof: owned worker evidence gates Setup -> Ready."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import uuid

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


def wait_for_fg(fg: FGObserver, workers: AttemptWorkerCoordinator, actor: Actor,
                attempt_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    latest = None
    seen_mono = None
    while time.monotonic() < deadline:
        health = workers.health(actor, attempt_id)
        if not health.healthy:
            raise CoreError(f"worker exited before readiness evidence: {health.exit_code}")
        pose = fg.drain()
        if pose is not None:
            latest = pose
            seen_mono = time.monotonic()
            return latest, seen_mono
        time.sleep(0.05)
    raise CoreError("raw FG readiness timeout")


def refresh_fg(fg: FGObserver, prior_pose, prior_seen):
    latest = fg.drain()
    if latest is None:
        return prior_pose, prior_seen
    return latest, time.monotonic()


def wait_cleanup(timeout: float = 8.0) -> tuple[bool, list[int]]:
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
        print("STATUS: DEV-007 BLOCKED: stale/busy SITL TCP ports", busy_tcp)
        return 41
    if not udp_port_free("127.0.0.1", FG_PORT):
        print("STATUS: DEV-007 BLOCKED: UDP 5503 busy")
        return 42
    if not udp_port_free("127.0.0.1", MAV_RX_PORT):
        print("STATUS: DEV-007 BLOCKED: UDP 14555 busy")
        return 43

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev007" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    runtime.mkdir(parents=True, exist_ok=False)

    store = Store(runtime / "training.sqlite")
    sessions = SessionManager(store)
    workers = AttemptWorkerCoordinator(store, runtime / "workers")
    readiness = TrustedReadinessCoordinator(store, sessions, workers)

    instructor = Actor("dev007-instructor", Role.INSTRUCTOR)
    session_id = sessions.create_session(instructor, "dev007-student")
    exercise_id = sessions.create_exercise(instructor, session_id, "dev007-readiness-v1")
    attempt = sessions.create_attempt(instructor, exercise_id, "omni-1")

    plan = build_initial_plan(
        source,
        entry_script=base / "src/training_core/omni_managed_entry.py",
        jsbsim=jsbsim,
    )

    print("Session:", session_id)
    print("Exercise:", exercise_id)
    print("Attempt:", attempt.id, "generation=", attempt.generation, "state=", attempt.state.value)
    print("Readiness contract: worker + raw FG + receive-only MAV heartbeat; NO flight commands")

    started = False
    accepted = False
    cleanup_ok = False
    result: dict = {
        "attempt_id": attempt.id,
        "generation": attempt.generation,
        "state_before": attempt.state.value,
    }

    try:
        with FGObserver(FG_PORT) as fg:
            health = workers.start(
                instructor,
                attempt.id,
                argv=plan.entry_argv,
                cwd=plan.cwd,
            )
            started = True
            print(f"worker started pid={health.pid}; collecting trusted readiness evidence...")

            pose, fg_seen = wait_for_fg(
                fg, workers, instructor, attempt.id, min(startup_timeout, 60.0)
            )
            print(
                "READINESS FG:",
                f"lat={pose.lat_deg:.7f}",
                f"lon={pose.lon_deg:.7f}",
                f"alt={pose.alt_msl_m:.2f}m",
                f"rx={fg.rx_total}",
                f"bad={fg.bad_total}",
            )

            mav = raw_mav_probe(
                source.venv_python,
                base / "src/training_core/raw_mav_probe.py",
                10.0,
            )
            if not mav.get("ok"):
                raise CoreError("readiness MAV failed: " + str(mav.get("error")))

            mav_seen = time.monotonic()
            pose, fg_seen = refresh_fg(fg, pose, fg_seen)
            health = workers.health(instructor, attempt.id)

            print(
                "READINESS MAV:",
                f"sys={mav.get('system_id')}",
                f"comp={mav.get('component_id')}",
                f"armed={mav.get('armed')}",
                f"custom_mode={mav.get('custom_mode')}",
            )

            evidence = ReadinessEvidence(
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
                mav_seen_monotonic=mav_seen,
                mav_system_id=int(mav["system_id"]),
                mav_component_id=int(mav["component_id"]),
                mav_armed=bool(mav["armed"]),
                mav_custom_mode=int(mav["custom_mode"]),
                created_monotonic=time.monotonic(),
            )

            ready, decision = readiness.mark_ready(
                instructor,
                attempt.id,
                evidence,
                expected_revision=attempt.revision,
            )
            accepted = True
            result.update({
                "readiness_accepted": True,
                "state_ready": ready.state.value,
                "ready_revision": ready.revision,
                "evidence_sha256": decision.evidence_sha256,
                "fg_age_ms": round(decision.fg_age_s * 1000, 3),
                "mav_age_ms": round(decision.mav_age_s * 1000, 3),
            })
            print(
                "READINESS ACCEPTED:",
                f"{attempt.state.value} -> {ready.state.value}",
                f"revision={ready.revision}",
                f"evidence={decision.evidence_sha256[:12]}...",
            )

            # DEV-007 does not auto-start training. Ready remains distinct from Active.
            if ready.state != State.READY:
                raise CoreError("trusted readiness did not produce Ready")
            print("ACTIVE NOT ENTERED: training start remains a separate instructor action")

            ended = sessions.transition(
                instructor,
                attempt.id,
                State.ENDED,
                expected_revision=ready.revision,
                reason="DEV-007 engineering trial complete",
            )
            result["state_after_trial"] = ended.state.value

    except Exception as exc:
        print("STATUS: DEV-007 READINESS FAILED:", f"{type(exc).__name__}: {exc}")
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
            print("cleanup incomplete: TCP ports", leftover)

        events = store.events(attempt.id)
        names = [e["event_type"] for e in events]
        result["events"] = names
        result["readiness_checked_event"] = "readiness.checked" in names
        result["readiness_accepted_event"] = "readiness.accepted" in names
        (runtime / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        store.close()

    required_events = result.get("readiness_checked_event") and result.get("readiness_accepted_event")
    if accepted and cleanup_ok and required_events:
        print("STATUS: DEV-007 ACCEPTED (engineering)")
        print("Proof: owned worker + fresh FG + fresh MAV -> Setup→Ready; cleanup verified")
        return 0

    print("STATUS: DEV-007 NOT ACCEPTED")
    return 44


def main(argv=None) -> int:
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
