from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import hmac
import ipaddress
import secrets
import socket
from urllib.parse import parse_qs, urlparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import threading
import time
import traceback
from typing import Any


DEV018_REV4_STUDENT_HANDSHAKE = True
DEV018_REV5_STUDENT_LIFECYCLE = True
STUDENT_HEARTBEAT_FRESH_SEC = 5.0

TARGET_SYMBOLS = (
    "Store",
    "SessionManager",
    "AttemptWorkerCoordinator",
    "TrustedReadinessCoordinator",
    "AuthorizedActiveStart",
    "RuntimeSlotAllocator",
    "Actor",
    "Role",
    "State",
    "FGObserver",
)
HELPER_SYMBOLS = (
    "source_from_root",
    "choose_jsbsim",
    "find_latest_working_worker_log",
    "extract_arduplane_template",
    "validate_template",
    "worker_argv",
    "evidence",
)


def _module_name(base: Path, py: Path) -> str:
    rel = py.relative_to(base.parent.parent)
    return ".".join(rel.with_suffix("").parts)


def _defs_in_file(py: Path) -> set[str]:
    try:
        tree = ast.parse(py.read_text())
    except Exception:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
    return out


def _import_module(name: str):
    return importlib.import_module(name)


def resolve_contract(base: Path) -> dict[str, Any]:
    core_dir = base / "src" / "training_core"
    if not core_dir.is_dir():
        raise RuntimeError(f"training_core missing: {core_dir}")

    files = sorted(core_dir.glob("*.py"))
    defs: dict[str, list[str]] = {}
    for py in files:
        mod = _module_name(core_dir, py)
        for name in _defs_in_file(py):
            defs.setdefault(name, []).append(mod)

    preferred = [
        "src.training_core.dev015_live_trial",
        "src.training_core.dev009_live_trial",
        "src.training_core.dev008_live_trial",
        "src.training_core.dev007_live_trial",
    ]
    contract: dict[str, Any] = {}

    for sym in TARGET_SYMBOLS:
        candidates = list(defs.get(sym, []))
        for p in preferred:
            if p not in candidates:
                candidates.append(p)
        found = None
        for mod_name in candidates:
            try:
                mod = _import_module(mod_name)
            except Exception:
                continue
            if hasattr(mod, sym):
                found = getattr(mod, sym)
                break
        if found is None:
            raise RuntimeError(f"required core symbol not found: {sym}")
        contract[sym] = found

    for sym in HELPER_SYMBOLS:
        candidates = preferred + list(defs.get(sym, []))
        found = None
        seen: set[str] = set()
        for mod_name in candidates:
            if mod_name in seen:
                continue
            seen.add(mod_name)
            try:
                mod = _import_module(mod_name)
            except Exception:
                continue
            if hasattr(mod, sym):
                found = getattr(mod, sym)
                break
        if found is None:
            raise RuntimeError(f"required live helper not found: {sym}")
        contract[sym] = found

    return contract


