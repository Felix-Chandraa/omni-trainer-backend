"""DEV-009 live proof: two independent Sessions/Workers concurrently.

The live proof uses the direct worker supervisor, not sim_vehicle.py.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import subprocess
import time
import uuid

from .active_start import AuthorizedActiveStart
from .fg_adapter import FGObserver
from .models import Actor, CoreError, Role, State
from .multi_runtime import (
    RuntimeSlotAllocator,
    SlotLease,
    extract_arduplane_template,
    find_latest_working_worker_log,
    validate_template,
)
from .omni_launcher_adapter import choose_jsbsim, source_from_root
from .readiness import ReadinessEvidence, TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator


def tcp_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def udp_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def slot_ports_free(lease: SlotLease) -> bool:
    s = lease.slot
    return (
        tcp_free(s.sitl_tcp)
        and tcp_free(s.sitl_tcp_secondary)
        and udp_free(s.fg_udp)
        and udp_free(s.mav_monitor_udp)
    )


def wait_tcp_cleanup(lease: SlotLease, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tcp_free(lease.slot.sitl_tcp) and tcp_free(lease.slot.sitl_tcp_secondary):
            return True
        time.sleep(0.1)
    return tcp_free(lease.slot.sitl_tcp) and tcp_free(lease.slot.sitl_tcp_secondary)


def wait_fg(fg, workers, actor, attempt_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        h = workers.health(actor, attempt_id)
        if not h.healthy:
            raise CoreError(f"worker {attempt_id} unhealthy rc={h.exit_code}")
        pose = fg.drain()
        if pose is not None:
            return pose, time.monotonic()
        time.sleep(0.05)
    raise CoreError(f"FG timeout for {attempt_id}")


def raw_mav_probe_slot(venv_python: Path, script: Path, *, conn: str,
                       timeout: float = 10.0) -> dict:
    """Run the receive-only MAV probe against one worker's own UDP port."""
    cmd = [
        str(venv_python),
        str(script),
        "--conn", conn,
        "--timeout", str(timeout),
    ]
    try:
        cp = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout + 5.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "probe process timeout", "conn": conn}

    lines = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
    if not lines:
        return {
            "ok": False,
            "error": f"probe produced no output rc={cp.returncode}",
            "conn": conn,
        }

    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"probe invalid JSON rc={cp.returncode}: {lines[-1][:240]}",
            "conn": conn,
        }

    if cp.returncode != 0 and payload.get("ok"):
        payload = dict(payload)
        payload["ok"] = False
        payload["error"] = f"probe rc={cp.returncode} despite ok payload"
    return payload


def evidence(source, base, fg, workers, actor, attempt, lease, timeout):
    pose, fg_seen = wait_fg(fg, workers, actor, attempt.id, timeout)
    mav = raw_mav_probe_slot(
        source.venv_python,
        base / "src/training_core/raw_mav_probe.py",
        conn=f"udpin:127.0.0.1:{lease.slot.mav_monitor_udp}",
        timeout=10.0,
    )
    if not mav.get("ok"):
        raise CoreError(f"MAV timeout for {attempt.id}: {mav.get('error')}")
    latest = fg.drain()
    if latest is not None:
        pose = latest
        fg_seen = time.monotonic()
    h = workers.health(actor, attempt.id)
    now = time.monotonic()
    return ReadinessEvidence(
        attempt_id=attempt.id,
        aircraft_id=attempt.aircraft_id,
        generation=attempt.generation,
        worker_pid=h.pid,
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
    )


def worker_argv(base, source, jsbsim, template_json, lease, runtime_dir):
    mavproxy = source.root / ".venv/bin/mavproxy.py"
    model_src = source.root / "omnitrainer-sitl/assets/ardupilot/aircraft/Omni-Trainer"
    if not mavproxy.is_file():
        raise CoreError(f"MAVProxy executable missing: {mavproxy}")
    return (
        str(source.venv_python),
        "-m", "src.training_core.multi_worker_entry",
        "--template-json", str(template_json),
        "--instance", str(lease.slot.instance),
        "--runtime-dir", str(runtime_dir),
        "--autotest-model-src", str(model_src),
        "--jsbsim", str(jsbsim),
        "--mavproxy", str(mavproxy),
        "--sitl-tcp", str(lease.slot.sitl_tcp),
        "--rcin-udp", str(lease.slot.rcin_udp),
        "--mav-client-udp", str(lease.slot.mav_client_udp),
        "--mav-monitor-udp", str(lease.slot.mav_monitor_udp),
    )


