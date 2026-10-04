from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import signal
import threading
import time
from types import MethodType
from typing import Any
from urllib.parse import urlsplit

from . import instructor_console_rev6 as rev6


DEV018_REV9R2_AUTHORITATIVE_FLIGHT_PAUSE = True


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    ppid: int
    pgrp: int
    session: int
    starttime: int
    state: str
    comm: str
    cmdline: str

    def public(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgrp": self.pgrp,
            "session": self.session,
            "starttime": self.starttime,
            "state": self.state,
            "comm": self.comm,
            "cmdline": self.cmdline,
        }


def _proc_identity(pid: int) -> ProcIdentity | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
    except OSError:
        return None
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    tail = raw[rparen + 2 :].split()
    if len(tail) < 20:
        return None
    try:
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        cmdline = cmd.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        cmdline = ""
    try:
        return ProcIdentity(
            pid=int(pid),
            ppid=int(tail[1]),
            pgrp=int(tail[2]),
            session=int(tail[3]),
            starttime=int(tail[19]),
            state=str(tail[0]),
            comm=raw[raw.find("(") + 1 : rparen],
            cmdline=cmdline,
        )
    except (ValueError, IndexError):
        return None


def _same_process(identity: ProcIdentity) -> bool:
    now = _proc_identity(identity.pid)
    return now is not None and now.starttime == identity.starttime


def _all_processes() -> dict[int, ProcIdentity]:
    out: dict[int, ProcIdentity] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return out
    for item in entries:
        if not item.name.isdigit():
            continue
        ident = _proc_identity(int(item.name))
        if ident is not None:
            out[ident.pid] = ident
    return out


def _descendant_snapshot(root_pid: int) -> dict[int, ProcIdentity]:
    table = _all_processes()
    owned = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, ident in table.items():
            if pid in owned:
                continue
            if ident.ppid in owned:
                owned.add(pid)
                changed = True
    return {pid: table[pid] for pid in sorted(owned) if pid in table}


def _wait_stop_state(
    snapshot: dict[int, ProcIdentity],
    *,
    stopped: bool,
    timeout: float = 2.0,
) -> tuple[bool, dict[int, str]]:
    deadline = time.monotonic() + timeout
    last: dict[int, str] = {}
    while time.monotonic() < deadline:
        okay = True
        states: dict[int, str] = {}
        for pid, old in snapshot.items():
            now = _proc_identity(pid)
            if now is None or now.starttime != old.starttime:
                states[pid] = "gone_or_reused"
                okay = False
                continue
            states[pid] = now.state
            is_stopped = now.state in {"T", "t"}
            if is_stopped != stopped:
                okay = False
        last = states
        if okay:
            return True, states
        time.sleep(0.05)
    return False, last


def _signal_exact(identity: ProcIdentity, sig: int) -> None:
    if not _same_process(identity):
        raise RuntimeError(f"process identity changed: pid={identity.pid}")
    os.kill(identity.pid, sig)


def _signal_topology(
    root_pid: int,
    snapshot: dict[int, ProcIdentity],
    sig: int,
) -> None:
    root = snapshot.get(int(root_pid))
    if root is None or not _same_process(root):
        raise RuntimeError("owned worker root identity missing/changed")
    now_root = _proc_identity(root.pid)
    if now_root is None or now_root.pgrp != root.pid:
        raise RuntimeError(
            f"owned worker process-group contract changed: "
            f"pid={root.pid} pgrp={None if now_root is None else now_root.pgrp}"
        )

    os.killpg(root.pid, sig)

    for pid, ident in snapshot.items():
        if pid == root.pid:
            continue
        if ident.pgrp == root.pid:
            continue
        _signal_exact(ident, sig)


