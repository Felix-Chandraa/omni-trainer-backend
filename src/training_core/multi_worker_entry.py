"""Direct per-aircraft runtime supervisor for DEV-009.

This deliberately does NOT execute sim_vehicle.py. The legacy sim_vehicle
process manager uses global/name-based cleanup that is incompatible with
concurrent isolated workers.

AircraftWorker owns this supervisor. The supervisor owns:
- one direct ArduPlane SITL process;
- one MAVProxy process;
- exact descendant PIDs observed under ArduPlane (notably JSBSim).

Shutdown is PID-scoped. No pkill/killall/name-based cleanup is used.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from .multi_runtime import adapt_arduplane_command


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    starttime: int
    cmdline: str


def proc_identity(pid: int) -> ProcIdentity | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        rparen = stat.rfind(")")
        fields = stat[rparen + 2:].split()
        # fields[0] is state (field 3), fields[19] is starttime (field 22).
        starttime = int(fields[19])
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        return ProcIdentity(pid=pid, starttime=starttime, cmdline=cmdline)
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None


def same_process(identity: ProcIdentity) -> bool:
    current = proc_identity(identity.pid)
    return current is not None and current.starttime == identity.starttime


def descendants_of(root_pid: int) -> list[ProcIdentity]:
    ppid_map: dict[int, list[int]] = {}
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            stat = (item / "stat").read_text()
            rparen = stat.rfind(")")
            fields = stat[rparen + 2:].split()
            ppid = int(fields[1])
            ppid_map.setdefault(ppid, []).append(int(item.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue

    found: list[ProcIdentity] = []
    stack = list(ppid_map.get(root_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        ident = proc_identity(pid)
        if ident is not None:
            found.append(ident)
        stack.extend(ppid_map.get(pid, []))
    return found


def scoped_signal(identity: ProcIdentity, sig: int) -> None:
    if same_process(identity):
        try:
            os.kill(identity.pid, sig)
        except ProcessLookupError:
            pass


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


def wait_listener(port: int, timeout: float, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if not tcp_free(port):
            return True
        time.sleep(0.05)
    return False


def wait_proc(proc: subprocess.Popen | None, timeout: float) -> bool:
    if proc is None:
        return True
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--template-json", required=True)
    p.add_argument("--instance", type=int, required=True)
    p.add_argument("--runtime-dir", required=True)
    p.add_argument("--autotest-model-src", required=True)
    p.add_argument("--jsbsim", required=True)
    p.add_argument("--mavproxy", required=True)
    p.add_argument("--sitl-tcp", type=int, required=True)
    p.add_argument("--rcin-udp", type=int, required=True)
    p.add_argument("--mav-client-udp", type=int, required=True)
    p.add_argument("--mav-monitor-udp", type=int, required=True)
    a = p.parse_args(argv)

    if a.instance < 1:
        print("ERROR: instance 0 forbidden for concurrent managed runtime", flush=True)
        return 60

    runtime = Path(a.runtime_dir).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    plane_cwd = runtime / "plane"
    autotest = runtime / "autotest"
    aircraft_dst = autotest / "aircraft" / "Omni-Trainer"
    plane_cwd.mkdir(parents=True, exist_ok=True)
    aircraft_dst.parent.mkdir(parents=True, exist_ok=True)

    model_src = Path(a.autotest_model_src).resolve()
    if not model_src.is_dir():
        print(f"ERROR: model source missing: {model_src}", flush=True)
        return 61
    if aircraft_dst.exists():
        shutil.rmtree(aircraft_dst)
    shutil.copytree(model_src, aircraft_dst)

    # Preserve named-location lookup if the proven template carries a symbolic
    # --home value. The normal working template typically carries resolved
    # coordinates, but copying this read-only file makes the isolation robust.
    template_preview = tuple(json.loads(Path(a.template_json).read_text())["arduplane_argv"])
    ap_root = Path(template_preview[0]).resolve().parents[3]
    locations_src = ap_root / "Tools" / "autotest" / "locations.txt"
    if locations_src.is_file():
        shutil.copy2(locations_src, autotest / "locations.txt")

    jsbsim = Path(a.jsbsim).resolve()
    mavproxy = Path(a.mavproxy).resolve()
    if not jsbsim.is_file() or not os.access(jsbsim, os.X_OK):
        print(f"ERROR: JSBSim invalid: {jsbsim}", flush=True)
        return 62
    if not mavproxy.is_file() or not os.access(mavproxy, os.X_OK):
        print(f"ERROR: MAVProxy invalid: {mavproxy}", flush=True)
        return 63

    template = template_preview
    plane_argv = adapt_arduplane_command(
        template, instance=a.instance, autotest_dir=autotest
    )

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([
        str(jsbsim.parent),
        str(mavproxy.parent),
        env.get("PATH", ""),
    ])
    env.pop("DISPLAY", None)
    env["OMNI_MULTI_INSTANCE"] = str(a.instance)
    env["OMNI_MULTI_RUNTIME"] = str(runtime)

    tools_mavproxy = Path(template[0]).resolve().parents[3] / "Tools" / "mavproxy_modules"
    if tools_mavproxy.is_dir():
        env["PYTHONPATH"] = os.pathsep.join([
            str(tools_mavproxy),
            env.get("PYTHONPATH", ""),
        ])

    print("OMNI_MULTI direct ArduPlane:", " ".join(plane_argv), flush=True)
    print(
        "OMNI_MULTI ports:",
        f"instance={a.instance}",
        f"sitl={a.sitl_tcp}",
        f"rcin={a.rcin_udp}",
        f"mav_client={a.mav_client_udp}",
        f"mav_monitor={a.mav_monitor_udp}",
        flush=True,
    )

    stop_event = threading.Event()

    def stdin_watch():
        try:
            sys.stdin.buffer.read(1)
        finally:
            stop_event.set()

    threading.Thread(target=stdin_watch, daemon=True).start()

    def signal_stop(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, signal_stop)
    signal.signal(signal.SIGINT, signal_stop)

    plane = None
    mav = None
    owned_desc: dict[tuple[int, int], ProcIdentity] = {}
    exit_code = 0

    try:
        plane = subprocess.Popen(
            plane_argv,
            cwd=plane_cwd,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        plane_ident = proc_identity(plane.pid)
        print(f"OMNI_MULTI ArduPlane pid={plane.pid}", flush=True)

        if not wait_listener(a.sitl_tcp, 30.0, plane):
            print("ERROR: ArduPlane SITL listener did not become ready", flush=True)
            exit_code = 64
            return exit_code

        mav_argv = [
            str(mavproxy),
            "--retries", "10",
            "--master", f"tcp:127.0.0.1:{a.sitl_tcp}",
            "--sitl", f"127.0.0.1:{a.rcin_udp}",
            "--out", f"udp:127.0.0.1:{a.mav_client_udp}",
            "--out", f"udp:127.0.0.1:{a.mav_monitor_udp}",
        ]
        mav = subprocess.Popen(
            mav_argv,
            cwd=plane_cwd,
            env=env,
            stdin=subprocess.PIPE,
        )
        print(f"OMNI_MULTI MAVProxy pid={mav.pid}", flush=True)

        while not stop_event.is_set():
            if plane.poll() is not None:
                print(f"ERROR: ArduPlane exited rc={plane.returncode}", flush=True)
                exit_code = 65
                break
            if mav.poll() is not None:
                print(f"ERROR: MAVProxy exited rc={mav.returncode}", flush=True)
                exit_code = 66
                break

            for ident in descendants_of(plane.pid):
                owned_desc[(ident.pid, ident.starttime)] = ident
            manifest = {
                "schema": "omni.r2.direct-worker-children.v1",
                "instance": a.instance,
                "supervisor_pid": os.getpid(),
                "arduplane_pid": plane.pid,
                "mavproxy_pid": mav.pid,
                "descendants": [asdict(x) for x in owned_desc.values()],
            }
            tmp = runtime / ".children.tmp"
            tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
            os.replace(tmp, runtime / "children.json")
            time.sleep(0.20)

    finally:
        # Snapshot descendants before terminating ArduPlane. JSBSim calls
        # setsid(), so process-group cleanup alone is insufficient.
        if plane is not None and plane.poll() is None:
            for ident in descendants_of(plane.pid):
                owned_desc[(ident.pid, ident.starttime)] = ident

        if mav is not None and mav.poll() is None:
            if mav.stdin is not None and not mav.stdin.closed:
                try:
                    mav.stdin.close()
                except OSError:
                    pass
            if not wait_proc(mav, 2.0):
                mav.terminate()
                if not wait_proc(mav, 2.0):
                    mav.kill()
                    wait_proc(mav, 1.0)

        if plane is not None and plane.poll() is None:
            plane.terminate()
            if not wait_proc(plane, 2.0):
                plane.kill()
                wait_proc(plane, 1.0)

        # Kill only exact descendant identities observed under this ArduPlane.
        for ident in owned_desc.values():
            scoped_signal(ident, signal.SIGTERM)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if not any(same_process(x) for x in owned_desc.values()):
                break
            time.sleep(0.05)
        for ident in owned_desc.values():
            scoped_signal(ident, signal.SIGKILL)

        print(
            "OMNI_MULTI shutdown:",
            f"plane_rc={None if plane is None else plane.poll()}",
            f"mavproxy_rc={None if mav is None else mav.poll()}",
            f"owned_descendants={len(owned_desc)}",
            flush=True,
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