def run_trial(base: Path, source_root: Path, startup_timeout: float) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())

    proven_log = find_latest_working_worker_log(base)
    template = extract_arduplane_template(proven_log)
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev009" / f"{stamp}-{uuid.uuid4().hex[:8]}"
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
    instructor = Actor("dev009-instructor", Role.INSTRUCTOR)

    records = []
    started = []
    leases = []
    accepted = False
    result = {"source_worker_log": str(proven_log)}

    try:
        for idx in (1, 2):
            sid = sessions.create_session(instructor, f"dev009-student-{idx}")
            eid = sessions.create_exercise(instructor, sid, "dev009-concurrency-v1")
            attempt = sessions.create_attempt(instructor, eid, f"omni-{idx}")
            lease = slots.claim(
                attempt_id=attempt.id,
                session_id=sid,
                aircraft_id=attempt.aircraft_id,
                preferred_slot=idx,
            )
            leases.append(lease)
            if not slot_ports_free(lease):
                raise CoreError(f"slot {idx} ports are not free before launch")
            records.append((sid, eid, attempt, lease))

        print("DEV-009 port bundles:")
        for sid, eid, attempt, lease in records:
            s = lease.slot
            print(
                f"  slot={s.slot} attempt={attempt.id[:8]} aircraft={attempt.aircraft_id}",
                f"SITL={s.sitl_tcp}/{s.sitl_tcp_secondary}",
                f"FG={s.fg_udp}",
                f"MAV={s.mav_client_udp}/{s.mav_monitor_udp}",
            )
        print("instance 0 is NOT used; sim_vehicle.py is NOT used for concurrent runtime")

        with ExitStack() as stack:
            fg_by_attempt = {}
            for _, _, attempt, lease in records:
                fg_by_attempt[attempt.id] = stack.enter_context(FGObserver(lease.slot.fg_udp))

            # Start worker A and prove direct runtime before starting B.
            sid1, eid1, a1, l1 = records[0]
            w1_runtime = runtime / "direct" / a1.id
            h1 = workers.start(
                instructor,
                a1.id,
                argv=worker_argv(base, source, jsbsim, template_json, l1, w1_runtime),
                cwd=base,
            )
            started.append(a1.id)
            print(f"worker A started supervisor_pid={h1.pid} instance={l1.slot.instance}")
            ev1 = evidence(
                source, base, fg_by_attempt[a1.id], workers, instructor, a1, l1,
                min(startup_timeout, 60.0),
            )
            r1, _ = readiness.mark_ready(
                instructor, a1.id, ev1, expected_revision=a1.revision
            )
            print(f"worker A Ready pid={h1.pid}")

            # Keep A running while B starts.
            sid2, eid2, a2, l2 = records[1]
            if not workers.health(instructor, a1.id).healthy:
                raise CoreError("worker A lost health before worker B start")
            w2_runtime = runtime / "direct" / a2.id
            h2 = workers.start(
                instructor,
                a2.id,
                argv=worker_argv(base, source, jsbsim, template_json, l2, w2_runtime),
                cwd=base,
            )
            started.append(a2.id)
            print(f"worker B started supervisor_pid={h2.pid} instance={l2.slot.instance}")
            ev2 = evidence(
                source, base, fg_by_attempt[a2.id], workers, instructor, a2, l2,
                min(startup_timeout, 60.0),
            )
            r2, _ = readiness.mark_ready(
                instructor, a2.id, ev2, expected_revision=a2.revision
            )
            print(f"worker B Ready pid={h2.pid}")

            # Fresh evidence for Active on both; do not reuse Ready evidence.
            ev1a = evidence(
                source, base, fg_by_attempt[a1.id], workers, instructor, a1, l1, 15.0
            )
            ac1, _ = active_gate.start_active(
                instructor, a1.id, ev1a, expected_revision=r1.revision
            )
            ev2a = evidence(
                source, base, fg_by_attempt[a2.id], workers, instructor, a2, l2, 15.0
            )
            ac2, _ = active_gate.start_active(
                instructor, a2.id, ev2a, expected_revision=r2.revision
            )

            if store.attempt(a1.id).state != State.ACTIVE or store.attempt(a2.id).state != State.ACTIVE:
                raise CoreError("both Attempts are not Active concurrently")
            print(
                "CONCURRENT ACTIVE:",
                f"A pid={h1.pid} instance={l1.slot.instance}",
                f"B pid={h2.pid} instance={l2.slot.instance}",
            )

            # Prove both FG streams advance while both workers are alive.
            fg1 = fg_by_attempt[a1.id]
            fg2 = fg_by_attempt[a2.id]
            before1, before2 = fg1.rx_total, fg2.rx_total
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not workers.health(instructor, a1.id).healthy:
                    raise CoreError("worker A unhealthy during overlap")
                if not workers.health(instructor, a2.id).healthy:
                    raise CoreError("worker B unhealthy during overlap")
                fg1.drain()
                fg2.drain()
                time.sleep(0.03)
            delta1 = fg1.rx_total - before1
            delta2 = fg2.rx_total - before2
            if delta1 <= 0 or delta2 <= 0:
                raise CoreError("one FG stream did not advance during overlap")
            print(f"OVERLAP TELEMETRY: A_delta={delta1} B_delta={delta2}")

            # End/stop A first; B must remain Active and healthy.
            end1 = sessions.transition(
                instructor, a1.id, State.ENDED,
                expected_revision=ac1.revision,
                reason="DEV-009 isolate worker A stop",
            )
            stop1 = workers.stop(instructor, a1.id, timeout=15.0)
            started.remove(a1.id)
            if not wait_tcp_cleanup(l1):
                raise CoreError("worker A TCP ports not released")
            slots.release(a1.id)

            b_before = fg2.rx_total
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                hb = workers.health(instructor, a2.id)
                if not hb.healthy:
                    raise CoreError("stopping worker A affected worker B")
                if store.attempt(a2.id).state != State.ACTIVE:
                    raise CoreError("worker B state changed when A stopped")
                fg2.drain()
                time.sleep(0.03)
            b_delta_after_a_stop = fg2.rx_total - b_before
            if b_delta_after_a_stop <= 0:
                raise CoreError("worker B telemetry stopped after worker A cleanup")
            print(
                "ISOLATION VERIFIED:",
                f"A stop_rc={stop1.exit_code}",
                f"B still Active pid={h2.pid}",
                f"B_FG_delta_after_A_stop={b_delta_after_a_stop}",
            )

            end2 = sessions.transition(
                instructor, a2.id, State.ENDED,
                expected_revision=ac2.revision,
                reason="DEV-009 trial complete",
            )
            stop2 = workers.stop(instructor, a2.id, timeout=15.0)
            started.remove(a2.id)
            if not wait_tcp_cleanup(l2):
                raise CoreError("worker B TCP ports not released")
            slots.release(a2.id)

            accepted = True
            result.update({
                "accepted": True,
                "worker_a_pid": h1.pid,
                "worker_b_pid": h2.pid,
                "slot_a": l1.slot.slot,
                "slot_b": l2.slot.slot,
                "fg_overlap_delta_a": delta1,
                "fg_overlap_delta_b": delta2,
                "fg_b_after_a_stop": b_delta_after_a_stop,
                "stop_a_rc": stop1.exit_code,
                "stop_b_rc": stop2.exit_code,
            })

    except Exception as exc:
        print("STATUS: DEV-009 FAILED:", f"{type(exc).__name__}: {exc}")
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for attempt_id in list(reversed(started)):
            try:
                workers.stop(instructor, attempt_id, timeout=15.0)
            except Exception as exc:
                print("cleanup stop error:", attempt_id, exc)
        for lease in leases:
            slots.release(lease.attempt_id)
        result["remaining_leases"] = [
            x.attempt_id for x in slots.snapshot()
        ]
        (runtime / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        store.close()

    if accepted and not result.get("remaining_leases"):
        print("STATUS: DEV-009 ACCEPTED (engineering)")
        print("Proof: 2 Sessions + 2 direct workers Active concurrently; stopping A does not disturb B")
        print("Architecture pool supports slots 1..3 for target 1 instructor + 3 students")
        return 0

    print("STATUS: DEV-009 NOT ACCEPTED")
    return 70


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

