from __future__ import annotations

import argparse
import ast
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any
from urllib.parse import urlparse, unquote

from . import instructor_console_v1 as v1
from .client_evidence_gateway import EvidenceLanTelemetryGateway
from .dev015_live_trial import install_control_endpoint, state_dict, visual_dict
from .evidence_extensions import attach_ardupilot_bins
from .evidence_recorder import AttemptEvidenceRecorder, AttemptIdentity
from .flight_command_router import FlightCommandRouter
from .lan_control_server import _dev017_install_actuator_observer


DEV018_REV6_SAME_ATTEMPT_FEATHER_CONTROL = True


def _state_text(value: Any) -> str:
    return v1._state_text(value)


def _discover_feather_map(feather_root: Path) -> str:
    cfg = feather_root / "config.py"
    if cfg.is_file():
        try:
            tree = ast.parse(cfg.read_text())
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id == "MAP_FILENAME"
                            and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str)
                        ):
                            value = node.value.value.strip()
                            if value and (feather_root / value).is_file():
                                return value
        except Exception:
            pass
    for candidate in ("map.html", "index.html"):
        if (feather_root / candidate).is_file():
            return candidate
    raise RuntimeError(f"Feather map entry not found under {feather_root}")


class ObservableFlightCommandRouter(FlightCommandRouter):
    """Existing DEV-015 router plus read-only connection observability."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client_lock = threading.RLock()
        self._client_count = 0
        self._last_client_connect_ns: int | None = None
        self._last_client_disconnect_ns: int | None = None

    async def on_connect(self, ws, principal) -> None:
        # A disconnect releases the old authority epoch in the existing router.
        # Reconnect therefore receives a fresh epoch; commands from the old
        # connection remain stale by construction.
        assignment = principal.assignment
        if self.authority.current(assignment.attempt_id) is None:
            self.grant_principal(principal, reason="DEV-018 rev6 student Flight client connect/reconnect")
        await super().on_connect(ws, principal)
        with self._client_lock:
            self._client_count += 1
            self._last_client_connect_ns = time.monotonic_ns()

    async def on_disconnect(self, principal) -> None:
        try:
            await super().on_disconnect(principal)
        finally:
            with self._client_lock:
                self._client_count = max(0, self._client_count - 1)
                self._last_client_disconnect_ns = time.monotonic_ns()

    def connection_snapshot(self) -> dict[str, Any]:
        with self._client_lock:
            return {
                "client_connected": self._client_count > 0,
                "client_count": self._client_count,
                "last_connect_monotonic_ns": self._last_client_connect_ns,
                "last_disconnect_monotonic_ns": self._last_client_disconnect_ns,
            }


class FeatherStaticHandler(SimpleHTTPRequestHandler):
    """LAN static host for Feather web assets, excluding source/secrets."""

    denied_suffixes = {
        ".py", ".pyc", ".pyo", ".sh", ".bash", ".zsh",
        ".sqlite", ".db", ".log", ".pem", ".key", ".env",
    }

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _allowed(self) -> bool:
        path = unquote(urlparse(self.path).path)
        parts = [p for p in Path(path).parts if p not in {"/", ""}]
        if any(p.startswith(".") for p in parts):
            return False
        suffix = Path(path).suffix.lower()
        if suffix in self.denied_suffixes:
            return False
        return True

    def do_GET(self) -> None:
        if not self._allowed():
            self.send_error(403)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if not self._allowed():
            self.send_error(403)
            return
        super().do_HEAD()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class FeatherStaticServer:
    def __init__(self, root: Path, port: int):
        self.root = Path(root)
        self.port = int(port)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        handler = partial(FeatherStaticHandler, directory=str(self.root))
        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name=f"OmniFeatherStatic:{self.port}",
        )
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None


class Dev018ControlBridge:
    def __init__(self, runtime: "InstructorRuntime", attempt, lease):
        self.runtime = runtime
        self.ctx = runtime.ctx
        assert self.ctx is not None
        self.attempt = attempt
        self.lease = lease
        slot_no = int(getattr(lease.slot, "slot", 1))
        self.control_port = int(os.environ.get("OMNI_CONTROL_PORT_BASE", "9120")) + slot_no - 1
        self.http_port = int(os.environ.get("OMNI_FEATHER_HTTP_PORT_BASE", "8016")) + slot_no - 1
        self.feather_root = runtime.source_root / "Feather-Flight-main"
        self.map_filename = _discover_feather_map(self.feather_root)

        self.recorder = None
        self.endpoint = None
        self.router: ObservableFlightCommandRouter | None = None
        self.gateway = None
        self.assignment = None
        self.principal = None
        self.authority_info: dict[str, Any] | None = None
        self.static_server: FeatherStaticServer | None = None

        self.fg = None
        self.latest_pose = None
        self._pose_lock = threading.RLock()
        self._pump_stop = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._finished = False
        self._gateway_started = False

    def setup(self) -> None:
        c = self.ctx
        run_dir = Path(c["run_dir"])
        identity = AttemptIdentity(
            c["session_id"],
            c["exercise_id"],
            self.attempt.id,
            self.attempt.aircraft_id,
            self.attempt.generation,
        )
        self.recorder = AttemptEvidenceRecorder(
            run_dir / "evidence",
            identity,
            configuration={
                "dev": "DEV-018-rev6",
                "proof": "same_attempt_instructor_student_feather_control",
                "deadman_ms": 350,
                "flight_commands_enabled": True,
                "student_station_id": c.get("student_station_id"),
                "scenario": c.get("scenario"),
            },
        )
        self.recorder.record_lifecycle("Setup", revision=self.attempt.revision)

        self.endpoint = install_control_endpoint(
            self.recorder,
            self.lease.slot.mav_client_udp,
        )
        _dev017_install_actuator_observer(self.endpoint)

        self.router = ObservableFlightCommandRouter(self.runtime.store, deadman_ms=350)
        self.gateway = EvidenceLanTelemetryGateway("0.0.0.0", self.control_port)
        self.gateway.set_command_router(self.router)

        self.assignment = self.gateway.register_assignment(
            student_id=c["student_id"],
            session_id=c["session_id"],
            attempt_id=self.attempt.id,
            aircraft_id=self.attempt.aircraft_id,
            generation=self.attempt.generation,
        )
        self.principal = self.gateway.register_evidence_principal(
            self.assignment,
            self.recorder,
            station_id=c.get("student_station_id") or "student-station-1",
            training_role="FLIGHT",
        )
        self.router.register_attempt(
            attempt_id=self.attempt.id,
            generation=self.attempt.generation,
            recorder=self.recorder,
            endpoint=self.endpoint,
        )
        self.authority_info = self.router.grant_principal(
            self.principal,
            reason="DEV-018 rev6 pre-Active student Flight assignment",
        )

        self.gateway.start()
        self._gateway_started = True

        self.static_server = FeatherStaticServer(self.feather_root, self.http_port)
        self.static_server.start()

        # Worker is already Ready when bridge.setup() is called, so the MAVLink
        # endpoint should learn the existing worker peer without spawning another
        # simulator or another Attempt.
        self.endpoint.wait_peer(timeout=5.0)

        ready = self.runtime.ctx.get("ready_attempt")
        if ready is not None:
            self.recorder.record_lifecycle("Ready", revision=ready.revision)

    def abort_setup(self, reason: str) -> None:
        try:
            self.quiesce()
        except Exception:
            pass
        try:
            if self.endpoint is not None:
                self.endpoint.stop()
        except Exception:
            pass
        try:
            if self.static_server is not None:
                self.static_server.stop()
        except Exception:
            pass
        try:
            if self.gateway is not None and self._gateway_started:
                self.gateway.stop()
        except Exception:
            pass
        try:
            if self.router is not None:
                self.router.shutdown()
        except Exception:
            pass
        try:
            if self.recorder is not None:
                self.recorder.abort(reason)
        except Exception:
            pass

    def activate(self, fg, revision: int) -> None:
        if self.fg is not None:
            return
        self.fg = fg
        if self.recorder is not None:
            self.recorder.record_lifecycle("Active", revision=revision)
        self._pump_stop.clear()
        self._pump_thread = threading.Thread(
            target=self._pump,
            daemon=True,
            name=f"OmniDev018Telemetry:{self.attempt.id[:8]}",
        )
        self._pump_thread.start()

    def _pump(self) -> None:
        while not self._pump_stop.is_set():
            try:
                pose = self.fg.drain() if self.fg is not None else None
                if pose is not None:
                    with self._pose_lock:
                        self.latest_pose = pose
                    if self.recorder is not None:
                        self.recorder.record_state(state_dict(pose), source="FGNetFDM")
                    if self.gateway is not None and self.assignment is not None:
                        actuators = (
                            self.endpoint.actuator_snapshot()
                            if self.endpoint is not None and hasattr(self.endpoint, "actuator_snapshot")
                            else {}
                        )
                        self.gateway.publish_telemetry(
                            self.assignment,
                            {**visual_dict(pose), **actuators},
                            quality={"pose_source": "fg", "dev": "DEV-018-rev6"},
                        )
            except Exception as exc:
                self.runtime.last_error = f"control telemetry pump: {type(exc).__name__}: {exc}"
                self.runtime._audit(
                    "control_telemetry_pump_error",
                    {"attempt_id": self.attempt.id, "error": str(exc)},
                )
                break
            time.sleep(1.0 / 30.0)

    def latest_pose_snapshot(self):
        with self._pose_lock:
            return self.latest_pose

    def public_status(self, *, include_token: bool = False) -> dict[str, Any]:
        peer_ready = False
        armed = None
        if self.endpoint is not None:
            try:
                vs = self.endpoint.vehicle_status()
                peer_ready = bool(vs.peer_ready)
                armed = vs.armed
            except Exception:
                pass

        conn = (
            self.router.connection_snapshot()
            if self.router is not None
            else {"client_connected": False, "client_count": 0}
        )
        authority = None
        if self.router is not None:
            try:
                lease = self.router.authority.current(self.attempt.id)
                authority = lease.public() if lease is not None else None
            except Exception:
                authority = None

        out = {
            "available": True,
            "gateway_started": self._gateway_started,
            "control_port": self.control_port,
            "feather_http_port": self.http_port,
            "feather_map_filename": self.map_filename,
            "student_id": self.ctx["student_id"],
            "generation": self.attempt.generation,
            "mav_peer_ready": peer_ready,
            "armed": armed,
            "authority": authority,
            **conn,
        }
        out["ready"] = bool(
            out["gateway_started"]
            and out["mav_peer_ready"]
            and out["client_connected"]
            and authority is not None
        )
        if include_token and self.assignment is not None:
            out["token"] = self.assignment.token
        return out

    def quiesce(self) -> None:
        self._pump_stop.set()
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2.0)
            self._pump_thread = None

        if self.router is not None:
            try:
                self.router.release_attempt(self.attempt.id, reason="DEV-018 rev6 lifecycle cleanup")
            except Exception:
                pass

        if self.endpoint is not None:
            try:
                self.endpoint.release_override()
            except Exception:
                pass
            try:
                if self.endpoint.vehicle_status().armed:
                    self.endpoint.send_arm_disarm(False)
                    self.endpoint.wait_armed(False, timeout=3.0)
            except Exception:
                pass

    def finish(self, final_state: str) -> None:
        if self._finished:
            return
        self._finished = True
        self.quiesce()

        if self.gateway is not None and self._gateway_started:
            try:
                self.gateway.stop()
            finally:
                self._gateway_started = False

        if self.router is not None:
            try:
                self.router.shutdown()
            except Exception:
                pass

        if self.static_server is not None:
            try:
                self.static_server.stop()
            except Exception:
                pass

        if self.fg is not None:
            try:
                self.fg.__exit__(None, None, None)
            except Exception:
                pass
            self.fg = None

        if self.endpoint is not None:
            try:
                self.endpoint.stop()
            except Exception as exc:
                self.runtime._audit(
                    "control_endpoint_stop_error",
                    {"attempt_id": self.attempt.id, "error": str(exc)},
                )

        if self.recorder is not None:
            try:
                current = self.runtime.store.attempt(self.attempt.id)
                self.recorder.record_lifecycle(
                    _state_text(current.state),
                    revision=current.revision,
                )
            except Exception:
                pass

            if final_state == "ended":
                try:
                    attach_ardupilot_bins(
                        self.recorder,
                        Path(self.ctx["run_dir"]),
                        self.attempt.id,
                    )
                except Exception as exc:
                    self.runtime._audit(
                        "control_bin_attach_error",
                        {"attempt_id": self.attempt.id, "error": str(exc)},
                    )
                try:
                    index = self.recorder.finalize(status="complete")
                    self.runtime._audit(
                        "control_evidence_finalized",
                        {"attempt_id": self.attempt.id, "index": str(index)},
                    )
                except Exception as exc:
                    self.runtime._audit(
                        "control_evidence_finalize_error",
                        {"attempt_id": self.attempt.id, "error": str(exc)},
                    )
            else:
                try:
                    self.recorder.abort(f"DEV-018 rev6 cleanup in state {final_state}")
                except Exception:
                    pass


class InstructorRuntime(v1.InstructorRuntime):
    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        with self.lock:
            station = self._student_station_status_locked()
            if not station.get("connected"):
                raise RuntimeError(
                    "open the Student Join Link first; a connected station is required before trusted readiness"
                )

        super().prepare(timeout)

        with self.lock:
            if self.ctx is None:
                raise RuntimeError("Attempt context disappeared during readiness")
            if self.ctx.get("control_bridge") is not None:
                return self.status()

            attempt = self._attempt()
            lease = self.ctx.get("lease")
            if attempt is None or lease is None:
                raise RuntimeError("Ready Attempt/slot missing for control bridge")

            bridge = Dev018ControlBridge(self, attempt, lease)
            try:
                bridge.setup()
            except Exception as exc:
                bridge.abort_setup(f"DEV-018 rev6 bridge setup failed: {exc}")
                self.last_error = f"control bridge setup failed: {type(exc).__name__}: {exc}"
                self._audit(
                    "control_bridge_setup_failed",
                    {"attempt_id": attempt.id, "error": str(exc)},
                )
                try:
                    current = self._attempt()
                    if current is not None and _state_text(current.state).lower() != "ended":
                        self.sessions.transition(
                            self.instructor,
                            current.id,
                            self.contract["State"].ENDED,
                            expected_revision=current.revision,
                            reason="control_bridge_setup_failed",
                        )
                except Exception:
                    pass
                super()._cleanup_runtime()
                raise RuntimeError(self.last_error) from exc

            self.ctx["control_bridge"] = bridge
            self._audit(
                "control_bridge_ready",
                {
                    "attempt_id": attempt.id,
                    "control_port": bridge.control_port,
                    "feather_http_port": bridge.http_port,
                    "student_station_id": self.ctx.get("student_station_id"),
                },
            )
            return self.status()

    def _control_bridge(self) -> Dev018ControlBridge | None:
        if not self.ctx:
            return None
        bridge = self.ctx.get("control_bridge")
        return bridge if isinstance(bridge, Dev018ControlBridge) else None

    def _student_control_payload(self) -> dict[str, Any]:
        bridge = self._control_bridge()
        if bridge is None:
            return {"available": False, "ready": False}
        return bridge.public_status(include_token=True)

    def student_context(self, attempt_id: str, token: str) -> dict[str, Any]:
        out = super().student_context(attempt_id, token)
        out["control"] = self._student_control_payload()
        return out

    def student_heartbeat(self, body: dict[str, Any], remote_ip: str) -> dict[str, Any]:
        out = super().student_heartbeat(body, remote_ip)
        out["control"] = self._student_control_payload()
        return out

    def start(self) -> dict[str, Any]:
        with self.lock:
            bridge = self._control_bridge()
            if bridge is None:
                raise RuntimeError("Feather/control bridge has not been prepared")
            ctl = bridge.public_status()
            if not ctl.get("mav_peer_ready"):
                raise RuntimeError("MAVLink control adapter is not ready")
            if not ctl.get("client_connected"):
                raise RuntimeError("Feather Flight control client is not connected")
            if not ctl.get("authority"):
                raise RuntimeError("student Flight authority is not assigned")

        super().start()

        with self.lock:
            bridge = self._control_bridge()
            if bridge is None or self.ctx is None:
                raise RuntimeError("control bridge disappeared during Start")
            fg = self.ctx.get("fg")
            active = self.ctx.get("active_attempt")
            if fg is None or active is None:
                raise RuntimeError("Active Attempt missing FG/control context")
            try:
                bridge.activate(fg, active.revision)
                # From Active onward the control bridge is the single FG consumer
                # and publishes the same pose to Feather + evidence. Instructor
                # status reads bridge.latest_pose instead of racing drain().
                self.ctx["fg"] = None
            except Exception as exc:
                self.last_error = f"control activation failed: {type(exc).__name__}: {exc}"
                current = self._attempt()
                if current is not None and _state_text(current.state).lower() == "active":
                    try:
                        self.sessions.transition(
                            self.instructor,
                            current.id,
                            self.contract["State"].INTERRUPTED,
                            expected_revision=current.revision,
                            reason="control_activation_failed",
                        )
                    except Exception:
                        pass
                self._cleanup_runtime()
                raise RuntimeError(self.last_error) from exc

            self._audit(
                "control_bridge_active",
                {"attempt_id": active.id, "revision": active.revision},
            )
            return self.status()

    def _cleanup_runtime(self) -> None:
        bridge = self._control_bridge()
        if bridge is not None:
            try:
                bridge.quiesce()
            except Exception as exc:
                self._audit(
                    "control_quiesce_error",
                    {"attempt_id": self.ctx.get("attempt_id") if self.ctx else None, "error": str(exc)},
                )

        # If Active transferred FG ownership to the bridge, v1 sees ctx["fg"] =
        # None. Worker/slot cleanup remains exactly the existing scoped lifecycle.
        super()._cleanup_runtime()

        if bridge is not None and self.ctx is not None:
            state = self._student_attempt_state_locked()
            try:
                bridge.finish(state)
            finally:
                self.ctx["control_bridge"] = None

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev6"
        out["limitations"] = [
            "Student Feather/Cesium and DEV-015 Flight command path are integrated into the SAME DEV-018 Attempt.",
            "Start requires connected student heartbeat, connected Feather control client, fresh MAVLink control peer, and student READY.",
            "If the Flight client disconnects, existing router/deadman releases Flight override/authority, but the Attempt does not yet auto-transition Active -> Interrupted.",
            "Full Pause/Resume is not exposed until authoritative full-pause semantics are implemented.",
            "Instructor takeover/handback is not exposed in this revision.",
            "Voice/webcam readiness is not yet authoritative in this revision.",
        ]
        current = out.get("current")
        if current is not None:
            bridge = self._control_bridge()
            current["control"] = (
                bridge.public_status(include_token=False)
                if bridge is not None
                else {"available": False, "ready": False}
            )
            if bridge is not None:
                pose = bridge.latest_pose_snapshot()
                if pose is not None:
                    current["pose"] = v1._pose_dict(pose)
        return out


def serve(base: Path, source: Path, host: str, port: int, web_root: Path) -> int:
    runtime = InstructorRuntime(base, source)
    v1.Handler.runtime = runtime
    v1.Handler.web_root = web_root
    server = ThreadingHTTPServer((host, port), v1.Handler)
    runtime.public_port = port

    stopped = threading.Event()

    def request_stop(signum=None, frame=None):
        if stopped.is_set():
            return
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(f"OMNI Instructor Console DEV-018 rev6: http://{host}:{port}/", flush=True)
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Student Feather/control is bound to the SAME Session/Exercise/Attempt; "
        "Ctrl+C follows existing Interrupted/scoped-cleanup policy.",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        runtime.shutdown()
    return 0


def probe(base: Path, source: Path) -> dict[str, Any]:
    # Import/signature existence is intentionally simple and fail-closed.
    feather = source / "Feather-Flight-main"
    result = {
        "rev6": True,
        "feather_root": str(feather),
        "map_filename": _discover_feather_map(feather),
        "v1_rev5": bool(getattr(v1, "DEV018_REV5_STUDENT_LIFECYCLE", False)),
        "gateway_methods": {
            name: hasattr(EvidenceLanTelemetryGateway, name)
            for name in (
                "set_command_router",
                "register_assignment",
                "register_evidence_principal",
                "start",
                "stop",
                "publish_telemetry",
            )
        },
        "router_methods": {
            name: hasattr(FlightCommandRouter, name)
            for name in ("register_attempt", "grant_principal", "release_attempt", "shutdown")
        },
    }
    result["ok"] = (
        result["v1_rev5"]
        and all(result["gateway_methods"].values())
        and all(result["router_methods"].values())
    )
    return result


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
        import json
        result = probe(base, source)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 70

    web_root = (
        Path(args.web_root).expanduser().resolve()
        if args.web_root
        else base / "web" / "instructor_console_v1"
    )
    if not (web_root / "index.html").is_file() or not (web_root / "student.html").is_file():
        raise SystemExit(f"Instructor/Student web assets missing: {web_root}")
    return serve(base, source, args.host, args.port, web_root)


if __name__ == "__main__":
    raise SystemExit(main())