class InstructorRuntime(rev6.InstructorRuntime):
    """rev6 plus authoritative hold/resume for the current Flight stack."""

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9_pause_barrier = False
        self._rev9_hold_snapshot: dict[int, ProcIdentity] | None = None
        self._rev9_hold_root_pid: int | None = None
        self._rev9_hold_started_mono: float | None = None
        self._rev9_hold_total_s = 0.0
        self._rev9_active_accum_s = 0.0
        self._rev9_active_segment_mono: float | None = None

    def _install_command_pause_guard_locked(self) -> None:
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("control bridge/router missing")
        router = bridge.router
        if getattr(router, "_dev018_rev9r2_pause_guard", False):
            return

        runtime = self
        original = router._validate_envelope

        def guarded(this, principal, incoming, binding):
            if runtime._rev9_pause_barrier:
                raise ValueError("attempt_pause_barrier")
            return original(principal, incoming, binding)

        router._validate_envelope = MethodType(guarded, router)
        router._dev018_rev9r2_pause_guard = True
        self._audit(
            "pause_command_guard_installed",
            {"attempt_id": self.ctx.get("attempt_id") if self.ctx else None},
        )

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        super().prepare(timeout)
        with self.lock:
            self._install_command_pause_guard_locked()
            return self.status()

    def start(self) -> dict[str, Any]:
        super().start()
        with self.lock:
            current = self._attempt()
            if current is None or rev6._state_text(current.state).lower() != "active":
                raise RuntimeError("expected Active Attempt after Start")
            self._rev9_active_accum_s = 0.0
            self._rev9_active_segment_mono = time.monotonic()
            return self.status()

    def _snapshot_owned_topology_locked(self) -> tuple[int, dict[int, ProcIdentity]]:
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        attempt_id = self.ctx["attempt_id"]
        health = self.workers.health(self.instructor, attempt_id)
        if not health.healthy:
            raise RuntimeError(
                f"worker unhealthy before hold: {health.status}/{health.exit_code}"
            )
        root_pid = int(health.pid)
        snapshot = _descendant_snapshot(root_pid)
        root = snapshot.get(root_pid)
        if root is None:
            raise RuntimeError("owned worker root missing from /proc topology")
        if root.pgrp != root_pid:
            raise RuntimeError(
                f"worker process-group contract changed: pid={root_pid} pgrp={root.pgrp}"
            )

        out_of_group = [
            ident for pid, ident in snapshot.items()
            if pid != root_pid and ident.pgrp != root_pid
        ]
        jsbsim = [
            ident for ident in out_of_group
            if "jsbsim" in (ident.comm + " " + ident.cmdline).lower()
        ]
        if not jsbsim:
            raise RuntimeError(
                "no exact out-of-group JSBSim descendant found; "
                "refusing incomplete authoritative hold"
            )
        return root_pid, snapshot

    def _record_lifecycle_locked(self, state: str, revision: int) -> None:
        bridge = self._control_bridge()
        if bridge is None or bridge.recorder is None:
            return
        try:
            bridge.recorder.record_lifecycle(state, revision=revision)
        except Exception as exc:
            self._audit(
                "pause_lifecycle_record_error",
                {"state": state, "revision": revision, "error": str(exc)},
            )

    def pause(self) -> dict[str, Any]:
        with self.lock:
            current = self._attempt()
            if current is None:
                raise RuntimeError("no current Attempt")
            if rev6._state_text(current.state).lower() != "active":
                raise RuntimeError("Pause requires an Active Attempt")
            if self._rev9_hold_snapshot is not None:
                raise RuntimeError("Attempt already has an active hold")

            bridge = self._control_bridge()
            if bridge is None or bridge.router is None:
                raise RuntimeError("Flight control bridge missing")

            self._rev9_pause_barrier = True
            bridge.router.release_attempt(
                current.id,
                reason="instructor_pause_before_hold",
            )

            root_pid = None
            snapshot = None
            try:
                root_pid, snapshot = self._snapshot_owned_topology_locked()
                _signal_topology(root_pid, snapshot, signal.SIGSTOP)
                stopped, states = _wait_stop_state(
                    snapshot, stopped=True, timeout=2.0
                )
                if not stopped:
                    raise RuntimeError(
                        f"authoritative hold incomplete; process states={states}"
                    )

                paused = self.sessions.transition(
                    self.instructor,
                    current.id,
                    self.contract["State"].PAUSED,
                    expected_revision=current.revision,
                    reason="instructor_pause_authoritative_flight_hold",
                )

                now = time.monotonic()
                if self._rev9_active_segment_mono is not None:
                    self._rev9_active_accum_s += max(
                        0.0, now - self._rev9_active_segment_mono
                    )
                    self._rev9_active_segment_mono = None

                self._rev9_hold_root_pid = root_pid
                self._rev9_hold_snapshot = snapshot
                self._rev9_hold_started_mono = now
                self.ctx["active_attempt"] = None
                self.ctx["paused_attempt"] = paused
                self._record_lifecycle_locked("Paused", paused.revision)
                self._audit(
                    "attempt_paused_authoritative_flight_hold",
                    {
                        "attempt_id": current.id,
                        "revision": paused.revision,
                        "root_pid": root_pid,
                        "owned_processes": [
                            x.public() for x in snapshot.values()
                        ],
                    },
                )
                return self.status()

            except Exception:
                if root_pid is not None and snapshot:
                    try:
                        _signal_topology(root_pid, snapshot, signal.SIGCONT)
                        _wait_stop_state(snapshot, stopped=False, timeout=2.0)
                    finally:
                        self._rev9_pause_barrier = False
                else:
                    self._rev9_pause_barrier = False
                raise

    def _wait_fresh_mav_after_resume_locked(
        self,
        bridge,
        *,
        previous_heartbeat_ns: int | None,
        timeout: float = 5.0,
    ) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = bridge.endpoint.vehicle_status()
            hb = status.last_heartbeat_monotonic_ns
            if (
                status.peer_ready
                and hb is not None
                and (
                    previous_heartbeat_ns is None
                    or int(hb) > int(previous_heartbeat_ns)
                )
            ):
                return int(hb)
            time.sleep(0.05)
        raise RuntimeError("fresh MAV heartbeat did not recover after resume")

    def resume(self) -> dict[str, Any]:
        with self.lock:
            current = self._attempt()
            if current is None:
                raise RuntimeError("no current Attempt")
            if rev6._state_text(current.state).lower() != "paused":
                raise RuntimeError("Resume requires a Paused Attempt")
            if (
                self._rev9_hold_snapshot is None
                or self._rev9_hold_root_pid is None
            ):
                raise RuntimeError("Paused Attempt has no authoritative hold snapshot")

            station = self._student_station_status_locked()
            if not station.get("connected"):
                raise RuntimeError(
                    "student station heartbeat is stale; Resume blocked"
                )

            bridge = self._control_bridge()
            if bridge is None or bridge.router is None:
                raise RuntimeError("Flight control bridge missing")
            ctl = bridge.public_status()
            if not ctl.get("client_connected"):
                raise RuntimeError(
                    "Feather Flight control client is disconnected; Resume blocked"
                )
            if not ctl.get("authority"):
                raise RuntimeError(
                    "Student Flight authority assignment missing; Resume blocked"
                )

            old_hb = bridge.endpoint.vehicle_status().last_heartbeat_monotonic_ns
            root_pid = self._rev9_hold_root_pid
            snapshot = self._rev9_hold_snapshot

            _signal_topology(root_pid, snapshot, signal.SIGCONT)
            running, states = _wait_stop_state(
                snapshot, stopped=False, timeout=2.0
            )
            if not running:
                try:
                    _signal_topology(root_pid, snapshot, signal.SIGSTOP)
                    _wait_stop_state(snapshot, stopped=True, timeout=2.0)
                except Exception:
                    pass
                raise RuntimeError(
                    f"authoritative resume incomplete; process states={states}"
                )

            try:
                health = self.workers.health(self.instructor, current.id)
                if not health.healthy:
                    raise RuntimeError(
                        f"worker unhealthy after resume: "
                        f"{health.status}/{health.exit_code}"
                    )

                fresh_hb = self._wait_fresh_mav_after_resume_locked(
                    bridge,
                    previous_heartbeat_ns=old_hb,
                    timeout=5.0,
                )

                active = self.sessions.transition(
                    self.instructor,
                    current.id,
                    self.contract["State"].ACTIVE,
                    expected_revision=current.revision,
                    reason="instructor_resume_after_authoritative_flight_hold",
                )
                self.ctx["paused_attempt"] = None
                self.ctx["active_attempt"] = active
                if self._rev9_hold_started_mono is not None:
                    self._rev9_hold_total_s += max(
                        0.0, time.monotonic() - self._rev9_hold_started_mono
                    )
                self._rev9_hold_started_mono = None
                self._rev9_hold_snapshot = None
                self._rev9_hold_root_pid = None
                self._rev9_active_segment_mono = time.monotonic()
                self._record_lifecycle_locked("Active", active.revision)
                self._audit(
                    "attempt_resumed_authoritative_flight_hold",
                    {
                        "attempt_id": current.id,
                        "revision": active.revision,
                        "fresh_mav_heartbeat_monotonic_ns": fresh_hb,
                    },
                )
                self._rev9_pause_barrier = False
                return self.status()

            except Exception:
                try:
                    _signal_topology(root_pid, snapshot, signal.SIGSTOP)
                    _wait_stop_state(snapshot, stopped=True, timeout=2.0)
                except Exception as hold_exc:
                    self.last_error = (
                        "resume failed and re-hold failed: "
                        f"{type(hold_exc).__name__}: {hold_exc}"
                    )
                raise

    def _cleanup_runtime(self) -> None:
        if (
            self._rev9_hold_snapshot is not None
            and self._rev9_hold_root_pid is not None
        ):
            try:
                _signal_topology(
                    self._rev9_hold_root_pid,
                    self._rev9_hold_snapshot,
                    signal.SIGCONT,
                )
                _wait_stop_state(
                    self._rev9_hold_snapshot,
                    stopped=False,
                    timeout=2.0,
                )
            except Exception as exc:
                self._audit(
                    "pause_cleanup_resume_error",
                    {"error": str(exc)},
                )
            finally:
                self._rev9_hold_snapshot = None
                self._rev9_hold_root_pid = None
                self._rev9_hold_started_mono = None
        self._rev9_pause_barrier = True
        super()._cleanup_runtime()

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r2"
        current = out.get("current")
        if current is not None:
            state = str(current.get("state") or "").lower()
            active_elapsed = self._rev9_active_accum_s
            if state == "active" and self._rev9_active_segment_mono is not None:
                active_elapsed += max(
                    0.0,
                    time.monotonic() - self._rev9_active_segment_mono,
                )
            current["active_elapsed_s"] = active_elapsed
            current["flight_pause"] = {
                "authoritative_hold": self._rev9_hold_snapshot is not None,
                "command_barrier": self._rev9_pause_barrier,
                "hold_total_s": self._rev9_hold_total_s
                + (
                    max(
                        0.0,
                        time.monotonic() - self._rev9_hold_started_mono,
                    )
                    if self._rev9_hold_started_mono is not None
                    else 0.0
                ),
                "scope": "current_flight_stack",
                "full_product_pause_claimed": False,
            }
        out["limitations"] = [
            "rev9 rev2 implements authoritative hold/resume for the current Flight stack: worker, ArduPlane, JSBSim and MAVProxy.",
            "Flight commands are blocked by an explicit pause barrier and by Attempt state while Paused.",
            "The Student Flight authority assignment is retained across manual Pause; active RC override is released before hold.",
            "Resume requires fresh Student heartbeat, connected Feather control client, existing Flight authority assignment, preserved process identity and a fresh MAV heartbeat.",
            "This revision does NOT yet auto-transition station/control loss to Interrupted.",
            "Payload/resource/event-scheduler full-pause semantics are not claimed until those subsystems are integrated.",
            "Instructor takeover/handback and voice/webcam readiness remain unwired.",
        ]
        return out


