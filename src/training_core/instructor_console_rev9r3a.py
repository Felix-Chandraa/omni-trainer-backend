from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
from types import MethodType
from typing import Any

from . import instructor_console_rev9r3 as r3


DEV018_REV9R3A_RECOVERY_SLOT_HOTFIX = True


def _authority_public(lease):
    if lease is None:
        return None
    if hasattr(lease, "public"):
        try:
            return lease.public()
        except Exception:
            pass
    return {
        "attempt_id": getattr(lease, "attempt_id", None),
        "aircraft_id": getattr(lease, "aircraft_id", None),
        "actor_id": getattr(lease, "actor_id", None),
        "station_id": getattr(lease, "station_id", None),
        "generation": getattr(lease, "generation", None),
        "epoch": getattr(lease, "epoch", None),
    }


class InstructorRuntime(r3.InstructorRuntime):
    """rev9r3 hotfix: direct router continuity + fresh reconnect authority."""

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9r3a_guard_installed = False

    def _install_reconnect_authority_guard_locked(self) -> None:
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("control bridge/router missing")

        router = bridge.router
        if getattr(router, "_dev018_rev9r3a_reconnect_guard", False):
            self._rev9r3a_guard_installed = True
            return

        runtime = self
        original = router.on_connect

        async def guarded_on_connect(this, ws, principal):
            attempt_id = principal.assignment.attempt_id
            current = runtime.store.attempt(attempt_id)
            state = r3.r2.rev6._state_text(current.state).lower()

            lease = this.authority.current(attempt_id)

            # A real control disconnect may invalidate the authority lease.
            # If the SAME authenticated/bound Student principal reconnects
            # during Interrupted/Paused, issue a fresh epoch. Commands still
            # cannot execute because state != ACTIVE and pause barrier is ON.
            if state in {"interrupted", "paused"} and lease is None:
                lease_info = this.grant_principal(
                    principal,
                    reason="rev9r3a_verified_same_principal_reconnect",
                )
                runtime._audit(
                    "flight_authority_regranted_for_recovery",
                    {
                        "attempt_id": attempt_id,
                        "state": state,
                        "station_id": principal.station_id,
                        "epoch": lease_info.get("epoch"),
                        "commands_enabled": False,
                    },
                )

            # Original on_connect sends the authoritative current lease/epoch
            # to Feather. This is essential if reconnect created a fresh epoch.
            await original(ws, principal)

        router.on_connect = MethodType(guarded_on_connect, router)
        router._dev018_rev9r3a_reconnect_guard = True
        self._rev9r3a_guard_installed = True

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        out = super().prepare(timeout)
        with self.lock:
            self._install_reconnect_authority_guard_locked()
            return self.status()

    def _continuity_snapshot_locked(self) -> dict[str, Any]:
        station = self._student_station_status_locked()
        bridge = self._control_bridge()

        client_connected = False
        connection = {}
        authority = None

        if bridge is not None and bridge.router is not None:
            router = bridge.router

            # Do not depend on Dev018ControlBridge.public_status() field names.
            # Read the authoritative router state directly.
            try:
                connection = router.connection_snapshot()
                client_connected = bool(
                    connection.get("client_connected")
                    or int(connection.get("client_count") or 0) > 0
                )
            except Exception:
                try:
                    status = bridge.public_status()
                    client_connected = bool(status.get("client_connected"))
                    connection = dict(status)
                except Exception:
                    pass

            if self.ctx:
                authority = router.authority.current(
                    self.ctx["attempt_id"]
                )

        expected_station = None if not self.ctx else self.ctx.get(
            "student_station_id"
        )
        actual_station = station.get("station_id")

        processes_preserved = True
        if self._rev9_hold_snapshot:
            processes_preserved = all(
                r3.r2._same_process(identity)
                for identity in self._rev9_hold_snapshot.values()
            )

        return {
            "student_connected": bool(station.get("connected")),
            "student_station_id": actual_station,
            "expected_station_id": expected_station,
            "same_station": bool(
                expected_station
                and actual_station
                and expected_station == actual_station
            ),
            "control_connected": client_connected,
            "control_connection": connection,
            "authority_present": authority is not None,
            "authority": _authority_public(authority),
            "process_identity_preserved": bool(processes_preserved),
            "student_last_seen_age_s": station.get("last_seen_age_s"),
        }

    @staticmethod
    def _continuity_ok(c: dict[str, Any]) -> bool:
        # Authority is intentionally NOT a prerequisite here.
        # Same authenticated station + Flight transport + preserved held
        # processes establish continuity. If authority was invalidated by
        # disconnect, reconnect guard issues a fresh epoch while commands
        # remain blocked.
        return bool(
            c.get("student_connected")
            and c.get("same_station")
            and c.get("control_connected")
            and c.get("process_identity_preserved")
        )

    def _recover_interrupted_to_paused(
        self,
        continuity: dict[str, Any],
    ) -> bool:
        with self.lock:
            current = self._attempt()
            if current is None:
                return False
            if r3.r2.rev6._state_text(current.state).lower() != "interrupted":
                return False
            if not self._continuity_ok(continuity):
                return False
            if (
                self._rev9_hold_snapshot is None
                or self._rev9_hold_root_pid is None
            ):
                raise RuntimeError(
                    "Interrupted Attempt lost authoritative hold snapshot"
                )

            bridge = self._control_bridge()
            if bridge is None or bridge.router is None:
                raise RuntimeError("Flight router missing during recovery")

            # By this point reconnect/on_connect normally recreated authority.
            # Fail closed if it somehow did not; do not claim Paused recovery.
            lease = bridge.router.authority.current(current.id)
            if lease is None:
                raise RuntimeError(
                    "Flight authority not restored after verified reconnect"
                )

            paused = self.sessions.transition(
                self.instructor,
                current.id,
                self.contract["State"].PAUSED,
                expected_revision=current.revision,
                reason="continuity_verified_after_interruption",
                continuity_verified=True,
            )
            self.ctx["interrupted_attempt"] = None
            self.ctx["paused_attempt"] = paused
            self.ctx["continuity_verified"] = True

            import time
            self._rev9r3_recovery_verified_utc_ns = time.time_ns()

            self._record_lifecycle_locked("Paused", paused.revision)
            self._audit(
                "interrupted_continuity_verified_to_paused",
                {
                    "attempt_id": current.id,
                    "revision": paused.revision,
                    "continuity": self._continuity_snapshot_locked(),
                    "authority_epoch": getattr(lease, "epoch", None),
                    "authoritative_hold_remains": True,
                    "auto_resume": False,
                },
            )
            return True

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r3a"
        current = out.get("current")
        if current is not None:
            current["recovery_hotfix"] = {
                "router_authority_direct": True,
                "fresh_epoch_on_same_principal_reconnect": True,
                "authority_required_before_paused": True,
                "auto_resume": False,
            }
        return out


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3.probe(base, source)
    out.update(
        {
            "rev9r3a": True,
            "router_authority_direct": True,
            "fresh_epoch_on_same_principal_reconnect": True,
            "stale_slot_reconciliation_installer": True,
            "auto_resume": False,
        }
    )
    out["ok"] = bool(out.get("ok"))
    return out


def serve(base: Path, source: Path, host: str, port: int, web_root: Path) -> int:
    runtime = InstructorRuntime(base, source)
    r3.r2.Handler.runtime = runtime
    r3.r2.Handler.web_root = web_root
    server = r3.r2.rev6.ThreadingHTTPServer(
        (host, port),
        r3.r2.Handler,
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
        f"OMNI Instructor Console DEV-018 rev9 rev3a: http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Recovery hotfix: direct router continuity + fresh authority epoch "
        "on same-principal reconnect.",
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
