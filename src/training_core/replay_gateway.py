"""Read-only WebSocket replay gateway for existing Feather/Cesium."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import threading
import time

from .lan_protocol import (
    Assignment,
    PROTOCOL,
)
from .replay_reader import ReplayPackage

try:
    from websockets.legacy.server import serve as ws_serve
except Exception:
    from websockets import serve as ws_serve  # type: ignore


class ReplayGateway:
    def __init__(
        self,
        package: ReplayPackage,
        *,
        host: str = "127.0.0.1",
        port: int = 9200,
        token: str | None = None,
        speed: float = 1.0,
        loop_playback: bool = False,
    ):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.package = package
        self.host = host
        self.port = int(port)
        self.token = token or secrets.token_urlsafe(24)
        self.speed = float(speed)
        self.loop_playback = bool(loop_playback)
        self.assignment = Assignment(
            student_id="replay-viewer",
            session_id=str(
                package.identity.get("session_id", "unknown")
            ),
            attempt_id=str(
                package.identity["attempt_id"]
            ),
            aircraft_id=str(
                package.identity.get(
                    "aircraft_id", "unknown"
                )
            ),
            generation=int(
                package.identity.get("generation", 1)
            ),
            token=self.token,
        )
        self._loop = None
        self._server = None
        self._thread = None
        self._ready = threading.Event()
        self._error = None

    def start(self, timeout: float = 5.0) -> None:
        if (
            self._thread is not None
            and self._thread.is_alive()
        ):
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="OmniReplayGateway",
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(
                "replay gateway start timeout"
            )
        if self._error is not None:
            raise RuntimeError(
                f"replay gateway failed: {self._error}"
            )

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._shutdown(), self._loop
            )
            fut.result(timeout=4.0)
        except Exception:
            try:
                self._loop.call_soon_threadsafe(
                    self._loop.stop
                )
            except Exception:
                pass
        if (
            self._thread is not None
            and self._thread.is_alive()
        ):
            self._thread.join(timeout=4.0)

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
                loop.run_until_complete(
                    self._shutdown()
                )
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
            max_size=8192,
        )

    async def _send_json(self, ws, send_lock, payload: dict) -> None:
        async with send_lock:
            await ws.send(
                json.dumps(
                    payload,
                    separators=(",", ":"),
                )
            )

    async def _handler(self, ws) -> None:
        producer = None
        send_lock = asyncio.Lock()
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=5.0
            )
            try:
                hello = json.loads(raw)
            except Exception:
                await self._error_frame(
                    ws, "invalid_json", send_lock
                )
                return
            if not isinstance(hello, dict):
                await self._error_frame(
                    ws, "hello_not_object", send_lock
                )
                return
            if hello.get("protocol") != PROTOCOL:
                await self._error_frame(
                    ws, "protocol_mismatch", send_lock
                )
                return
            if (
                hello.get("student_id")
                != self.assignment.student_id
                or hello.get("token") != self.token
            ):
                await self._error_frame(
                    ws, "authentication_failed", send_lock
                )
                return

            await self._send_json(
                ws,
                send_lock,
                {
                    "type": "welcome",
                    "protocol": PROTOCOL,
                    "server_time_unix": time.time(),
                    "assignment": self.assignment.public(),
                    "capabilities": {
                        "telemetry": True,
                        "commands": False,
                        "cesium_pose": True,
                        "client_evidence": False,
                        "clock_sync": False,
                        "replay": True,
                    },
                    "replay": {
                        "attempt_id": self.package.identity[
                            "attempt_id"
                        ],
                        "duration_s": self.package.duration_s,
                        "frame_count": len(
                            self.package.frames
                        ),
                        "speed": self.speed,
                        "loop": self.loop_playback,
                        "read_only": True,
                    },
                },
            )
            producer = asyncio.create_task(
                self._stream(ws, send_lock)
            )
            async for raw_in in ws:
                try:
                    incoming = json.loads(raw_in)
                except Exception:
                    await self._error_frame(
                        ws, "invalid_json", send_lock
                    )
                    continue
                kind = (
                    incoming.get("type")
                    if isinstance(incoming, dict)
                    else None
                )
                if kind == "cmd":
                    await self._error_frame(
                        ws, "replay_read_only", send_lock
                    )
                elif kind == "clock_ping":
                    await self._error_frame(
                        ws, "replay_clock_sync_disabled", send_lock
                    )
                elif kind == "client_event":
                    # Never mutate the original Attempt evidence package.
                    continue
                else:
                    await self._error_frame(
                        ws, "replay_messages_read_only", send_lock
                    )
        except Exception:
            pass
        finally:
            if producer is not None:
                producer.cancel()
                try:
                    await producer
                except BaseException:
                    pass

    async def _stream(self, ws, send_lock) -> None:
        while True:
            start = time.monotonic()
            for idx, frame in enumerate(
                self.package.frames, 1
            ):
                target = (
                    start
                    + frame.relative_ns
                    / 1_000_000_000
                    / self.speed
                )
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # websockets.send() may complete without yielding when the
                    # kernel buffer has room. Accelerated replay must still
                    # yield so inbound read-only probes and disconnects are
                    # serviced promptly.
                    await asyncio.sleep(0)
                await self._send_json(
                    ws,
                    send_lock,
                    {
                        "type": "telemetry",
                        "protocol": PROTOCOL,
                        "seq": idx,
                        "server_time_unix": time.time(),
                        "routing": self.assignment.public(),
                        "data": frame.telemetry,
                        "quality": {
                            "pose_source": "replay_fg",
                            "replay": True,
                            "source_state_sequence": (
                                frame.source_sequence
                            ),
                            "source_elapsed_ns": (
                                frame.elapsed_ns
                            ),
                            "replay_relative_ns": (
                                frame.relative_ns
                            ),
                        },
                    },
                )
            await self._send_json(
                ws,
                send_lock,
                {
                    "type": "replay_end",
                    "protocol": PROTOCOL,
                    "attempt_id": (
                        self.package.identity[
                            "attempt_id"
                        ]
                    ),
                },
            )
            if not self.loop_playback:
                return
            await asyncio.sleep(0.5)

    async def _error_frame(
        self, ws, code: str, send_lock=None
    ) -> None:
        try:
            payload = {
                "type": "error",
                "protocol": PROTOCOL,
                "code": code,
                "server_time_unix": time.time(),
            }
            if send_lock is None:
                await ws.send(
                    json.dumps(
                        payload,
                        separators=(",", ":"),
                    )
                )
            else:
                await self._send_json(
                    ws, send_lock, payload
                )
        except Exception:
            pass

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if (
            self._loop is not None
            and self._loop.is_running()
        ):
            self._loop.call_soon(
                self._loop.stop
            )


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--evidence", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9200)
    p.add_argument("--token", default="")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--credentials-file")
    p.add_argument("--allow-incomplete", action="store_true")
    a = p.parse_args(argv)

    package = ReplayPackage(
        Path(a.evidence),
        allow_incomplete=a.allow_incomplete,
    )
    gateway = ReplayGateway(
        package,
        host=a.host,
        port=a.port,
        token=a.token or None,
        speed=a.speed,
        loop_playback=a.loop,
    )
    gateway.start()
    if a.credentials_file:
        path = Path(a.credentials_file)
        path.parent.mkdir(
            parents=True, exist_ok=True
        )
        path.write_text(
            json.dumps(
                {
                    "server": (
                        f"ws://127.0.0.1:{a.port}"
                    ),
                    "student": (
                        gateway.assignment.student_id
                    ),
                    "token": gateway.token,
                    "attempt_id": (
                        package.identity["attempt_id"]
                    ),
                    "aircraft_id": (
                        package.identity.get(
                            "aircraft_id"
                        )
                    ),
                    "duration_s": package.duration_s,
                    "frames": len(package.frames),
                },
                indent=2,
            )
            + "\n"
        )
    print(
        "REPLAY READY:",
        f"attempt={package.identity['attempt_id']}",
        f"frames={len(package.frames)}",
        f"duration={package.duration_s:.3f}s",
        f"speed={a.speed}x",
        flush=True,
    )
    for warning in package.integrity.warnings:
        print(
            "INTEGRITY WARNING:", warning,
            flush=True,
        )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
