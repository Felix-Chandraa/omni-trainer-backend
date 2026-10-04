from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
from types import MethodType
from typing import Any

from . import instructor_console_rev9r3a as r3a


DEV018_REV9R3B_RECONNECT_EPOCH_SEQUENCE_FIX = True


class InstructorRuntime(r3a.InstructorRuntime):
    """Fresh authority epoch for every live/recovery Flight reconnect."""

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9r3b_guard_installed = False
        self._rev9r3b_reconnect_epoch_count = 0
        self._rev9r3b_last_reconnect_epoch: int | None = None

    def _install_reconnect_epoch_guard_locked(self) -> None:
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("control bridge/router missing")

        router = bridge.router
        if getattr(router, "_dev018_rev9r3b_epoch_guard", False):
            self._rev9r3b_guard_installed = True
            return

        runtime = self
        previous_on_connect = router.on_connect

        async def epoch_on_connect(this, ws, principal):
            attempt_id = principal.assignment.attempt_id
            attempt = runtime.store.attempt(attempt_id)
            state = r3a.r3.r2.rev6._state_text(attempt.state).lower()

            # Initial Student connection occurs in Setup/Ready and keeps the
            # epoch prepared by rev6. Any later connection while the attempt
            # is live/recovering is a new command transport incarnation.
            #
            # Always mint a fresh authority epoch here—even when the same
            # Student remains owner. FlightCommandRouter anti-replay state is
            # keyed by (attempt, station, epoch), so the new transport can
            # safely restart its local command sequence from 1 while packets
            # carrying the old epoch are rejected by authority.validate().
            if state in {"active", "interrupted", "paused"}:
                before = this.authority.current(attempt_id)
                lease_info = this.grant_principal(
                    principal,
                    reason="rev9r3b_flight_transport_reconnect_fresh_epoch",
                )
                new_epoch = int(lease_info["epoch"])
                runtime._rev9r3b_reconnect_epoch_count += 1
                runtime._rev9r3b_last_reconnect_epoch = new_epoch
                runtime._audit(
                    "flight_transport_reconnect_fresh_epoch",
                    {
                        "attempt_id": attempt_id,
                        "state": state,
                        "station_id": principal.station_id,
                        "previous_epoch": (
                            getattr(before, "epoch", None)
                            if before is not None
                            else None
                        ),
                        "new_epoch": new_epoch,
                        "sequence_namespace_reset_by_epoch": True,
                    },
                )

            # rev9r3a's on_connect then sends the CURRENT authoritative lease
            # to Feather. Because the grant above runs first, Feather receives
            # the new epoch immediately.
            await previous_on_connect(ws, principal)

        router.on_connect = MethodType(epoch_on_connect, router)
        router._dev018_rev9r3b_epoch_guard = True
        self._rev9r3b_guard_installed = True
        self._audit(
            "reconnect_epoch_guard_installed",
            {"attempt_id": self.ctx.get("attempt_id") if self.ctx else None},
        )

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        super().prepare(timeout)
        with self.lock:
            self._install_reconnect_epoch_guard_locked()
            return self.status()

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r3b"
        current = out.get("current")
        if current is not None:
            current["command_sequence_recovery"] = {
                "epoch_scoped": True,
                "fresh_epoch_on_live_reconnect": True,
                "reconnect_epoch_count":
                    self._rev9r3b_reconnect_epoch_count,
                "last_reconnect_epoch":
                    self._rev9r3b_last_reconnect_epoch,
                "client_sequence_may_restart_at_one": True,
            }
        return out


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3a.probe(base, source)
    out.update(
        {
            "rev9r3b": True,
            "sequence_namespace": "attempt+station+authority_epoch",
            "fresh_epoch_on_active_reconnect": True,
            "fresh_epoch_on_interrupted_reconnect": True,
            "fresh_epoch_on_paused_reconnect": True,
            "client_sequence_restart_supported": True,
            "auto_resume": False,
        }
    )
    out["ok"] = bool(out.get("ok"))
    return out


def serve(
    base: Path,
    source: Path,
    host: str,
    port: int,
    web_root: Path,
) -> int:
    runtime = InstructorRuntime(base, source)
    r3a.r3.r2.Handler.runtime = runtime
    r3a.r3.r2.Handler.web_root = web_root
    server = r3a.r3.r2.rev6.ThreadingHTTPServer(
        (host, port),
        r3a.r3.r2.Handler,
    )
    runtime.public_port = port
    stopped = threading.Event()

    def request_stop(signum=None, frame=None):
        if stopped.is_set():
            return
        stopped.set()
        threading.Thread(
            target=server.shutdown,
            daemon=True,
        ).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(
        f"OMNI Instructor Console DEV-018 rev9 rev3b: "
        f"http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Reconnect protocol: every live/recovery Flight reconnect gets a "
        "fresh authority epoch; command sequence restarts are safe.",
        flush=True,
    )

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        runtime.shutdown()
    return 0


def main(argv=None) -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--base",required=True)
    ap.add_argument("--source",required=True)
    ap.add_argument("--host",default="0.0.0.0")
    ap.add_argument("--port",type=int,default=8020)
    ap.add_argument("--web-root")
    ap.add_argument("--probe",action="store_true")
    args=ap.parse_args(argv)

    base=Path(args.base).expanduser().resolve()
    source=Path(args.source).expanduser().resolve()

    if args.probe:
        result=probe(base,source)
        print(json.dumps(result,indent=2,sort_keys=True))
        return 0 if result.get("ok") else 70

    web_root=(
        Path(args.web_root).expanduser().resolve()
        if args.web_root
        else base/"web"/"instructor_console_v1"
    )
    return serve(base,source,args.host,args.port,web_root)


if __name__=="__main__":
    raise SystemExit(main())
