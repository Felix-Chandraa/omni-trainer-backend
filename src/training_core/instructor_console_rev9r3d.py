from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
from typing import Any

from . import instructor_console_rev9r3c as r3c


DEV018_REV9R3D_REPEATABLE_RECOVERY_CYCLES = True


class InstructorRuntime(r3c.InstructorRuntime):
    """
    Make interruption recovery repeatable for N cycles.

    rev9r3c used a boolean ctx['continuity_verified'] as the trigger for
    sequence-window rebase. A boolean is not a durable transaction identity:
    after one cycle it is consumed, and a subsequent recovery can race with
    context updates and reach PAUSED without being recognized as a distinct
    new recovery transaction.

    rev9r3d assigns a monotonically increasing recovery_cycle. Every successful
    INTERRUPTED -> PAUSED transition creates exactly one new cycle. Resume must
    rebase that cycle exactly once before PAUSED -> ACTIVE.
    """

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9r3d_recovery_cycle = 0
        self._rev9r3d_rebased_cycle = 0
        self._rev9r3d_resumed_cycle = 0
        self._rev9r3d_cycle_history: list[dict[str, Any]] = []

    def _recover_interrupted_to_paused(
        self,
        continuity: dict[str, Any],
    ) -> bool:
        # Use rev9r3a/rev9r3 logic to perform the authoritative transition
        # and continuity verification. Only allocate a cycle AFTER it succeeds.
        changed = r3c.r3b.r3a.InstructorRuntime._recover_interrupted_to_paused(
            self,
            continuity,
        )
        if not changed:
            return False

        with self.lock:
            self._rev9r3d_recovery_cycle += 1
            cycle = self._rev9r3d_recovery_cycle
            entry = {
                "cycle": cycle,
                "phase": "paused_verified",
                "attempt_id": self.ctx.get("attempt_id") if self.ctx else None,
            }
            self._rev9r3d_cycle_history.append(entry)
            self._rev9r3d_cycle_history = self._rev9r3d_cycle_history[-16:]
            if self.ctx is not None:
                self.ctx["recovery_cycle"] = cycle
                self.ctx["continuity_verified"] = True

            self._audit(
                "recovery_cycle_created",
                {
                    "attempt_id": entry["attempt_id"],
                    "cycle": cycle,
                    "state": "Paused",
                },
            )
        return True

    def _rebase_cycle_locked(self, cycle: int) -> dict[str, Any]:
        if cycle <= self._rev9r3d_rebased_cycle:
            return {
                "cycle": cycle,
                "already_rebased": True,
            }

        info = self._rebase_current_sequence_window_locked()
        info["cycle"] = cycle
        info["already_rebased"] = False
        self._rev9r3d_rebased_cycle = cycle

        self._rev9r3d_cycle_history.append(
            {
                "cycle": cycle,
                "phase": "sequence_rebased",
                "attempt_id": info["attempt_id"],
                "authority_epoch": info["authority_epoch"],
                "previous_last_sequence":
                    info["previous_last_sequence"],
            }
        )
        self._rev9r3d_cycle_history = self._rev9r3d_cycle_history[-16:]

        self._audit(
            "recovery_cycle_sequence_rebased",
            info,
        )
        return info

    def resume(self) -> dict[str, Any]:
        with self.lock:
            current = self._attempt()
            if current is None:
                raise RuntimeError("no current Attempt")

            state = r3c.r3b.r3a.r3.r2.rev6._state_text(
                current.state
            ).lower()
            if state != "paused":
                raise RuntimeError("Resume requires a Paused Attempt")

            cycle = int(
                (self.ctx or {}).get("recovery_cycle")
                or self._rev9r3d_recovery_cycle
                or 0
            )

            is_recovery_resume = (
                cycle > 0
                and cycle > self._rev9r3d_resumed_cycle
            )

            if is_recovery_resume:
                if not self._rev9_pause_barrier:
                    raise RuntimeError(
                        "recovery cycle Resume requires command barrier"
                    )
                self._rebase_cycle_locked(cycle)

        # Deliberately bypass rev9r3c.resume() so the old boolean-based rebase
        # is not part of the transaction anymore. rev9r3b/r3a/r3/r2 carries
        # the authoritative physical Resume implementation.
        out = r3c.r3b.InstructorRuntime.resume(self)

        if is_recovery_resume:
            with self.lock:
                self._rev9r3d_resumed_cycle = cycle
                if self.ctx is not None:
                    self.ctx["continuity_verified"] = False
                self._rev9r3d_cycle_history.append(
                    {
                        "cycle": cycle,
                        "phase": "active_resumed",
                        "attempt_id":
                            self.ctx.get("attempt_id") if self.ctx else None,
                    }
                )
                self._rev9r3d_cycle_history = (
                    self._rev9r3d_cycle_history[-16:]
                )
                self._audit(
                    "recovery_cycle_resumed_active",
                    {
                        "attempt_id":
                            self.ctx.get("attempt_id") if self.ctx else None,
                        "cycle": cycle,
                    },
                )

        return out

    def start(self) -> dict[str, Any]:
        out = super().start()
        with self.lock:
            # New Attempt Active starts with no recovery transaction.
            self._rev9r3d_recovery_cycle = 0
            self._rev9r3d_rebased_cycle = 0
            self._rev9r3d_resumed_cycle = 0
            self._rev9r3d_cycle_history = []
            if self.ctx is not None:
                self.ctx["recovery_cycle"] = 0
            return self.status()

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r3d"
        current = out.get("current")
        if current is not None:
            current["repeat_recovery"] = {
                "cycle": self._rev9r3d_recovery_cycle,
                "rebased_cycle": self._rev9r3d_rebased_cycle,
                "resumed_cycle": self._rev9r3d_resumed_cycle,
                "repeatable": True,
                "history": list(self._rev9r3d_cycle_history),
            }
        return out


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3c.probe(base, source)
    out.update(
        {
            "rev9r3d": True,
            "repeatable_recovery_cycles": True,
            "recovery_identity": "monotonic_cycle",
            "sequence_rebase_exactly_once_per_recovery_cycle": True,
            "boolean_recovery_trigger_retired": True,
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
    r3c.r3b.r3a.r3.r2.Handler.runtime = runtime
    r3c.r3b.r3a.r3.r2.Handler.web_root = web_root
    server = r3c.r3b.r3a.r3.r2.rev6.ThreadingHTTPServer(
        (host, port),
        r3c.r3b.r3a.r3.r2.Handler,
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
        f"OMNI Instructor Console DEV-018 rev9 rev3d: "
        f"http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Recovery protocol: each INTERRUPTED->PAUSED transition creates a "
        "new recovery cycle; sequence is rebased exactly once per cycle.",
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
