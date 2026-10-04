from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
from types import MethodType
from typing import Any

from . import instructor_console_rev9r3c as r3c


DEV018_REV9R3E_LOGICAL_COMMAND_STREAM_SEQUENCE = True


def _sequence_stream(name: str) -> str:
    if name in {"flight_axes", "release_axes"}:
        return "axes"
    if name in {"arm", "disarm"}:
        return "discrete"
    return f"other:{name}"


class InstructorRuntime(r3c.InstructorRuntime):
    """
    Keep anti-replay ordering per logical command stream.

    FlightCommandRouter historically used one sequence namespace for every
    Flight command:
        (attempt, station, authority_epoch)

    Feather control has independent continuous-axis and discrete-action paths.
    After interruption/recovery, a fast axis producer can advance the shared
    server sequence ahead of ARM/DISARM, causing valid discrete commands to be
    rejected until their local counter catches up.

    rev9r3e keeps two monotonic anti-replay domains:
        axes     = flight_axes + release_axes
        discrete = arm + disarm

    This preserves ordering where it matters:
      * release_axes is ordered with flight_axes
      * arm/disarm are ordered with each other
      * continuous axes cannot make a valid ARM stale
      * stale commands within either stream remain rejected
    """

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._rev9r3e_guard_installed = False

    def _install_stream_sequence_guard_locked(self) -> None:
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("control bridge/router missing")

        router = bridge.router
        if getattr(router, "_dev018_rev9r3e_stream_guard", False):
            self._rev9r3e_guard_installed = True
            return

        original = router._validate_envelope
        router._dev018_stream_last_sequence = {}

        def validate_by_stream(this, principal, incoming, binding):
            name = incoming.get("name")
            epoch = incoming.get("authority_epoch")
            sequence = incoming.get("sequence")

            # Let the original validator own schema/authority/state/freshness
            # validation. We only replace its cross-command sequence domain.
            if (
                isinstance(name, str)
                and isinstance(epoch, int)
                and isinstance(sequence, int)
            ):
                attempt_id = principal.assignment.attempt_id
                station_id = principal.station_id
                legacy_key = (attempt_id, station_id, int(epoch))
                stream = _sequence_stream(name)
                stream_key = (
                    attempt_id,
                    station_id,
                    int(epoch),
                    stream,
                )

                with this._lock:
                    previous = this._dev018_stream_last_sequence.get(
                        stream_key, 0
                    )
                    if sequence <= previous:
                        raise ValueError("stale_command_sequence")

                    # Disable only the old aggregate sequence check while the
                    # original validator executes. RLock makes this atomic
                    # relative to other command validations on this router.
                    this._last_sequence.pop(legacy_key, None)
                    try:
                        result = original(
                            principal,
                            incoming,
                            binding,
                        )
                    finally:
                        # The original validator writes legacy_key on success.
                        # It is intentionally not authoritative anymore.
                        this._last_sequence.pop(legacy_key, None)

                    this._dev018_stream_last_sequence[
                        stream_key
                    ] = sequence
                    return result

            return original(principal, incoming, binding)

        router._validate_envelope = MethodType(
            validate_by_stream,
            router,
        )
        router._dev018_rev9r3e_stream_guard = True
        self._rev9r3e_guard_installed = True
        self._audit(
            "logical_command_stream_sequence_guard_installed",
            {
                "streams": {
                    "axes": ["flight_axes", "release_axes"],
                    "discrete": ["arm", "disarm"],
                }
            },
        )

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        super().prepare(timeout)
        with self.lock:
            self._install_stream_sequence_guard_locked()
            return self.status()

    def _rebase_current_sequence_window_locked(self) -> dict[str, Any]:
        """
        Recovery rebase clears both logical streams for the current authority
        epoch while PAUSED + command barrier remain authoritative.
        """
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
                "Flight authority missing before recovery sequence rebase"
            )

        station_id = getattr(lease, "station_id", None)
        epoch = getattr(lease, "epoch", None)
        if not station_id or not isinstance(epoch, int):
            raise RuntimeError("invalid current Flight authority lease")

        removed: dict[str, int | None] = {}
        with router._lock:
            legacy_key = (
                attempt_id,
                station_id,
                int(epoch),
            )
            router._last_sequence.pop(legacy_key, None)

            stream_state = getattr(
                router,
                "_dev018_stream_last_sequence",
                {},
            )
            for stream in ("axes", "discrete"):
                key = (
                    attempt_id,
                    station_id,
                    int(epoch),
                    stream,
                )
                removed[stream] = stream_state.pop(
                    key,
                    None,
                )

        info = {
            "attempt_id": attempt_id,
            "station_id": station_id,
            "authority_epoch": int(epoch),
            "previous_stream_sequences": removed,
            "new_stream_sequences": {
                "axes": 0,
                "discrete": 0,
            },
        }
        self._rev9r3c_rebase_count += 1
        self._rev9r3c_last_rebase = dict(info)
        self._audit(
            "recovery_resume_logical_stream_sequences_rebased",
            {
                **info,
                "reason":
                    "continuity_verified_pause_resume_boundary",
                "attempt_state": "Paused",
                "command_barrier": True,
            },
        )
        return info

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-rev9r3e"
        current = out.get("current")
        if current is not None:
            current["command_sequence_domains"] = {
                "mode": "logical_stream",
                "axes": [
                    "flight_axes",
                    "release_axes",
                ],
                "discrete": [
                    "arm",
                    "disarm",
                ],
                "authority_epoch_scoped": True,
                "recovery_rebase_both_streams": True,
            }
        return out


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3c.probe(base, source)
    out.update(
        {
            "rev9r3e": True,
            "logical_command_stream_sequence": True,
            "axes_sequence_stream":
                "flight_axes+release_axes",
            "discrete_sequence_stream":
                "arm+disarm",
            "cross_stream_stale_rejection": False,
            "within_stream_stale_rejection": True,
            "recovery_rebases_both_streams": True,
            "base_recovery": "rev9r3c",
            "rev9r3d_cycle_dispatch_used": False,
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
        f"OMNI Instructor Console DEV-018 rev9 rev3e: "
        f"http://{host}:{port}/",
        flush=True,
    )
    print(f"Core DB: {runtime.db_path}", flush=True)
    print(
        "Command anti-replay: axes/release and arm/disarm "
        "use separate epoch-scoped sequence streams.",
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