class Handler(rev6.v1.Handler):
    """Adds localhost-only Pause/Resume endpoints; all other routes remain rev6."""

    def _rev9_localhost(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _rev9_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/api/session/pause", "/api/session/resume"}:
            return super().do_POST()

        if not self._rev9_localhost():
            return self._rev9_json(
                403,
                {"ok": False, "error": "instructor_api_loopback_only"},
            )

        try:
            if path == "/api/session/pause":
                result = self.runtime.pause()
            else:
                result = self.runtime.resume()
            return self._rev9_json(200, result)
        except Exception as exc:
            return self._rev9_json(
                409,
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = rev6.probe(base, source)
    out.update(
        {
            "rev9r2": True,
            "authoritative_flight_pause": True,
            "pause_process_identity": "pid+starttime",
            "jsbsim_out_of_group_handled": True,
            "full_product_pause_claimed": False,
            "auto_interrupted_fault_policy": False,
        }
    )
    out["ok"] = bool(out.get("ok"))
    return out


def serve(base: Path, source: Path, host: str, port: int, web_root: Path) -> int:
    runtime = InstructorRuntime(base, source)
    Handler.runtime = runtime
    Handler.web_root = web_root
    server = rev6.ThreadingHTTPServer((host, port), Handler)
    runtime.public_port = port
    stopped = threading.Event()

    def request_stop(signum=None, frame=None):
        if stopped.is_set():
            return
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(
        f"OMNI Instructor Console DEV-018 rev9 rev2: http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "PAUSE: barrier -> release override -> Flight topology HOLD -> Active->Paused",
        flush=True,
    )
    print(
        "RESUME: continuity -> exact topology CONT -> fresh MAV -> Paused->Active",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        runtime.shutdown()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--web-root")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.base).expanduser().resolve()
    source = Path(args.source).expanduser().resolve()

    if args.probe:
        result = probe(base, source)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 70

    web_root = (
        Path(args.web_root).expanduser().resolve()
        if args.web_root
        else base / "web" / "instructor_console_v1"
    )
    if not (web_root / "index.html").is_file():
        raise SystemExit(f"Instructor web asset missing: {web_root}")
    if not (web_root / "student.html").is_file():
        raise SystemExit(f"Student web asset missing: {web_root}")

    return serve(base, source, args.host, args.port, web_root)


if __name__ == "__main__":
    raise SystemExit(main())
