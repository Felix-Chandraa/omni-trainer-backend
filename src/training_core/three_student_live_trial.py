"""DEV-010 live proof: 1 instructor + 3 students / 3 concurrent workers.

This is an engineering concurrency/isolaton proof. It does not add flight
command routing. All three workers use the direct DEV-009 supervisor.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time
import uuid

from .active_start import AuthorizedActiveStart
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


def mem_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def process_rss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def resource_snapshot(worker_pids: dict[str, int]) -> dict:
    snap = {
        "loadavg": list(os.getloadavg()),
        "mem_available_mb": mem_available_mb(),
        "supervisor_rss_mb": {},
    }
    for label, pid in worker_pids.items():
        snap["supervisor_rss_mb"][label] = process_rss_mb(pid)
    return snap


def verify_streams_advance(records, fg_by_attempt, workers, instructor,
                           *, duration: float, labels: tuple[str, ...]) -> dict[str, int]:
    selected = [r for r in records if r["label"] in labels]
    before = {r["label"]: fg_by_attempt[r["attempt"].id].rx_total for r in selected}
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline:
        for r in selected:
            attempt = r["attempt"]
            health = workers.health(instructor, attempt.id)
            if not health.healthy:
                raise CoreError(
                    f"worker {r['label']} unhealthy during overlap rc={health.exit_code}"
                )
            if r.get("expected_state") is not None:
                if workers is None:
                    raise AssertionError("unreachable")
            fg_by_attempt[attempt.id].drain()
        time.sleep(0.03)

    delta = {
        r["label"]: fg_by_attempt[r["attempt"].id].rx_total - before[r["label"]]
        for r in selected
    }
    zero = [label for label, value in delta.items() if value <= 0]
    if zero:
        raise CoreError("telemetry did not advance for: " + ",".join(zero))
    return delta


def assert_active(store: Store, records, labels: tuple[str, ...]) -> None:
    for r in records:
        if r["label"] not in labels:
            continue
        state = store.attempt(r["attempt"].id).state
        if state != State.ACTIVE:
            raise CoreError(f"worker {r['label']} expected Active, got {state.value}")


def run_trial(base: Path, source_root: Path, startup_timeout: float) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())

    proven_log = find_latest_working_worker_log(base)
    template = extract_arduplane_template(proven_log)
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev010" / f"{stamp}-{uuid.uuid4().hex[:8]}"
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
    instructor = Actor("dev010-instructor", Role.INSTRUCTOR)

    records: list[dict] = []
    started: list[str] = []
    accepted = False
    result: dict = {
        "target": "1 instructor + 3 students",
        "source_worker_log": str(proven_log),
    }

    try:
        # Create three independent Sessions/Attempts and bind exact slots 1..3.
        for idx, label in zip((1, 2, 3), ("A", "B", "C")):
            sid = sessions.create_session(instructor, f"dev010-student-{idx}")
            eid = sessions.create_exercise(
                instructor, sid, "dev010-three-student-concurrency-v1"
            )
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
                "label": label,
                "student": idx,
                "session_id": sid,
                "exercise_id": eid,
                "attempt": attempt,
                "lease": lease,
            })

        print("DEV-010 three-worker port bundles:")
        for r in records:
            s = r["lease"].slot
            print(
                f"  {r['label']} student={r['student']} slot={s.slot}",
                f"attempt={r['attempt'].id[:8]} aircraft={r['attempt'].aircraft_id}",
                f"SITL={s.sitl_tcp}/{s.sitl_tcp_secondary}",
                f"FG={s.fg_udp}",
                f"MAV={s.mav_client_udp}/{s.mav_monitor_udp}",
            )
        print("All workers use direct DEV-009 supervisor; instance 0 remains unused.")

        with ExitStack() as stack:
            fg_by_attempt = {
                r["attempt"].id: stack.enter_context(FGObserver(r["lease"].slot.fg_udp))
                for r in records
            }

            # Start A, B, C sequentially while keeping prior workers alive.
            for r in records:
                attempt = r["attempt"]
                lease = r["lease"]

                # All previously started workers must still be healthy.
                for prev in records:
                    if prev["attempt"].id not in started:
                        continue
                    hprev = workers.health(instructor, prev["attempt"].id)
                    if not hprev.healthy:
                        raise CoreError(
                            f"worker {prev['label']} lost health before {r['label']} start"
                        )

                direct_runtime = runtime / "direct" / attempt.id
                h = workers.start(
                    instructor,
                    attempt.id,
                    argv=worker_argv(
                        base, source, jsbsim, template_json, lease, direct_runtime
                    ),
                    cwd=base,
                )
                started.append(attempt.id)
                r["health_start"] = h
                print(
                    f"worker {r['label']} started",
                    f"supervisor_pid={h.pid}",
                    f"instance={lease.slot.instance}",
                )

                ev = evidence(
                    source, base, fg_by_attempt[attempt.id],
                    workers, instructor, attempt, lease,
                    min(startup_timeout, 60.0),
                )
                ready, decision = readiness.mark_ready(
                    instructor,
                    attempt.id,
                    ev,
                    expected_revision=attempt.revision,
                )
                r["ready"] = ready
                print(
                    f"worker {r['label']} Ready",
                    f"pid={h.pid}",
                    f"evidence={decision.evidence_sha256[:12]}...",
                )

            # Fresh revalidation before each Active transition.
            for r in records:
                attempt = r["attempt"]
                lease = r["lease"]
                ev = evidence(
                    source, base, fg_by_attempt[attempt.id],
                    workers, instructor, attempt, lease, 15.0,
                )
                active, decision = active_gate.start_active(
                    instructor,
                    attempt.id,
                    ev,
                    expected_revision=r["ready"].revision,
                )
                r["active"] = active
                r["active_evidence"] = decision.evidence_sha256
                print(
                    f"worker {r['label']} Active",
                    f"pid={r['health_start'].pid}",
                    f"revision={active.revision}",
                )

            assert_active(store, records, ("A", "B", "C"))
            print(
                "THREE CONCURRENT ACTIVE:",
                " ".join(
                    f"{r['label']} pid={r['health_start'].pid} instance={r['lease'].slot.instance}"
                    for r in records
                ),
            )

            overlap = verify_streams_advance(
                records, fg_by_attempt, workers, instructor,
                duration=3.0, labels=("A", "B", "C"),
            )
            print(
                "THREE-WAY TELEMETRY:",
                " ".join(f"{k}_delta={v}" for k, v in overlap.items()),
            )

            worker_pids = {
                r["label"]: r["health_start"].pid for r in records
            }
            result["resource_snapshot_three_active"] = resource_snapshot(worker_pids)
            rs = result["resource_snapshot_three_active"]
            print(
                "RESOURCE SNAPSHOT:",
                f"load1={rs['loadavg'][0]:.2f}",
                f"mem_available_mb={rs['mem_available_mb']:.1f}"
                if rs["mem_available_mb"] is not None else "mem_available_mb=n/a",
                "supervisor_rss_mb=" + json.dumps(rs["supervisor_rss_mb"], sort_keys=True),
            )

            # Stop A. B and C must remain Active, healthy, and streaming.
            a = records[0]
            sessions.transition(
                instructor,
                a["attempt"].id,
                State.ENDED,
                expected_revision=a["active"].revision,
                reason="DEV-010 isolate A stop",
            )
            stop_a = workers.stop(instructor, a["attempt"].id, timeout=15.0)
            started.remove(a["attempt"].id)
            if not wait_tcp_cleanup(a["lease"]):
                raise CoreError("worker A TCP ports not released")
            slots.release(a["attempt"].id)

            assert_active(store, records, ("B", "C"))
            bc_delta = verify_streams_advance(
                records, fg_by_attempt, workers, instructor,
                duration=2.0, labels=("B", "C"),
            )
            print(
                "ISOLATION A->B+C VERIFIED:",
                f"A_stop_rc={stop_a.exit_code}",
                f"B_delta={bc_delta['B']}",
                f"C_delta={bc_delta['C']}",
            )

            # Stop B. C must remain Active, healthy, and streaming.
            b = records[1]
            sessions.transition(
                instructor,
                b["attempt"].id,
                State.ENDED,
                expected_revision=b["active"].revision,
                reason="DEV-010 isolate B stop",
            )
            stop_b = workers.stop(instructor, b["attempt"].id, timeout=15.0)
            started.remove(b["attempt"].id)
            if not wait_tcp_cleanup(b["lease"]):
                raise CoreError("worker B TCP ports not released")
            slots.release(b["attempt"].id)

            assert_active(store, records, ("C",))
            c_delta = verify_streams_advance(
                records, fg_by_attempt, workers, instructor,
                duration=2.0, labels=("C",),
            )
            print(
                "ISOLATION B->C VERIFIED:",
                f"B_stop_rc={stop_b.exit_code}",
                f"C_delta={c_delta['C']}",
            )

            # Stop final worker C.
            c = records[2]
            sessions.transition(
                instructor,
                c["attempt"].id,
                State.ENDED,
                expected_revision=c["active"].revision,
                reason="DEV-010 full 1+3 trial complete",
            )
            stop_c = workers.stop(instructor, c["attempt"].id, timeout=15.0)
            started.remove(c["attempt"].id)
            if not wait_tcp_cleanup(c["lease"]):
                raise CoreError("worker C TCP ports not released")
            slots.release(c["attempt"].id)

            # Lease cleanup can be verified while FGObserver contexts are
            # still open, but full UDP port reusability CANNOT. Each
            # FGObserver intentionally owns its slot's FG UDP socket until the
            # ExitStack closes below. Treating that test-owned socket as an
            # orphan runtime process caused DEV-010 rev2's false negative.
            remaining = slots.snapshot()
            if remaining:
                raise CoreError(
                    "runtime slot leases remain: "
                    + ",".join(x.attempt_id for x in remaining)
                )

            result.update({
                "runtime_sequence_pass": True,
                "overlap_fg_delta": overlap,
                "after_a_stop_fg_delta": bc_delta,
                "after_b_stop_fg_delta": c_delta,
                "stop_rc": {
                    "A": stop_a.exit_code,
                    "B": stop_b.exit_code,
                    "C": stop_c.exit_code,
                },
                "worker_pids": worker_pids,
                "slots": {
                    r["label"]: r["lease"].slot.slot for r in records
                },
            })

        # ExitStack has now closed all DEV-010-owned FGObserver sockets.
        # Only at this point is it valid to assert that the full slot bundles
        # are reusable by a future worker.
        for r in records:
            if not slot_ports_free(r["lease"]):
                raise CoreError(
                    f"slot {r['lease'].slot.slot} not completely reusable after observer cleanup"
                )

        result["all_slot_ports_reusable"] = True
        result["accepted"] = True
        accepted = True
        print("FINAL CLEANUP VERIFIED: all slot 1/2/3 port bundles reusable")

    except Exception as exc:
        print("STATUS: DEV-010 FAILED:", f"{type(exc).__name__}: {exc}")
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Fail-safe cleanup is still scoped to each AttemptWorker.
        for attempt_id in list(reversed(started)):
            try:
                workers.stop(instructor, attempt_id, timeout=15.0)
            except Exception as exc:
                print("cleanup stop error:", attempt_id, exc)

        for r in records:
            try:
                slots.release(r["attempt"].id)
            except Exception as exc:
                print("slot release error:", r["attempt"].id, exc)

        result["remaining_leases"] = [
            x.attempt_id for x in slots.snapshot()
        ]
        result_path = runtime / "result.json"
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True))
        print("Result:", result_path)
        store.close()

    if accepted and not result.get("remaining_leases"):
        print("STATUS: DEV-010 ACCEPTED (engineering)")
        print("Proof: 1 instructor + 3 Sessions/Students + 3 isolated live workers")
        print("Proof: A stop leaves B+C alive; B stop leaves C alive; all slots reusable")
        return 0

    print("STATUS: DEV-010 NOT ACCEPTED")
    return 90


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--startup-timeout", type=float, default=75.0)
    a = p.parse_args(argv)
    return run_trial(
        Path(a.base).expanduser().resolve(),
        Path(a.source).expanduser().resolve(),
        a.startup_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
