"""OMNI DEV-011 read-only LAN training protocol."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hmac
import json
import secrets
import threading
import time
from typing import Any

try:
    from websockets.legacy.server import serve as ws_serve
except Exception:
    from websockets import serve as ws_serve  # type: ignore

PROTOCOL = "omni.training.v1"
MAX_HELLO_BYTES = 8192
HELLO_TIMEOUT_SEC = 6.0


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Assignment:
    student_id: str
    session_id: str
    attempt_id: str
    aircraft_id: str
    generation: int
    token: str

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("token", None)
        return data


def issue_token() -> str:
    return secrets.token_urlsafe(24)


def parse_hello(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_HELLO_BYTES:
        raise ProtocolError("hello_too_large")
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_json") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("hello_not_object")
    if msg.get("type") != "hello":
        raise ProtocolError("hello_required")
    if msg.get("protocol") != PROTOCOL:
        raise ProtocolError("protocol_mismatch")
    if msg.get("role") != "student":
        raise ProtocolError("student_role_required")
    student_id = msg.get("student_id")
    token = msg.get("token")
    if not isinstance(student_id, str) or not student_id or len(student_id) > 128:
        raise ProtocolError("invalid_student_id")
    if not isinstance(token, str) or not token or len(token) > 256:
        raise ProtocolError("invalid_token")
    return msg


class LanTelemetryGateway:
    """Threaded asyncio WebSocket server with assignment-scoped fanout."""

    def __init__(self, host: str = "0.0.0.0", port: int = 9100):
        self.host = host
        self.port = int(port)
        self._by_student: dict[str, Assignment] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._seq: dict[str, int] = {}
        self._clients: dict[Any, Assignment] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def register_assignment(
        self,
        *,
        student_id: str,
        session_id: str,
        attempt_id: str,
        aircraft_id: str,
        generation: int,
        token: str | None = None,
    ) -> Assignment:
        if student_id in self._by_student:
            raise ProtocolError(f"duplicate_student:{student_id}")
        assignment = Assignment(
            student_id=student_id,
            session_id=session_id,
            attempt_id=attempt_id,
            aircraft_id=aircraft_id,
            generation=int(generation),
            token=token or issue_token(),
        )
        self._by_student[student_id] = assignment
        self._seq[attempt_id] = 0
        return assignment

    def start(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="OmniLanGateway"
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise ProtocolError("gateway_start_timeout")
        if self._error is not None:
            raise ProtocolError(f"gateway_bind_failed:{self._error}")

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                fut.result(timeout=4.0)
            except Exception:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4.0)

    def publish_telemetry(
        self,
        assignment: Assignment,
        data: dict[str, Any],
        *,
        quality: dict[str, Any] | None = None,
    ) -> None:
        self._seq[assignment.attempt_id] = self._seq.get(assignment.attempt_id, 0) + 1
        envelope = {
            "type": "telemetry",
            "protocol": PROTOCOL,
            "seq": self._seq[assignment.attempt_id],
            "server_time_unix": time.time(),
            "routing": assignment.public(),
            "data": data,
            "quality": quality or {},
        }
        self._latest[assignment.attempt_id] = envelope
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._publish_for_assignment(assignment, envelope), loop
            )

    def publish_event(self, assignment: Assignment, name: str, **payload: Any) -> None:
        envelope = {
            "type": "event",
            "protocol": PROTOCOL,
            "server_time_unix": time.time(),
            "routing": assignment.public(),
            "name": name,
            **payload,
        }
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._publish_for_assignment(assignment, envelope), loop
            )

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            self._error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                if self._server is not None:
                    self._server.close()
                    loop.run_until_complete(self._server.wait_closed())
            except Exception:
                pass
            loop.close()

    async def _serve(self) -> None:
        self._server = await ws_serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_HELLO_BYTES,
        )

    async def _handler(self, ws) -> None:
        assignment: Assignment | None = None
        try:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                await self._send_error(ws, "hello_timeout")
                return

            try:
                msg = parse_hello(raw)
                assignment = self._authenticate(msg["student_id"], msg["token"])
            except ProtocolError as exc:
                await self._send_error(ws, str(exc))
                return

            self._clients[ws] = assignment
            await ws.send(json.dumps({
                "type": "welcome",
                "protocol": PROTOCOL,
                "server_time_unix": time.time(),
                "assignment": assignment.public(),
                "capabilities": {
                    "telemetry": True,
                    "commands": False,
                    "cesium_pose": True,
                },
            }, separators=(",", ":")))

            latest = self._latest.get(assignment.attempt_id)
            if latest is not None:
                await ws.send(json.dumps(latest, separators=(",", ":")))

            async for raw_in in ws:
                try:
                    incoming = json.loads(raw_in)
                except (TypeError, ValueError):
                    await self._send_error(ws, "invalid_json")
                    continue
                if isinstance(incoming, dict) and incoming.get("type") == "cmd":
                    await self._send_error(ws, "commands_disabled_dev011")
                else:
                    await self._send_error(ws, "client_messages_read_only")
        except Exception:
            pass
        finally:
            self._clients.pop(ws, None)

    def _authenticate(self, student_id: str, token: str) -> Assignment:
        assignment = self._by_student.get(student_id)
        if assignment is None:
            raise ProtocolError("unknown_student")
        if not hmac.compare_digest(assignment.token, token):
            raise ProtocolError("authentication_failed")
        return assignment

    async def _publish_for_assignment(
        self, assignment: Assignment, envelope: dict[str, Any]
    ) -> None:
        text = json.dumps(envelope, separators=(",", ":"), default=str)
        dead = []
        for ws, current in list(self._clients.items()):
            if current.attempt_id != assignment.attempt_id:
                continue
            try:
                await ws.send(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)

    async def _send_error(self, ws, code: str) -> None:
        try:
            await ws.send(json.dumps({
                "type": "error",
                "protocol": PROTOCOL,
                "code": code,
                "server_time_unix": time.time(),
            }, separators=(",", ":")))
        except Exception:
            pass

    async def _shutdown(self) -> None:
        clients = list(self._clients.keys())
        self._clients.clear()
        for ws in clients:
            try:
                await ws.close(code=1001, reason="server_shutdown")
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon(self._loop.stop)
