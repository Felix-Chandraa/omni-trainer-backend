"""F1 — Telemetry Bus WebSocket (broadcaster multi-window).

Peran (SDD Bab 3 / F1): mengganti bridge lama `runJavaScript` ke SATU
QWebEngineView agar telemetri bisa dikonsumsi banyak window/monitor.

Desain:
- Server WebSocket asyncio yang berjalan di THREAD-nya sendiri (daemon), jadi
  event loop GUI PyQt tidak terganggu (regresi nol). BEBAS PyQt -> bisa
  di-unit-test headless & dipakai ulang di luar GUI.
- `broadcast(message)` AMAN dipanggil dari thread Qt (worker/GUI): pekerjaan
  socket dijadwalkan ke loop bus via run_coroutine_threadsafe.
- Pesan ber-ENVELOPE (SDD F1): {"type":"telemetry","data":{...}} dan
  {"type":"event",...}. Bus legacy dipaksa loopback-only, membatasi Origin
  browser, dan client->server message dinonaktifkan pada wiring produk.

Invarian: bus TIDAK menurunkan/menyatukan besaran apa pun (Bab 2.3). Ia hanya
menyiarkan payload yang SUDAH dirakit di satu titik (main_window.update_hud) —
sumber tunggal telemetri UI -> semua window identik by construction.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import threading
from typing import Any, Callable, Iterable, Optional, Set

try:
    import websockets
    from websockets.legacy.server import WebSocketServerProtocol  # type: ignore
except Exception:  # pragma: no cover - dependency check happens di startup
    websockets = None  # type: ignore
    WebSocketServerProtocol = Any  # type: ignore


ClientMessageHandler = Callable[[dict, "WebSocketServerProtocol"], None]


class TelemetryBusSecurityError(RuntimeError):
    """Fail-closed legacy bus security policy violation."""


def _is_loopback_host(host: str) -> bool:
    value = str(host).strip().lower()
    if value == "localhost":
        return True
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class TelemetryBus:
    """WebSocket broadcaster satu-ke-banyak untuk telemetri Omni-Trainer."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_client_message: Optional[ClientMessageHandler] = None,
        allowed_origins: Optional[Iterable[str]] = None,
        allow_client_messages: bool = False,
    ) -> None:
        if websockets is None:
            raise RuntimeError(
                "The 'websockets' package is not installed. Run: "
                "pip install websockets  (see requirements.txt)"
            )
        self._host = host
        self._port = port
        self._on_client_message = on_client_message
        configured_origins = tuple(allowed_origins or ())
        # None deliberately permits local non-browser diagnostics without an
        # Origin header. This is local-trust compatibility, not authentication.
        self._allowed_origins = tuple(dict.fromkeys((*configured_origins, None)))
        self._allow_client_messages = bool(allow_client_messages)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._clients: Set["WebSocketServerProtocol"] = set()
        self._ready = threading.Event()
        self._startup_abort = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._running = False
        self._start_error: Optional[BaseException] = None

    # ---- lifecycle ---------------------------------------------------- #

    def start(self, timeout: float = 3.0) -> bool:
        """Mulai server di thread sendiri; True hanya jika bind benar-benar sukses."""
        with self._lifecycle_lock:
            if not _is_loopback_host(self._host):
                self._running = False
                self._server = None
                self._start_error = TelemetryBusSecurityError(
                    "Legacy telemetry bus is loopback-local-only; "
                    f"refusing bind host {self._host!r}."
                )
                return False

            if self._running and self._server is not None:
                return True

            if self._thread is not None and self._thread.is_alive():
                self._start_error = RuntimeError(
                    "Telemetry bus startup/stop is still in progress."
                )
                return False

            self._ready.clear()
            self._startup_abort.clear()
            self._start_error = None
            self._server = None
            self._running = False
            self._thread = threading.Thread(
                target=self._run, name="TelemetryBus", daemon=True
            )
            thread = self._thread

        thread.start()

        if not self._ready.wait(timeout):
            timeout_error = TimeoutError(
                f"Telemetry bus startup timed out after {timeout:.3f}s"
            )
            with self._lifecycle_lock:
                self._start_error = timeout_error
                self._startup_abort.set()
                loop = self._loop

            # Fail closed: interrupt an in-progress startup so it cannot later
            # become an untracked/ghost listener after start() returned False.
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass

            if thread.is_alive() and threading.current_thread() is not thread:
                thread.join(timeout=1.0)
            return False

        with self._lifecycle_lock:
            return (
                self._running
                and self._server is not None
                and self._start_error is None
            )

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._startup_abort.set()
            loop = self._loop
            thread = self._thread

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

        if (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        ):
            thread.join(timeout=2.0)

        with self._lifecycle_lock:
            self._running = False
            if thread is None or not thread.is_alive():
                self._server = None
                self._loop = None

    @property
    def start_error(self) -> Optional[BaseException]:
        """Most recent startup failure, or None after a successful start."""
        with self._lifecycle_lock:
            return self._start_error

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def security_posture(self) -> str:
        return (
            "loopback-local-only; browser-origin-restricted; "
            "unauthenticated-local-telemetry; client-messages-disabled"
        )

    # ---- publish (thread-safe) --------------------------------------- #

    def broadcast(self, message: dict) -> None:
        """Kirim satu envelope ke semua client. Aman dari thread mana pun.

        Fire-and-forget: tidak memblokir pemanggil (Qt thread). Payload
        di-serialize sekali; client yang putus dibuang di coroutine bus.
        """
        loop = self._loop
        if (
            not self._running
            or loop is None
            or loop.is_closed()
            or not self._clients
        ):
            return
        try:
            text = json.dumps(message, separators=(",", ":"), default=_json_default)
        except (TypeError, ValueError):
            return
        # jadwalkan di loop bus; tidak menunggu hasil
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(text), loop)
        except RuntimeError:
            # Loop may close between the state check and scheduling.
            return

    def publish_telemetry(self, data: dict) -> None:
        """Convenience: bungkus payload UI sbg envelope telemetry."""
        self.broadcast({"type": "telemetry", "data": data})

    def publish_event(self, name: str, **payload: Any) -> None:
        self.broadcast({"type": "event", "name": name, **payload})

    # ---- internals (jalan di loop bus) ------------------------------- #

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        server = None
        with self._lifecycle_lock:
            self._loop = loop
        asyncio.set_event_loop(loop)

        try:
            if self._startup_abort.is_set():
                self._ready.set()
                return

            server = loop.run_until_complete(self._serve())

            with self._lifecycle_lock:
                if self._startup_abort.is_set():
                    # Caller already timed out/stopped. Never publish this
                    # late bind as a successful running server.
                    self._running = False
                else:
                    self._server = server
                    self._running = True
                    self._start_error = None

            self._ready.set()

            if self._startup_abort.is_set():
                return

            loop.run_forever()
        except BaseException as exc:
            with self._lifecycle_lock:
                # Preserve a caller-created timeout error; otherwise expose
                # the concrete bind/startup exception.
                if self._start_error is None:
                    self._start_error = exc
                self._running = False
                self._server = None
            self._ready.set()
        finally:
            if server is not None:
                try:
                    server.close()
                    if hasattr(server, "wait_closed") and not loop.is_closed():
                        loop.run_until_complete(server.wait_closed())
                except (RuntimeError, Exception):
                    pass

            self._clients.clear()

            if not loop.is_closed():
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    try:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    except RuntimeError:
                        pass
                loop.close()

            with self._lifecycle_lock:
                self._running = False
                if self._server is server:
                    self._server = None
                if self._loop is loop:
                    self._loop = None
            self._ready.set()

    async def _serve(self):
        return await websockets.serve(
            self._handler,
            self._host,
            self._port,
            ping_interval=20,
            origins=list(self._allowed_origins),
        )

    async def _handler(self, ws: "WebSocketServerProtocol") -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                if not self._allow_client_messages:
                    await ws.close(
                        code=1008,
                        reason="Legacy telemetry bus is receive-only",
                    )
                    break
                self._on_inbound(raw, ws)
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    def _on_inbound(self, raw: Any, ws: "WebSocketServerProtocol") -> None:
        if self._on_client_message is None:
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if isinstance(msg, dict):
            # F1: hanya diteruskan. Validasi (whitelist skenario) = P4.
            self._on_client_message(msg, ws)

    async def _broadcast(self, text: str) -> None:
        if not self._clients:
            return
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


def _json_default(obj: Any) -> Any:
    # Toleran terhadap tipe non-JSON (mis. numpy floats) tanpa crash broadcast.
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)
