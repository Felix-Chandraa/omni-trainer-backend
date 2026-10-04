"""R2 DEV-006: guarded first live launch of OMNI through AircraftWorker.

This is an engineering startup trial, NOT a training Attempt and NOT acceptance.
The metadata Attempt intentionally remains Setup. No flight/payload command is sent.

Safety/ownership rules:
- never calls run_demo.sh;
- never pkill/killall or scans/kills unrelated PIDs;
- never wipes EEPROM or mutates model/source files;
- requires the legacy SITL TCP ports to be free before launch;
- takes exclusive raw FGNetFDM UDP :5503 before launch (visual must be closed);
- starts the explicit managed plan inside DEV-004's owned process group;
- stops only that owned process group;
- reports leftover ports instead of performing global cleanup.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from .fg_adapter import FGObserver
from .models import Actor, CoreError, Role
from .omni_launcher_adapter import (
    build_initial_plan, choose_jsbsim, source_from_root, validate_source,
)
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator


TCP_SITL_PORTS = (5760, 5762)
FG_PORT = 5503
MAV_RX_PORT = 14555


def tcp_port_free(host: str, port: int) -> bool:
    """Check bind availability without connecting to an existing SITL endpoint."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            return False
        raise
    finally:
        s.close()


def udp_port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((host, port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            return False
        raise
    finally:
        s.close()


def tail_text(path: Path, max_lines: int = 35, max_bytes: int = 64_000) -> str:
    if not path.is_file():
        return "<worker.log belum ada>"
    data = path.read_bytes()[-max_bytes:]
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def classify_log(text: str) -> list[str]:
    low = text.lower()
    flags = []
    for name, needles in (
        ("jsbsim_panic", ("panic", "jsbsim failed", "failed to start jsbsim")),
        ("port_conflict", ("address already in use", "bind failed", "port is already")),
        ("missing_model", ("omni-trainer", "aircraft not found")),
    ):
        if any(n in low for n in needles):
            flags.append(name)
    return flags


def require_clean_source(source, jsbsim: Path) -> None:
    checks = validate_source(source, jsbsim)
    fatal = [c for c in checks if not c.ok and c.severity == "error"]
    if fatal:
        raise CoreError("required source check failed: " + "; ".join(f"{c.name}: {c.detail}" for c in fatal))
    # DEV-005 treats a model mismatch as a warning because it is read-only.
    # DEV-006 is about to execute the model, so mismatch becomes a live-launch blocker.
    mismatch = next((c for c in checks if c.name == "model source == installed copy"), None)
    if mismatch is not None and not mismatch.ok:
        raise CoreError("model source/installed hash mismatch; refuse live launch instead of silently copying")


def probe_pymavlink_import(venv_python: Path) -> None:
    cp = subprocess.run([str(venv_python), "-c", "import pymavlink; print('pymavlink-ok')"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, timeout=10)
    if cp.returncode != 0:
        raise CoreError("OMNI venv cannot import pymavlink: " + cp.stdout.strip()[-400:])


def raw_mav_probe(venv_python: Path, probe_script: Path, timeout_s: float) -> dict:
    cp = subprocess.run([
        str(venv_python), str(probe_script),
        "--conn", f"udpin:127.0.0.1:{MAV_RX_PORT}",
        "--timeout", str(timeout_s),
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
       timeout=max(5.0, timeout_s + 3.0))
    lines = [x.strip() for x in cp.stdout.splitlines() if x.strip()]
    if not lines:
        return {"ok": False, "error": f"raw probe produced no output rc={cp.returncode}"}
    try:
        row = json.loads(lines[-1])
    except json.JSONDecodeError:
        row = {"ok": False, "error": "raw probe non-JSON output", "output": cp.stdout[-1000:]}
    row["returncode"] = cp.returncode
    return row


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_trial(*, base: Path, source_root: Path, startup_timeout: float, observe_seconds: float) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())
    require_clean_source(source, jsbsim)
    probe_pymavlink_import(source.venv_python)

    busy_tcp = [p for p in TCP_SITL_PORTS if not tcp_port_free("127.0.0.1", p)]
    if busy_tcp:
        print(f"STATUS: LIVE BLOCKED: TCP port already in use: {busy_tcp}")
        print("Tutup run_demo/SITL existing dahulu. DEV-006 tidak akan mematikannya.")
        return 20
    if not udp_port_free("127.0.0.1", FG_PORT):
        print("STATUS: LIVE BLOCKED: UDP 5503 sedang dipakai (biasanya visual/FG receiver).")
        print("Tutup Feather/visual dahulu; DEV-006 perlu raw FG port eksklusif untuk bukti langsung.")
        return 21
    if not udp_port_free("127.0.0.1", MAV_RX_PORT):
        print(f"STATUS: LIVE BLOCKED: UDP {MAV_RX_PORT} sedang dipakai.")
        print("Tutup GCS/consumer MAVLink lain dahulu; DEV-006 memakai output kedua sebagai receive-only evidence.")
        return 22

    plan = build_initial_plan(
        source,
        entry_script=base / "src/training_core/omni_managed_entry.py",
        jsbsim=jsbsim,
    )
    trial_root = base / "runtime" / "dev006" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    trial_root.mkdir(parents=True, exist_ok=False)
    db_path = trial_root / "metadata.sqlite"
    result_path = trial_root / "trial.json"
    store = Store(db_path)
    manager = SessionManager(store)
    instructor = Actor("instructor-dev006", Role.INSTRUCTOR)
    session_id = manager.create_session(instructor, "student-dev006")
    exercise_id = manager.create_exercise(instructor, session_id, "managed-live-startup@engineering")
    attempt = manager.create_attempt(instructor, exercise_id, "omni-1")
    coord = AttemptWorkerCoordinator(store, trial_root / "workers")
    worker_log = None
    stopped = None
    result = {
        "schema": "omni.r2.dev006-live-trial.v1",
        "training_claim": False,
        "attempt_state_claim": "Setup only",
        "source": str(source.root),
        "jsbsim": str(jsbsim),
        "attempt_id": attempt.id,
        "generation": attempt.generation,
        "aircraft_id": attempt.aircraft_id,
        "managed_entry": str(base / "src/training_core/omni_managed_entry.py"),
        "legacy_run_demo_used": False,
        "mission_loader_used": False,
        "commands_sent": False,
        "fg": {"ok": False},
        "mavlink": {"ok": False},
        "cleanup": {},
    }

    print("SOURCE:", source.root)
    print("JSBSim:", jsbsim)
    print("Attempt:", attempt.id, "generation=", attempt.generation, "state=Setup")
    print("Managed launch: run_demo.sh=NO, mission-loader=NO, commands=NO")
    print("Runtime:", trial_root)

    try:
        # Own raw FG before starting simulator; if any other consumer raced us,
        # __enter__ fails before a worker exists.
        with FGObserver(FG_PORT) as fg:
            health = coord.start(instructor, attempt.id, argv=plan.entry_argv, cwd=plan.cwd)
            worker_log = Path(health.runtime_dir) / "worker.log"
            result["worker"] = {"pid": health.pid, "runtime_dir": health.runtime_dir}
            print(f"worker started pid={health.pid}; menunggu raw FG + SITL startup...")

            deadline = time.monotonic() + startup_timeout
            first_pose = None
            while time.monotonic() < deadline:
                h = coord.health(instructor, attempt.id)
                if not h.healthy:
                    result["worker"]["early_exit"] = h.exit_code
                    print(f"STATUS: WORKER EXITED EARLY rc={h.exit_code}")
                    print("--- worker.log tail ---")
                    print(tail_text(worker_log))
                    result["log_flags"] = classify_log(tail_text(worker_log, 80))
                    write_json_atomic(result_path, result)
                    return 30
                pose = fg.drain()
                if pose is not None:
                    first_pose = pose
                    break
                time.sleep(0.05)

            if first_pose is None:
                print("STATUS: STARTUP INCOMPLETE: tidak menerima raw FGNetFDM v24 sebelum timeout")
                result["fg"] = {"ok": False, "rx_total": fg.rx_total, "bad_total": fg.bad_total}
                result["log_flags"] = classify_log(tail_text(worker_log, 80))
                return 31

            result["fg"] = {
                "ok": True,
                "rx_total_at_first": fg.rx_total,
                "bad_total": fg.bad_total,
                "lat_deg": first_pose.lat_deg,
                "lon_deg": first_pose.lon_deg,
                "alt_msl_m": first_pose.alt_msl_m,
                "agl_raw_m": first_pose.agl_raw_m,
                "yaw_deg": first_pose.yaw_deg,
            }
            print("RAW FG OK:",
                  f"lat={first_pose.lat_deg:.7f}", f"lon={first_pose.lon_deg:.7f}",
                  f"alt_msl={first_pose.alt_msl_m:.2f}m",
                  f"yaw={first_pose.yaw_deg:.2f}deg",
                  f"rx={fg.rx_total}", f"bad={fg.bad_total}")

            # Managed launch explicitly publishes MAVLink to UDP 14550/14555.
            # Bind the second stream and wait receive-only for the next HEARTBEAT.
            mav = raw_mav_probe(source.venv_python, base / "src/training_core/raw_mav_probe.py", 10.0)
            result["mavlink"] = mav
            if mav.get("ok"):
                print("RAW MAVLINK OK:",
                      f"heartbeat sys={mav.get('system_id')} comp={mav.get('component_id')}",
                      f"armed={mav.get('armed')} custom_mode={mav.get('custom_mode')}")
            else:
                print("RAW MAVLINK FAILED:", mav.get("error"))

            # Stability window while still draining FG; no command is dispatched.
            observe_deadline = time.monotonic() + observe_seconds
            last_pose = first_pose
            while time.monotonic() < observe_deadline:
                h = coord.health(instructor, attempt.id)
                if not h.healthy:
                    result["worker"]["exit_during_observation"] = h.exit_code
                    break
                p = fg.drain()
                if p is not None:
                    last_pose = p
                time.sleep(0.05)
            result["fg"].update({
                "rx_total_final": fg.rx_total,
                "bad_total_final": fg.bad_total,
                "last_lat_deg": last_pose.lat_deg,
                "last_lon_deg": last_pose.lon_deg,
                "last_alt_msl_m": last_pose.alt_msl_m,
            })
            print(f"stability window selesai: FG rx_total={fg.rx_total} bad={fg.bad_total}")

            if not result["mavlink"].get("ok"):
                return 32
            if result.get("worker", {}).get("exit_during_observation") is not None:
                print("STATUS: WORKER BECAME UNHEALTHY during observation")
                return 33

            result["runtime_evidence_pass"] = True
            print("STATUS: DEV-006 RUNTIME EVIDENCE VERIFIED; cleanup verification pending")
    finally:
        # Stop only the owned process group if it is still registered/alive.
        try:
            h = coord.health(instructor, attempt.id)
            if h.status == "running":
                stopped = coord.stop(instructor, attempt.id, timeout=5.0)
                print(f"owned worker stopped rc={stopped.exit_code}")
            elif h.status == "exited":
                stopped = coord.release_after_exit(instructor, attempt.id)
                print(f"owned worker reaped rc={stopped.exit_code}")
        except Exception as exc:
            result["cleanup"]["stop_error"] = f"{type(exc).__name__}: {exc}"
            print("CLEANUP WARNING:", result["cleanup"]["stop_error"])

        # Give owned children a moment to close listener ports. No global kill.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if all(tcp_port_free("127.0.0.1", p) for p in TCP_SITL_PORTS):
                break
            time.sleep(0.1)
        leftover = [p for p in TCP_SITL_PORTS if not tcp_port_free("127.0.0.1", p)]
        result["cleanup"].update({
            "tcp_ports_free_after_stop": not leftover,
            "leftover_tcp_ports": leftover,
            "global_kill_used": False,
        })
        if leftover:
            print("CLEANUP INCOMPLETE: port masih aktif", leftover)
            print("DEV-006 sengaja TIDAK melakukan pkill global. Jangan rerun; kirim output ini.")
        else:
            print("cleanup verified: TCP 5760/5762 kembali free")
        if worker_log is not None:
            result["log_flags"] = classify_log(tail_text(worker_log, 80))
            result["worker_log"] = str(worker_log)
        try:
            write_json_atomic(result_path, result)
            print("Trial result:", result_path)
            if worker_log is not None:
                print("Worker log:", worker_log)
        finally:
            store.close()

    # The success path reaches here only after the cleanup finally-block.
    cleanup_ok = bool(result.get("cleanup", {}).get("tcp_ports_free_after_stop"))
    runtime_ok = bool(result.get("runtime_evidence_pass"))
    result["live_pass"] = bool(runtime_ok and cleanup_ok)
    write_json_atomic(result_path, result)

    if runtime_ok and cleanup_ok:
        print("STATUS: DEV-006 ACCEPTED (engineering): runtime evidence + scoped cleanup verified")
        return 0
    if runtime_ok and not cleanup_ok:
        print("STATUS: DEV-006 NOT ACCEPTED: runtime evidence passed but cleanup incomplete")
        return 33
    return 34


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--startup-timeout", type=float, default=45.0)
    p.add_argument("--seconds", type=float, default=8.0)
    a = p.parse_args(argv)
    if not (5 <= a.startup_timeout <= 180):
        print("startup timeout harus 5..180 detik", file=sys.stderr)
        return 2
    if not (1 <= a.seconds <= 60):
        print("observation seconds harus 1..60", file=sys.stderr)
        return 2
    try:
        return run_trial(base=Path(a.base).resolve(), source_root=Path(a.source).resolve(),
                         startup_timeout=a.startup_timeout, observe_seconds=a.seconds)
    except (CoreError, OSError, subprocess.SubprocessError) as exc:
        print(f"STATUS: LIVE BLOCKED/FAILED SAFELY: {type(exc).__name__}: {exc}")
        return 40


if __name__ == "__main__":
    raise SystemExit(main())
