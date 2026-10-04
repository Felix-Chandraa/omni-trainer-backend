from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
from typing import Any

from . import instructor_console_rev9r3b as r3b


DEV018_REV9R3C_RECOVERY_RESUME_SEQUENCE_REBASE = True


class InstructorRuntime(r3b.InstructorRuntime):
    """
    rev9r3c fixes the non-transport-reconnect recovery case.

    If Student heartbeat is lost while the Flight websocket remains alive,
    there is no on_connect event and therefore no new authority epoch. Some
    Feather control paths restart their local command sequence after recovery.
    The server would then keep rejecting until the new sequence numerically
    catches the pre-interruption sequence.

    Recovery Resume is already protected by:
      * Attempt state PAUSED,
      * authoritative Flight HOLD,
      * explicit command barrier,
      * verified continuity,
      * command client monotonic freshness/expiry.

    Therefore, while still PAUSED and barriered, it is safe to clear only the
    anti-replay window for the CURRENT (attempt, station, authority epoch).
    Old delayed commands remain rejected by state/barrier before Active, and
    after Active by client-time freshness/expiry.
    """

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9r3c_rebase_count = 0
        self._rev9r3c_last_rebase: dict[str, Any] | None = None

    def _rebase_current_sequence_window_locked(self) -> dict[str, Any]:
        if not self.ctx:
            raise RuntimeError("no current Attempt")

        attempt_id = self.ctx["attempt_id"]
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("Flight router missing during sequence rebase")

        router = bridge.router
        lease = router.authority.current(attempt_id)
        if lease is None:
            raise RuntimeError(
                "Flight authority missing before recovery Resume sequence rebase"
            )

        station_id = getattr(lease, "station_id", None)
        epoch = getattr(lease, "epoch", None)
        if not station_id or not isinstance(epoch, int):
            raise RuntimeError("invalid current Flight authority lease")

        key = (attempt_id, station_id, int(epoch))

        with router._lock:
            previous = router._last_sequence.pop(key, None)

        info = {
            "attempt_id": attempt_id,
            "station_id": station_id,
            "authority_epoch": int(epoch),
            "previous_last_sequence": previous,
            "new_last_sequence": 0,
        }
        self._rev9r3c_rebase_count += 1
        self._rev9r3c_last_rebase = dict(info)
        self._audit(
            "recovery_resume_sequence_window_rebased",
            {
                **info,
                "reason": "continuity_verified_pause_resume_boundary",
                "attempt_state": "Paused",
                "command_barrier": True,
            },
        )
        return info

    def resume(self) -> dict[str, Any]:
        with self.lock:
            current = self._attempt()
            if current is None:
                raise RuntimeError("no current Attempt")

            state = r3b.r3a.r3.r2.rev6._state_text(current.state).lower()
            if state != "paused":
                raise RuntimeError("Resume requires a Paused Attempt")

            recovering = bool(
                self.ctx
                and self.ctx.get("continuity_verified")
            )

            if recovering:
                # Must happen while super().resume still sees PAUSED and while
                # _rev9_pause_barrier is still ON.
                if not self._rev9_pause_barrier:
                    raise RuntimeError(
                        "recovery Resume sequence rebase requires command barrier"
                    )
                self._rebase_current_sequence_window_locked()

        # super().resume performs:
        #   continuity checks -> physical SIGCONT -> fresh MAV ->
        #   PAUSED->ACTIVE -> barrier OFF.
        out = super().resume()

        if recovering:
            with self.lock:
                if self.ctx:
                    # Consume the one recovery marker. Later manual
                    # PAUSE/RESUME must not reset sequence unnecessarily.
                    self.ctx["continuity_verified"] = False

        return out

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r3c"
        current = out.get("current")
        if current is not None:
            current["sequence_resume_rebase"] = {
                "enabled": True,
                "rebase_only_after_verified_interruption_recovery": True,
                "count": self._rev9r3c_rebase_count,
                "last": self._rev9r3c_last_rebase,
            }
        return out


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3b.probe(base, source)
    out.update(
        {
            "rev9r3c": True,
            "recovery_resume_sequence_rebase": True,
            "rebase_scope": "attempt+station+current_authority_epoch",
            "rebase_only_after_verified_interruption_recovery": True,
            "manual_pause_resume_rebase": False,
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
    r3b.r3a.r3.r2.Handler.runtime = runtime
    r3b.r3a.r3.r2.Handler.web_root = web_root
    server = r3b.r3a.r3.r2.rev6.ThreadingHTTPServer(
        (host, port),
        r3b.r3a.r3.r2.Handler,
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
        f"OMNI Instructor Console DEV-018 rev9 rev3c: "
        f"http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Recovery Resume: rebase current epoch command sequence while PAUSED "
        "and command-barriered; then authoritative Resume.",
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