def _sig(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except Exception:
        return "<unknown>"


def probe_contract(base: Path) -> dict[str, Any]:
    c = resolve_contract(base)
    sm = c["SessionManager"]
    rw = c["AttemptWorkerCoordinator"]
    rr = c["TrustedReadinessCoordinator"]
    ag = c["AuthorizedActiveStart"]
    rs = c["RuntimeSlotAllocator"]

    required_methods = {
        "SessionManager": (sm, ["create_session", "create_exercise", "create_attempt", "transition"]),
        "AttemptWorkerCoordinator": (rw, ["start", "stop", "health"]),
        "TrustedReadinessCoordinator": (rr, ["mark_ready"]),
        "AuthorizedActiveStart": (ag, ["start_active"]),
        "RuntimeSlotAllocator": (rs, ["claim", "release"]),
    }
    for cls_name, (cls, names) in required_methods.items():
        for name in names:
            if not hasattr(cls, name):
                raise RuntimeError(f"{cls_name}.{name} missing")

    report: dict[str, Any] = {
        "symbols": {name: f"{c[name].__module__}.{getattr(c[name], '__name__', name)}" for name in TARGET_SYMBOLS + HELPER_SYMBOLS},
        "signatures": {
            "SessionManager.create_session": _sig(sm.create_session),
            "SessionManager.create_exercise": _sig(sm.create_exercise),
            "SessionManager.create_attempt": _sig(sm.create_attempt),
            "SessionManager.transition": _sig(sm.transition),
            "AttemptWorkerCoordinator.start": _sig(rw.start),
            "TrustedReadinessCoordinator.mark_ready": _sig(rr.mark_ready),
            "AuthorizedActiveStart.start_active": _sig(ag.start_active),
            "RuntimeSlotAllocator.claim": _sig(rs.claim),
            "evidence": _sig(c["evidence"]),
            "worker_argv": _sig(c["worker_argv"]),
        },
    }
    return report


def _state_text(state: Any) -> str:
    if state is None:
        return "UNKNOWN"
    v = getattr(state, "value", None)
    if isinstance(v, str):
        return v
    n = getattr(state, "name", None)
    if isinstance(n, str):
        return n
    return str(state)


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _pose_dict(pose: Any) -> dict[str, Any]:
    if pose is None:
        return {}
    aliases = {
        "lat": ("lat_deg", "lat"),
        "lon": ("lon_deg", "lon"),
        "alt_msl": ("alt_m", "alt_msl", "alt"),
        "roll": ("roll_deg", "roll"),
        "pitch": ("pitch_deg", "pitch"),
        "yaw": ("yaw_deg", "yaw", "heading_deg"),
        "agl": ("agl_m", "agl"),
        "groundspeed": ("groundspeed_mps", "groundspeed", "gs_mps"),
    }
    out: dict[str, Any] = {}
    for key, names in aliases.items():
        for name in names:
            if hasattr(pose, name):
                value = getattr(pose, name)
                if isinstance(value, (int, float)):
                    out[key] = float(value)
                else:
                    out[key] = _jsonable(value)
                break
    return out


class InstructorRuntime:
    def __init__(self, base: Path, source_root: Path):
        self.base = base
        self.source_root = source_root
        self.contract = resolve_contract(base)
        c = self.contract

        self.runtime_root = base / "runtime" / "dev018-instructor"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.runtime_root / "training.sqlite"
        self.audit_path = self.runtime_root / "ui_audit.jsonl"

        self.store = c["Store"](self.db_path)
        self.sessions = c["SessionManager"](self.store)
        self.workers = c["AttemptWorkerCoordinator"](self.store, self.runtime_root / "workers")
        self.readiness = c["TrustedReadinessCoordinator"](self.store, self.sessions, self.workers)
        self.active_gate = c["AuthorizedActiveStart"](self.store, self.sessions, self.workers)
        self.slots = c["RuntimeSlotAllocator"](self.runtime_root / "slot-leases", max_slots=3)
        self.instructor = c["Actor"]("dev018-instructor", c["Role"].INSTRUCTOR)

        self.source = c["source_from_root"](source_root)
        self.jsbsim = c["choose_jsbsim"](self.source, home=Path.home())
        self.proven_log = c["find_latest_working_worker_log"](base)
        self.template = c["extract_arduplane_template"](self.proven_log)
        c["validate_template"](self.template)

        self.lock = threading.RLock()
        self.ctx: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.started_mono: float | None = None
        self.public_port: int | None = None

    def _audit(self, action: str, data: dict[str, Any]) -> None:
        rec = {"utc_ns": time.time_ns(), "mono_ns": time.monotonic_ns(), "action": action, "data": _jsonable(data)}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def _attempt(self):
        if not self.ctx:
            return None
        return self.store.attempt(self.ctx["attempt_id"])

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.ctx is not None:
                state = _state_text(self._attempt().state)
                if state not in {"Ended", "ENDED", "ended"}:
                    raise RuntimeError("an instructor-console Attempt is already open")
                self.ctx = None

            student_id = str(body.get("student_id") or "").strip()
            student_name = str(body.get("student_name") or student_id).strip()
            scenario = str(body.get("scenario") or "").strip()
            aircraft_id = str(body.get("aircraft_id") or "omni-1").strip()
            if not student_id or not scenario:
                raise ValueError("student_id and scenario are required")

            sid = self.sessions.create_session(self.instructor, student_id)
            eid = self.sessions.create_exercise(self.instructor, sid, scenario)
            attempt = self.sessions.create_attempt(self.instructor, eid, aircraft_id)

            stamp = time.strftime("%Y%m%d-%H%M%S")
            run_dir = self.runtime_root / "sessions" / f"{stamp}-{attempt.id}"
            run_dir.mkdir(parents=True, exist_ok=False)
            template_json = run_dir / "arduplane-template.json"
            template_json.write_text(json.dumps({
                "source_worker_log": str(self.proven_log),
                "arduplane_argv": list(self.template),
            }, indent=2, sort_keys=True))

            meta = {
                "session_id": sid,
                "exercise_id": eid,
                "attempt_id": attempt.id,
                "student_id": student_id,
                "student_name": student_name,
                "scenario": scenario,
                "aircraft_id": aircraft_id,
                "training_mode": body.get("training_mode", "Practice"),
                "level": body.get("level", "Basic"),
                "role": body.get("role", "Solo"),
                "environment": body.get("environment", "Wiriadinata / ENV-WIR-03"),
                "runway": body.get("runway", "15 / 33"),
                "wind_speed_kt": body.get("wind_speed_kt", 4),
                "wind_direction_deg": body.get("wind_direction_deg", 150),
                "engineering_student_ready": False,
                "run_dir": str(run_dir),
            }
            (run_dir / "configuration.json").write_text(json.dumps(meta, indent=2, sort_keys=True))

            join_token = secrets.token_urlsafe(24)
            self.ctx = {
                **meta,
                "template_json": template_json,
                "attempt_initial": attempt,
                "lease": None,
                "fg": None,
                "worker_started": False,
                "ready_attempt": None,
                "student_join_token": join_token,
                "student_station_id": None,
                "student_station_ready": False,
                "student_last_heartbeat_mono_ns": None,
                "student_last_heartbeat_utc_ns": None,
                "student_remote_ip": None,
            }
            self.last_error = None
            self._audit("session_created", meta)
            return self.status()

    def _student_attempt_state_locked(self) -> str:
        attempt = self._attempt()
        return _state_text(getattr(attempt, "state", None)).lower() if attempt is not None else "unknown"

    def _student_station_status_locked(self) -> dict[str, Any]:
        if not self.ctx:
            return {"connected": False, "ready": False, "station_id": None, "last_seen_age_s": None}
        state = self._student_attempt_state_locked()
        if state == "ended":
            return {
                "connected": False,
                "ready": False,
                "station_id": self.ctx.get("student_station_id"),
                "remote_ip": self.ctx.get("student_remote_ip"),
                "last_seen_age_s": None,
            }
        last = self.ctx.get("student_last_heartbeat_mono_ns")
        age = None
        fresh = False
        if isinstance(last, int):
            age = max(0.0, (time.monotonic_ns() - last) / 1_000_000_000.0)
            fresh = age <= STUDENT_HEARTBEAT_FRESH_SEC
        return {
            "connected": fresh,
            "ready": bool(fresh and self.ctx.get("student_station_ready")),
            "station_id": self.ctx.get("student_station_id"),
            "remote_ip": self.ctx.get("student_remote_ip"),
            "last_seen_age_s": age,
        }

    def _validate_student_token_locked(self, attempt_id: str, token: str) -> None:
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        if attempt_id != self.ctx.get("attempt_id"):
            raise RuntimeError("student_attempt_mismatch")
        if self._student_attempt_state_locked() == "ended":
            raise RuntimeError("student_attempt_ended")
        expected = str(self.ctx.get("student_join_token") or "")
        if not expected or not hmac.compare_digest(str(token), expected):
            raise RuntimeError("invalid_student_join_token")

    def student_context(self, attempt_id: str, token: str) -> dict[str, Any]:
        with self.lock:
            self._validate_student_token_locked(attempt_id, token)
            s = self._student_station_status_locked()
            return {
                "ok": True,
                "attempt_id": self.ctx["attempt_id"],
                "student_id": self.ctx["student_id"],
                "student_name": self.ctx["student_name"],
                "scenario": self.ctx["scenario"],
                "attempt_state": self._student_attempt_state_locked(),
                "ready": bool(s["ready"]),
            }

    def student_heartbeat(self, body: dict[str, Any], remote_ip: str) -> dict[str, Any]:
        with self.lock:
            attempt_id = str(body.get("attempt_id") or "")
            token = str(body.get("token") or "")
            station_id = str(body.get("station_id") or "").strip()
            if not station_id or len(station_id) > 128:
                raise ValueError("valid station_id required")
            self._validate_student_token_locked(attempt_id, token)

            previous = self.ctx.get("student_station_id")
            if previous not in (None, station_id):
                raise RuntimeError("student_station_already_bound")

            state = self._student_attempt_state_locked()
            self.ctx["student_station_id"] = station_id
            if state in {"setup", "ready"}:
                self.ctx["student_station_ready"] = bool(body.get("ready"))
            self.ctx["student_last_heartbeat_mono_ns"] = time.monotonic_ns()
            self.ctx["student_last_heartbeat_utc_ns"] = time.time_ns()
            self.ctx["student_remote_ip"] = remote_ip

            status = self._student_station_status_locked()
            self._audit("student_station_heartbeat", {
                "attempt_id": attempt_id,
                "station_id": station_id,
                "ready": status["ready"],
                "attempt_state": state,
                "remote_ip": remote_ip,
            })
            return {"ok": True, "attempt_state": state, **status}

    def set_engineering_ready(self, ready: bool) -> dict[str, Any]:
        raise RuntimeError("engineering readiness override removed in DEV-018 rev4; use the real student station join link")

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        with self.lock:
            if not self.ctx:
                raise RuntimeError("create a Session first")
            if self.ctx["worker_started"]:
                return self.status()

            attempt = self.ctx["attempt_initial"]
            lease = self.slots.claim(
                attempt_id=attempt.id,
                session_id=self.ctx["session_id"],
                aircraft_id=attempt.aircraft_id,
                preferred_slot=1,
            )
            self.ctx["lease"] = lease

            fg = self.contract["FGObserver"](lease.slot.fg_udp)
            fg.__enter__()
            self.ctx["fg"] = fg

            runtime = Path(self.ctx["run_dir"]) / "direct" / attempt.id
            argv = self.contract["worker_argv"](
                self.base,
                self.source,
                self.jsbsim,
                Path(self.ctx["template_json"]),
                lease,
                runtime,
            )
            h = self.workers.start(self.instructor, attempt.id, argv=argv, cwd=self.base)
            self.ctx["worker_started"] = True
            self.ctx["worker_pid"] = getattr(h, "pid", None)
            self._audit("worker_started", {"attempt_id": attempt.id, "pid": getattr(h, "pid", None), "slot": getattr(lease.slot, "slot", None)})

            ev = self.contract["evidence"](
                self.source,
                self.base,
                fg,
                self.workers,
                self.instructor,
                attempt,
                lease,
                min(float(timeout), 60.0),
            )
            ready, ready_ev = self.readiness.mark_ready(
                self.instructor,
                attempt.id,
                ev,
                expected_revision=attempt.revision,
            )
            self.ctx["ready_attempt"] = ready
            self.ctx["ready_evidence"] = _jsonable(ready_ev)
            self._audit("attempt_ready", {"attempt_id": attempt.id, "revision": ready.revision})
            return self.status()

    def start(self) -> dict[str, Any]:
        with self.lock:
            if not self.ctx:
                raise RuntimeError("no current Attempt")
            station = self._student_station_status_locked()
            if not station.get("connected"):
                raise RuntimeError("student station is not connected or heartbeat is stale")
            if not station.get("ready"):
                raise RuntimeError("student station has not declared READY")
            ready = self.ctx.get("ready_attempt")
            lease = self.ctx.get("lease")
            fg = self.ctx.get("fg")
            attempt = self.ctx.get("attempt_initial")
            if ready is None or lease is None or fg is None:
                raise RuntimeError("Attempt has not passed trusted readiness")

            ev = self.contract["evidence"](
                self.source,
                self.base,
                fg,
                self.workers,
                self.instructor,
                attempt,
                lease,
                15.0,
            )
            active, active_ev = self.active_gate.start_active(
                self.instructor,
                attempt.id,
                ev,
                expected_revision=ready.revision,
            )
            self.ctx["active_attempt"] = active
            self.ctx["active_evidence"] = _jsonable(active_ev)
            self.started_mono = time.monotonic()
            self._audit("attempt_active", {"attempt_id": attempt.id, "revision": active.revision})
            return self.status()

    def end(self, reason: str) -> dict[str, Any]:
        with self.lock:
            if not self.ctx:
                raise RuntimeError("no current Attempt")
            attempt = self._attempt()
            if attempt is None:
                raise RuntimeError("Attempt missing from store")
            state = _state_text(attempt.state).lower()
            if state == "ended":
                return self.status()

            ended = self.sessions.transition(
                self.instructor,
                attempt.id,
                self.contract["State"].ENDED,
                expected_revision=attempt.revision,
                reason=reason or "instructor_console_end",
            )
            self.ctx["student_station_ready"] = False
            self._audit("attempt_ended", {"attempt_id": attempt.id, "revision": ended.revision, "reason": reason})
            self._audit("student_join_revoked", {"attempt_id": attempt.id, "reason": "attempt_ended"})
            self._cleanup_runtime()
            return self.status()

    def _cleanup_runtime(self) -> None:
        if not self.ctx:
            return
        attempt_id = self.ctx.get("attempt_id")
        if self.ctx.get("worker_started") and attempt_id:
            try:
                self.workers.stop(self.instructor, attempt_id, timeout=10.0)
            except Exception as exc:
                self._audit("worker_stop_error", {"attempt_id": attempt_id, "error": str(exc)})
            self.ctx["worker_started"] = False
        fg = self.ctx.get("fg")
        if fg is not None:
            try:
                fg.__exit__(None, None, None)
            except Exception:
                pass
            self.ctx["fg"] = None
        lease = self.ctx.get("lease")
        if lease is not None:
            try:
                self.slots.release(attempt_id)
            except Exception as exc:
                self._audit("slot_release_error", {"attempt_id": attempt_id, "error": str(exc)})
            self.ctx["lease"] = None

    def shutdown(self) -> None:
        with self.lock:
            try:
                if self.ctx and self.ctx.get("worker_started"):
                    attempt = self._attempt()
                    if attempt is not None and _state_text(attempt.state).lower() == "active":
                        try:
                            self.sessions.transition(
                                self.instructor,
                                attempt.id,
                                self.contract["State"].INTERRUPTED,
                                expected_revision=attempt.revision,
                                reason="instructor_console_shutdown",
                            )
                            self._audit("attempt_interrupted", {"attempt_id": attempt.id, "reason": "console_shutdown"})
                        except Exception as exc:
                            self._audit("interrupt_transition_error", {"attempt_id": attempt.id, "error": str(exc)})
                    self._cleanup_runtime()
            finally:
                try:
                    self.store.close()
                except Exception:
                    pass

    def _student_join_info_locked(self) -> dict[str, Any]:
        if not self.ctx:
            return {}
        if self._student_attempt_state_locked() == "ended":
            return {"revoked": True}
        token = str(self.ctx.get("student_join_token") or "")
        attempt = self.ctx["attempt_id"]
        path = f"/student.html?attempt={attempt}&token={token}"

        lan_ip = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("10.255.255.255", 1))
                candidate = s.getsockname()[0]
                if candidate and not ipaddress.ip_address(candidate).is_loopback:
                    lan_ip = candidate
            finally:
                s.close()
        except Exception:
            pass

        port = self.public_port or 8020
        return {
            "path": path,
            "local_url": f"http://127.0.0.1:{port}{path}",
            "lan_url": f"http://{lan_ip}:{port}{path}" if lan_ip else None,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            out: dict[str, Any] = {
                "ok": True,
                "dev": "DEV-018-rev5",
                "limitations": [
                    "Student-station lifecycle follows the authoritative Attempt and the join token is revoked at Ended; it is not yet the flight-control client.",
                    "Active student-station heartbeat loss is shown but does not yet automatically transition the Attempt to Interrupted.",
                    "Pause/Resume is not exposed until full backend pause semantics are implemented.",
                    "Flight/Payload takeover is not exposed in this revision.",
                    "Student control gateway/remote client is not launched by Instructor Console rev5.",
                    "Voice/webcam readiness is not yet authoritative in this revision.",
                ],
            }
            if not self.ctx:
                out["current"] = None
                return out

            attempt = self._attempt()
            pose = None
            fg = self.ctx.get("fg")
            if fg is not None:
                try:
                    pose = fg.drain()
                except Exception:
                    pose = None
            health = None
            if self.ctx.get("worker_started"):
                try:
                    health = self.workers.health(self.instructor, self.ctx["attempt_id"])
                except Exception as exc:
                    health = {"healthy": False, "error": str(exc)}

            state = _state_text(getattr(attempt, "state", None))
            out["current"] = {
                "session_id": self.ctx["session_id"],
                "exercise_id": self.ctx["exercise_id"],
                "attempt_id": self.ctx["attempt_id"],
                "student_id": self.ctx["student_id"],
                "student_name": self.ctx["student_name"],
                "scenario": self.ctx["scenario"],
                "level": self.ctx["level"],
                "training_mode": self.ctx["training_mode"],
                "role": self.ctx["role"],
                "environment": self.ctx["environment"],
                "runway": self.ctx["runway"],
                "state": state,
                "revision": getattr(attempt, "revision", None),
                "generation": getattr(attempt, "generation", None),
                "aircraft_id": getattr(attempt, "aircraft_id", self.ctx["aircraft_id"]),
                "student_station": self._student_station_status_locked(),
                "student_join": self._student_join_info_locked(),
                "worker_started": self.ctx.get("worker_started", False),
                "worker_health": _jsonable(health),
                "pose": _pose_dict(pose),
                "active_elapsed_s": (time.monotonic() - self.started_mono) if self.started_mono is not None and state.lower() == "active" else None,
            }
            return out


class Handler(BaseHTTPRequestHandler):
    runtime: InstructorRuntime
    web_root: Path

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except Exception:
            return False

    def _require_local_instructor(self) -> None:
        if not self._is_loopback():
            raise PermissionError("instructor_console_localhost_only")

    def log_message(self, fmt: str, *args: Any) -> None:
        print("HTTP", self.address_string(), fmt % args, flush=True)

    def _json(self, obj: Any, status: int = 200) -> None:
        raw = json.dumps(obj, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 1024 * 1024:
            raise ValueError("request too large")
        if n == 0:
            return {}
        obj = json.loads(self.rfile.read(n))
        if not isinstance(obj, dict):
            raise ValueError("JSON object required")
        return obj

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/student/context":
                qs = parse_qs(parsed.query)
                attempt = (qs.get("attempt") or [""])[0]
                token = (qs.get("token") or [""])[0]
                self._json(self.runtime.student_context(attempt, token))
                return

            if parsed.path == "/student.html":
                p = self.web_root / "student.html"
                raw = p.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            self._require_local_instructor()
            if parsed.path == "/api/status":
                self._json(self.runtime.status())
                return
            if parsed.path in {"/", "/index.html"}:
                p = self.web_root / "index.html"
                raw = p.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_error(404)
        except PermissionError as exc:
            self._json({"ok": False, "error": str(exc)}, 403)
        except (ValueError, RuntimeError) as exc:
            self._json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        try:
            body = self._body()
            if self.path == "/api/student/heartbeat":
                result = self.runtime.student_heartbeat(body, self.client_address[0])
                self._json(result)
                return

            self._require_local_instructor()
            if self.path == "/api/session/create":
                result = self.runtime.create(body)
            elif self.path == "/api/session/prepare":
                result = self.runtime.prepare(float(body.get("timeout", 60.0)))
            elif self.path == "/api/session/start":
                result = self.runtime.start()
            elif self.path == "/api/session/end":
                result = self.runtime.end(str(body.get("reason") or "exercise_completed"))
            else:
                self.send_error(404)
                return
            self._json(result)
        except PermissionError as exc:
            self._json({"ok": False, "error": str(exc)}, 403)
        except (ValueError, RuntimeError) as exc:
            self.runtime.last_error = str(exc)
            status = self.runtime.status() if self._is_loopback() else None
            self._json({"ok": False, "error": str(exc), "status": status}, 409)
        except Exception as exc:
            self.runtime.last_error = str(exc)
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(base: Path, source: Path, host: str, port: int, web_root: Path) -> int:
    runtime = InstructorRuntime(base, source)
    runtime.public_port = port
    Handler.runtime = runtime
    Handler.web_root = web_root
    server = ThreadingHTTPServer((host, port), Handler)

    stopped = threading.Event()

    def request_stop(signum=None, frame=None):
        if stopped.is_set():
            return
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(f"OMNI Instructor Console DEV-018 rev5: http://{host}:{port}/", flush=True)
    print(f"Core DB: {runtime.db_path}", flush=True)
    print("Ctrl+C stops the console; an Active attempt is transitioned toward Interrupted before scoped worker cleanup.", flush=True)
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
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--web-root")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.base).expanduser().resolve()
    source = Path(args.source).expanduser().resolve()
    if args.probe:
        print(json.dumps(probe_contract(base), indent=2, sort_keys=True))
        return 0

    web_root = Path(args.web_root).expanduser().resolve() if args.web_root else base / "web" / "instructor_console_v1"
    if not (web_root / "index.html").is_file():
        raise SystemExit(f"Instructor Console web assets missing: {web_root}")
    return serve(base, source, args.host, args.port, web_root)


if __name__ == "__main__":
    raise SystemExit(main())
