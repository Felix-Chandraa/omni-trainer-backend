from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
from types import MethodType
from typing import Any

from . import instructor_console_rev9r3a as r3a


DEV018_REV9R3F_TRUE_FRESH_AUTHORITY_EPOCH = True


class InstructorRuntime(r3a.InstructorRuntime):
    """
    Repeatable recovery using a real authority-epoch rotation.

    Client command sequence is intentionally client-local. A complete Feather
    client restart resets it to 0. The protocol therefore MUST create a fresh
    authority epoch for every new authenticated Flight transport incarnation
    after the Attempt has entered Active/Paused/Interrupted.

    No server sequence rebasing is performed. No command-stream splitting is
    performed. Original FlightCommandRouter anti-replay semantics remain intact:
        (attempt_id, station_id, authority_epoch) -> last_sequence
    """

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9r3f_guard_installed = False
        self._rev9r3f_rotations = 0
        self._rev9r3f_last_rotation: dict[str, Any] | None = None

    def _force_rotate_principal_locked(self, router, principal, state: str):
        a = principal.assignment
        binding = router._bindings.get(a.attempt_id)
        if binding is None:
            raise RuntimeError("control_binding_missing")

        before = router.authority.current(a.attempt_id)

        lease = router.authority.grant(
            attempt_id=a.attempt_id,
            aircraft_id=a.aircraft_id,
            actor_id=principal.actor_id,
            station_id=principal.station_id,
            generation=a.generation,
            recorder=binding.recorder,
            reason="rev9r3f_new_authenticated_flight_transport",
            force_new_epoch=True,
        )

        if before is not None and lease.epoch <= before.epoch:
            raise RuntimeError(
                f"authority epoch did not advance: "
                f"before={before.epoch} after={lease.epoch}"
            )

        # Old sequence namespaces can never be valid after epoch rotation.
        # Prune them so repeat reconnects do not accumulate stale counters.
        with router._lock:
            for key in list(router._last_sequence):
                if (
                    len(key) == 3
                    and key[0] == a.attempt_id
                    and key[1] == principal.station_id
                    and int(key[2]) != int(lease.epoch)
                ):
                    router._last_sequence.pop(key, None)

        self._rev9r3f_rotations += 1
        self._rev9r3f_last_rotation = {
            "attempt_id": a.attempt_id,
            "station_id": principal.station_id,
            "state": state,
            "previous_epoch": (
                getattr(before, "epoch", None)
                if before is not None
                else None
            ),
            "new_epoch": int(lease.epoch),
            "rotation": self._rev9r3f_rotations,
        }
        self._audit(
            "flight_transport_authority_epoch_rotated",
            {
                **self._rev9r3f_last_rotation,
                "sequence_namespace":
                    "attempt+station+authority_epoch",
                "client_sequence_reset_safe": True,
            },
        )
        return lease

    def _install_true_epoch_guard_locked(self) -> None:
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("control bridge/router missing")

        router = bridge.router
        if getattr(router, "_dev018_rev9r3f_true_epoch_guard", False):
            self._rev9r3f_guard_installed = True
            return

        runtime = self
        # rev3a already installed its recovery reconnect guard. Keep it as the
        # downstream handler because it eventually calls the original router
        # on_connect which sends the CURRENT authority lease to Feather.
        previous_on_connect = router.on_connect

        async def on_connect_with_true_epoch(this, ws, principal):
            attempt_id = principal.assignment.attempt_id
            attempt = runtime.store.attempt(attempt_id)
            state = r3a.r3.r2.rev6._state_text(attempt.state).lower()

            # Initial Setup/Ready connection uses the original prepared lease.
            # Every subsequent live/recovery transport gets a real new epoch.
            if state in {"active", "interrupted", "paused"}:
                with runtime.lock:
                    runtime._force_rotate_principal_locked(
                        this,
                        principal,
                        state,
                    )

            # This sends the lease created above to the newly authenticated WS.
            await previous_on_connect(ws, principal)

        router.on_connect = MethodType(on_connect_with_true_epoch, router)
        router._dev018_rev9r3f_true_epoch_guard = True
        self._rev9r3f_guard_installed = True

        self._audit(
            "true_reconnect_epoch_guard_installed",
            {
                "attempt_id":
                    self.ctx.get("attempt_id") if self.ctx else None,
            },
        )

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        super().prepare(timeout)
        with self.lock:
            self._install_true_epoch_guard_locked()
            return self.status()

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r3f"
        current = out.get("current")
        if current is not None:
            bridge = self._control_bridge()
            current_epoch = None
            if bridge is not None and bridge.router is not None and self.ctx:
                lease = bridge.router.authority.current(
                    self.ctx["attempt_id"]
                )
                current_epoch = (
                    getattr(lease, "epoch", None)
                    if lease is not None
                    else None
                )
            current["reconnect_epoch"] = {
                "true_rotation": True,
                "rotation_count": self._rev9r3f_rotations,
                "current_epoch": current_epoch,
                "last_rotation": self._rev9r3f_last_rotation,
                "server_sequence_rebase": False,
                "logical_stream_sequence_split": False,
            }
        return out


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3a.probe(base, source)
    out.update(
        {
            "rev9r3f": True,
            "true_fresh_authority_epoch": True,
            "force_new_epoch_primitive": True,
            "sequence_namespace":
                "attempt+station+authority_epoch",
            "sequence_rebase_workaround": False,
            "logical_stream_workaround": False,
            "base_recovery": "rev9r3a",
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
        f"OMNI Instructor Console DEV-018 rev9 rev3f: "
        f"http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Reconnect protocol: every authenticated live/recovery Flight "
        "transport receives a REAL new authority epoch.",
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
