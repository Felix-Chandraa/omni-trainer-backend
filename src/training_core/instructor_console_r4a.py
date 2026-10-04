from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import threading
import time
from types import MethodType
from typing import Any
from urllib.parse import urlsplit

from . import instructor_console_rev9r3f as r3f
from .review_service import (
    REVIEW_API_BASE,
    REVIEW_PROTOCOL,
    ReviewError,
    ReviewManager,
)


DEV018_R4A_FLIGHT_TAKEOVER_HANDBACK = True

INSTRUCTOR_ACTOR_ID = "dev018-instructor"
INSTRUCTOR_STATION_ID = "instructor-console-local"
HANDBACK_TIMEOUT_SEC = max(
    5.0,
    float(os.environ.get("OMNI_HANDBACK_TIMEOUT_SEC", "15.0")),
)


class InstructorRuntime(r3f.InstructorRuntime):
    """
    R4-A Flight authority transaction foundation.

    - Instructor TAKE CONTROL is immediate; Student approval is not required.
    - Transfer rotates Flight authority epoch and releases any active Student
      RC override before ownership changes.
    - HAND BACK creates a pending transaction only.
    - Student must explicitly ACCEPT CONTROL from its authenticated Feather WS.
    - Timeout/cancel/pause/interruption keeps Instructor ownership.
    - No Instructor Flight command source is added in R4-A; that is R4-B.
    """

    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        # DEV019_REVIEW_CANONICAL_BACKEND
        # Review is intentionally independent from live workers, MAVLink,
        # FlightCommandRouter, and authority state. It only receives the R2
        # metadata store for Ended-state validation and a separate audit sink.
        self.review = ReviewManager(
            base,
            self.store,
            reviewer_actor_id=INSTRUCTOR_ACTOR_ID,
            audit=self._audit,
        )
        self._r4a_pending: dict[str, Any] | None = None
        self._r4a_guard_installed = False
        self._r4a_stop = threading.Event()
        self._r4a_watch = threading.Thread(
            target=self._r4a_watch_loop,
            daemon=True,
            name="OmniR4AHandbackWatch",
        )
        self._r4a_watch.start()

    def _r4a_bridge_locked(self):
        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            raise RuntimeError("Flight control bridge/router missing")
        return bridge

    def _r4a_gateway_locked(self):
        bridge = self._r4a_bridge_locked()
        for value in vars(bridge).values():
            if (
                hasattr(value, "_clients")
                and hasattr(value, "_principals")
                and hasattr(value, "_loop")
            ):
                return value
        raise RuntimeError("Flight evidence/control gateway not found")

    def _r4a_principal_locked(self):
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        gateway = self._r4a_gateway_locked()
        principal = gateway._principals.get(self.ctx["attempt_id"])
        if principal is None:
            raise RuntimeError("Student Flight principal missing")
        return principal

    def _r4a_binding_locked(self):
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        router = self._r4a_bridge_locked().router
        binding = router._bindings.get(self.ctx["attempt_id"])
        if binding is None:
            raise RuntimeError("Flight control binding missing")
        return binding

    def _r4a_authority_payload_locked(self) -> dict[str, Any]:
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        bridge = self._r4a_bridge_locked()
        principal = self._r4a_principal_locked()
        lease = bridge.router.authority.current(self.ctx["attempt_id"])
        if lease is None:
            return {
                "type": "authority",
                "scope": "flight",
                "held": False,
                "owner_actor_id": None,
                "owner_station_id": None,
                "epoch": None,
                "generation": principal.assignment.generation,
                "aircraft_id": principal.assignment.aircraft_id,
                "attempt_id": principal.assignment.attempt_id,
                "deadman_ms": bridge.router.deadman_ns // 1_000_000,
                "commands": [
                    "flight_axes",
                    "release_axes",
                    "arm",
                    "disarm",
                ],
            }
        return {
            "type": "authority",
            "scope": "flight",
            "held": (
                lease.actor_id == principal.actor_id
                and lease.station_id == principal.station_id
            ),
            "owner_actor_id": lease.actor_id,
            "owner_station_id": lease.station_id,
            "epoch": lease.epoch,
            "generation": lease.generation,
            "aircraft_id": lease.aircraft_id,
            "attempt_id": lease.attempt_id,
            "deadman_ms": bridge.router.deadman_ns // 1_000_000,
            "commands": [
                "flight_axes",
                "release_axes",
                "arm",
                "disarm",
            ],
        }

    def _r4a_handback_payload_locked(self) -> dict[str, Any]:
        p = self._r4a_pending
        if not p:
            return {
                "type": "authority_handback",
                "scope": "flight",
                "pending": False,
                "handback_id": None,
                "expires_in_ms": 0,
            }
        remaining = max(0.0, p["deadline_mono"] - time.monotonic())
        return {
            "type": "authority_handback",
            "scope": "flight",
            "pending": True,
            "handback_id": p["id"],
            "expires_in_ms": int(remaining * 1000),
        }

    def _r4a_schedule_student_message_locked(
        self,
        payload: dict[str, Any],
    ) -> int:
        if not self.ctx:
            return 0
        gateway = self._r4a_gateway_locked()
        bridge = self._r4a_bridge_locked()
        loop = getattr(gateway, "_loop", None)
        if loop is None or not loop.is_running():
            return 0

        clients = []
        for ws, assignment in list(gateway._clients.items()):
            if getattr(assignment, "attempt_id", None) == self.ctx["attempt_id"]:
                clients.append(ws)

        sent = 0
        for ws in clients:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    bridge.router._send(ws, payload),
                    loop,
                )
                fut.result(timeout=1.5)
                sent += 1
            except Exception as exc:
                self._audit(
                    "r4a_student_notify_error",
                    {"error": str(exc), "type": payload.get("type")},
                )
        return sent

    def _r4a_owner_locked(self) -> dict[str, Any]:
        if not self.ctx:
            return {
                "kind": "none",
                "actor_id": None,
                "station_id": None,
                "epoch": None,
            }
        router = self._r4a_bridge_locked().router
        lease = router.authority.current(self.ctx["attempt_id"])
        if lease is None:
            return {
                "kind": "none",
                "actor_id": None,
                "station_id": None,
                "epoch": None,
            }

        principal = self._r4a_principal_locked()
        if (
            lease.actor_id == INSTRUCTOR_ACTOR_ID
            and lease.station_id == INSTRUCTOR_STATION_ID
        ):
            kind = "instructor"
        elif (
            lease.actor_id == principal.actor_id
            and lease.station_id == principal.station_id
        ):
            kind = "student"
        else:
            kind = "other"

        return {
            "kind": kind,
            "actor_id": lease.actor_id,
            "station_id": lease.station_id,
            "epoch": lease.epoch,
            "generation": lease.generation,
        }

    def _r4a_grant_instructor_locked(self, reason: str):
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        bridge = self._r4a_bridge_locked()
        binding = self._r4a_binding_locked()
        principal = self._r4a_principal_locked()
        attempt = self._attempt()
        if attempt is None:
            raise RuntimeError("Attempt missing")

        lease = bridge.router.authority.grant(
            attempt_id=attempt.id,
            aircraft_id=attempt.aircraft_id,
            actor_id=INSTRUCTOR_ACTOR_ID,
            station_id=INSTRUCTOR_STATION_ID,
            generation=attempt.generation,
            recorder=binding.recorder,
            reason=reason,
            force_new_epoch=True,
        )

        with bridge.router._lock:
            for key in list(bridge.router._last_sequence):
                if key[0] == attempt.id and int(key[2]) != int(lease.epoch):
                    bridge.router._last_sequence.pop(key, None)

        binding.recorder.record_event(
            "flight_authority_takeover",
            {
                "from_actor_id": principal.actor_id,
                "from_station_id": principal.station_id,
                "to_actor_id": INSTRUCTOR_ACTOR_ID,
                "to_station_id": INSTRUCTOR_STATION_ID,
                "epoch": lease.epoch,
                "reason": reason,
            },
            visibility="server",
        )
        return lease

    def _r4a_grant_student_locked(self, principal, reason: str):
        if not self.ctx:
            raise RuntimeError("no current Attempt")
        bridge = self._r4a_bridge_locked()
        binding = self._r4a_binding_locked()
        attempt = self._attempt()
        if attempt is None:
            raise RuntimeError("Attempt missing")

        if principal.assignment.attempt_id != attempt.id:
            raise RuntimeError("student_attempt_mismatch")
        if principal.station_id != self.ctx.get("student_station_id"):
            raise RuntimeError("student_station_mismatch")

        lease = bridge.router.authority.grant(
            attempt_id=attempt.id,
            aircraft_id=attempt.aircraft_id,
            actor_id=principal.actor_id,
            station_id=principal.station_id,
            generation=attempt.generation,
            recorder=binding.recorder,
            reason=reason,
            force_new_epoch=True,
        )

        with bridge.router._lock:
            for key in list(bridge.router._last_sequence):
                if key[0] == attempt.id and int(key[2]) != int(lease.epoch):
                    bridge.router._last_sequence.pop(key, None)

        binding.recorder.record_event(
            "flight_authority_handback_committed",
            {
                "to_actor_id": principal.actor_id,
                "to_station_id": principal.station_id,
                "epoch": lease.epoch,
                "reason": reason,
            },
            visibility="server",
        )
        return lease

    def _r4a_cancel_handback_locked(
        self,
        reason: str,
        *,
        notify: bool = True,
    ) -> bool:
        pending = self._r4a_pending
        if pending is None:
            return False

        self._r4a_pending = None
        try:
            binding = self._r4a_binding_locked()
            binding.recorder.record_event(
                "flight_handback_cancelled",
                {
                    "handback_id": pending["id"],
                    "reason": reason,
                },
                visibility="server",
            )
        except Exception:
            pass

        self._audit(
            "flight_handback_cancelled",
            {
                "attempt_id":
                    self.ctx.get("attempt_id") if self.ctx else None,
                "handback_id": pending["id"],
                "reason": reason,
            },
        )
        if notify:
            try:
                self._r4a_schedule_student_message_locked(
                    self._r4a_handback_payload_locked()
                )
            except Exception:
                pass
        return True

    def _r4a_watch_loop(self) -> None:
        while not self._r4a_stop.wait(0.2):
            try:
                with self.lock:
                    if (
                        self._r4a_pending is not None
                        and time.monotonic()
                        >= self._r4a_pending["deadline_mono"]
                    ):
                        self._r4a_cancel_handback_locked(
                            "timeout",
                            notify=True,
                        )
            except Exception as exc:
                try:
                    self._audit(
                        "r4a_handback_watch_error",
                        {"error": str(exc)},
                    )
                except Exception:
                    pass

    def takeover_flight(self) -> dict[str, Any]:
        with self.lock:
            current = self._attempt()
            if current is None:
                raise RuntimeError("no current Attempt")
            state = r3f.r3a.r3.r2.rev6._state_text(
                current.state
            ).lower()
            if state not in {"active", "paused"}:
                raise RuntimeError(
                    "Flight takeover requires Active or Paused Attempt"
                )

            owner = self._r4a_owner_locked()
            if owner["kind"] == "instructor":
                return self.status()
            if owner["kind"] != "student":
                raise RuntimeError(
                    f"unexpected Flight authority owner: {owner['kind']}"
                )

            self._r4a_cancel_handback_locked(
                "superseded_by_takeover",
                notify=False,
            )

            bridge = self._r4a_bridge_locked()
            bridge.router.release_attempt(
                current.id,
                reason="instructor_takeover",
            )
            lease = self._r4a_grant_instructor_locked(
                "instructor_takeover"
            )

            self._audit(
                "flight_authority_instructor_takeover",
                {
                    "attempt_id": current.id,
                    "state": state,
                    "epoch": lease.epoch,
                },
            )

            self._r4a_schedule_student_message_locked(
                self._r4a_authority_payload_locked()
            )
            self._r4a_schedule_student_message_locked(
                self._r4a_handback_payload_locked()
            )
            return self.status()

    def request_flight_handback(self) -> dict[str, Any]:
        with self.lock:
            current = self._attempt()
            if current is None:
                raise RuntimeError("no current Attempt")
            state = r3f.r3a.r3.r2.rev6._state_text(
                current.state
            ).lower()
            if state not in {"active", "paused"}:
                raise RuntimeError(
                    "Handback requires Active or Paused Attempt"
                )

            owner = self._r4a_owner_locked()
            if owner["kind"] != "instructor":
                raise RuntimeError(
                    "Instructor must hold Flight authority before handback"
                )

            if self._r4a_pending is not None:
                return self.status()

            station = self._student_station_status_locked()
            if not station.get("connected"):
                raise RuntimeError(
                    "Student station heartbeat is stale; handback blocked"
                )

            bridge = self._r4a_bridge_locked()
            connection = bridge.router.connection_snapshot()
            if not (
                connection.get("client_connected")
                or int(connection.get("client_count") or 0) > 0
            ):
                raise RuntimeError(
                    "Student Flight client is disconnected; handback blocked"
                )

            principal = self._r4a_principal_locked()
            if principal.station_id != station.get("station_id"):
                raise RuntimeError(
                    "Student Flight principal/station mismatch"
                )

            handback_id = "hb-" + secrets.token_hex(8)
            now = time.monotonic()
            self._r4a_pending = {
                "id": handback_id,
                "attempt_id": current.id,
                "station_id": principal.station_id,
                "actor_id": principal.actor_id,
                "created_mono": now,
                "deadline_mono": now + HANDBACK_TIMEOUT_SEC,
            }

            binding = self._r4a_binding_locked()
            binding.recorder.record_event(
                "flight_handback_pending",
                {
                    "handback_id": handback_id,
                    "to_actor_id": principal.actor_id,
                    "to_station_id": principal.station_id,
                    "timeout_s": HANDBACK_TIMEOUT_SEC,
                    "state": state,
                },
                visibility="server",
            )
            self._audit(
                "flight_handback_pending",
                {
                    "attempt_id": current.id,
                    "handback_id": handback_id,
                    "state": state,
                    "timeout_s": HANDBACK_TIMEOUT_SEC,
                },
            )

            self._r4a_schedule_student_message_locked(
                self._r4a_handback_payload_locked()
            )
            return self.status()

    def cancel_flight_handback(self) -> dict[str, Any]:
        with self.lock:
            self._r4a_cancel_handback_locked(
                "instructor_cancelled",
                notify=True,
            )
            return self.status()

    async def _r4a_accept_control_ws(
        self,
        ws,
        principal,
        incoming: dict[str, Any],
    ) -> None:
        handback_id = incoming.get("handback_id")
        result_payload = None
        authority_payload = None
        handback_payload = None
        peers = []

        try:
            with self.lock:
                current = self._attempt()
                if current is None:
                    raise RuntimeError("no_current_attempt")
                state = r3f.r3a.r3.r2.rev6._state_text(
                    current.state
                ).lower()
                if state not in {"active", "paused"}:
                    raise RuntimeError(
                        "accept_control_requires_active_or_paused"
                    )

                pending = self._r4a_pending
                if pending is None:
                    raise RuntimeError("no_pending_handback")
                if not isinstance(handback_id, str):
                    raise RuntimeError("handback_id_missing")
                if handback_id != pending["id"]:
                    raise RuntimeError("handback_id_mismatch")
                if time.monotonic() >= pending["deadline_mono"]:
                    self._r4a_cancel_handback_locked(
                        "timeout",
                        notify=False,
                    )
                    raise RuntimeError("handback_expired")

                if principal.assignment.attempt_id != current.id:
                    raise RuntimeError("student_attempt_mismatch")
                if principal.actor_id != pending["actor_id"]:
                    raise RuntimeError("student_actor_mismatch")
                if principal.station_id != pending["station_id"]:
                    raise RuntimeError("student_station_mismatch")

                station = self._student_station_status_locked()
                if (
                    not station.get("connected")
                    or station.get("station_id")
                    != principal.station_id
                ):
                    raise RuntimeError(
                        "student_readiness_continuity_missing"
                    )

                owner = self._r4a_owner_locked()
                if owner["kind"] != "instructor":
                    raise RuntimeError(
                        "instructor_no_longer_holds_flight_authority"
                    )

                lease = self._r4a_grant_student_locked(
                    principal,
                    "student_explicit_accept_control",
                )
                accepted_id = pending["id"]
                self._r4a_pending = None

                self._audit(
                    "flight_handback_student_accepted",
                    {
                        "attempt_id": current.id,
                        "handback_id": accepted_id,
                        "epoch": lease.epoch,
                        "state": state,
                    },
                )

                authority_payload = self._r4a_authority_payload_locked()
                handback_payload = self._r4a_handback_payload_locked()
                result_payload = {
                    "type": "authority_handback_result",
                    "scope": "flight",
                    "status": "applied",
                    "handback_id": accepted_id,
                    "epoch": lease.epoch,
                    "state": state,
                }

                gateway = self._r4a_gateway_locked()
                peers = [
                    peer
                    for peer, assignment in list(gateway._clients.items())
                    if getattr(assignment, "attempt_id", None)
                    == current.id
                ]

        except Exception as exc:
            result_payload = {
                "type": "authority_handback_result",
                "scope": "flight",
                "status": "rejected",
                "handback_id": (
                    handback_id if isinstance(handback_id, str) else None
                ),
                "reason": str(exc),
            }
            peers = [ws]

        bridge = self._control_bridge()
        if bridge is None or bridge.router is None:
            return

        if authority_payload is not None:
            for peer in peers:
                try:
                    await bridge.router._send(peer, authority_payload)
                except Exception:
                    pass
        if handback_payload is not None:
            for peer in peers:
                try:
                    await bridge.router._send(peer, handback_payload)
                except Exception:
                    pass
        if result_payload is not None:
            try:
                await bridge.router._send(ws, result_payload)
            except Exception:
                pass

    def _r4a_install_router_hooks_locked(self) -> None:
        bridge = self._r4a_bridge_locked()
        router = bridge.router
        if getattr(router, "_dev018_r4a_hooks", False):
            self._r4a_guard_installed = True
            return

        runtime = self
        original_handle = router.handle

        async def r4a_handle(this, ws, principal, incoming):
            if (
                incoming.get("type") == "cmd"
                and incoming.get("name") == "accept_control"
            ):
                await runtime._r4a_accept_control_ws(
                    ws,
                    principal,
                    incoming,
                )
                return
            await original_handle(ws, principal, incoming)

        async def r4a_on_connect(this, ws, principal):
            attempt_id = principal.assignment.attempt_id
            attempt = runtime.store.attempt(attempt_id)
            state = r3f.r3a.r3.r2.rev6._state_text(
                attempt.state
            ).lower()

            with runtime.lock:
                lease = this.authority.current(attempt_id)
                if (
                    state in {"active", "interrupted", "paused"}
                    and lease is not None
                    and lease.actor_id == principal.actor_id
                    and lease.station_id == principal.station_id
                ):
                    binding = this._bindings[attempt_id]
                    new_lease = this.authority.grant(
                        attempt_id=attempt_id,
                        aircraft_id=principal.assignment.aircraft_id,
                        actor_id=principal.actor_id,
                        station_id=principal.station_id,
                        generation=principal.assignment.generation,
                        recorder=binding.recorder,
                        reason="r4a_student_transport_reconnect",
                        force_new_epoch=True,
                    )
                    with this._lock:
                        for key in list(this._last_sequence):
                            if (
                                key[0] == attempt_id
                                and key[1] == principal.station_id
                                and int(key[2]) != int(new_lease.epoch)
                            ):
                                this._last_sequence.pop(key, None)

            await type(this).on_connect(this, ws, principal)

            with runtime.lock:
                pending_payload = (
                    runtime._r4a_handback_payload_locked()
                    if runtime._r4a_pending is not None
                    else None
                )
            if pending_payload is not None:
                await this._send(ws, pending_payload)

        router.handle = MethodType(r4a_handle, router)
        router.on_connect = MethodType(r4a_on_connect, router)
        router._dev018_r4a_hooks = True
        self._r4a_guard_installed = True
        self._audit(
            "r4a_router_hooks_installed",
            {
                "attempt_id":
                    self.ctx.get("attempt_id") if self.ctx else None,
            },
        )

    def prepare(self, timeout: float = 60.0) -> dict[str, Any]:
        super().prepare(timeout)
        with self.lock:
            self._r4a_gateway_locked()
            self._r4a_install_router_hooks_locked()
            return self.status()

    def pause(self) -> dict[str, Any]:
        with self.lock:
            self._r4a_cancel_handback_locked(
                "attempt_paused",
                notify=True,
            )
        return super().pause()

    def _interrupt_current(
        self,
        *,
        reason: str,
        detail: dict[str, Any],
    ) -> bool:
        with self.lock:
            self._r4a_cancel_handback_locked(
                "attempt_interrupted",
                notify=True,
            )
        return super()._interrupt_current(
            reason=reason,
            detail=detail,
        )

    def _cleanup_runtime(self) -> None:
        with self.lock:
            self._r4a_cancel_handback_locked(
                "runtime_cleanup",
                notify=False,
            )
        super()._cleanup_runtime()

    def shutdown(self) -> None:
        self._r4a_stop.set()
        self.review.clear()
        super().shutdown()

    def review_request(
        self,
        operation: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        actions = {
            "list": self.review.list,
            "open": self.review.open,
            "snapshot": self.review.snapshot,
            "play": self.review.play,
            "pause": self.review.pause,
            "speed": self.review.speed,
            "seek": self.review.seek,
            "step": self.review.step,
            "event-jump": self.review.event_jump,
            "close": self.review.close,
        }
        action = actions.get(operation)
        if action is None:
            raise ReviewError(
                "INVALID_REQUEST",
                f"unknown Review operation: {operation}",
                http_status=404,
            )
        return action(body)

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["dev"] = "DEV-018-R4A+DEV-019-Review"
        out.setdefault("capabilities", {})["review"] = {
            "available": True,
            "protocol": REVIEW_PROTOCOL,
            "api_base": REVIEW_API_BASE,
            "read_only": True,
            "stored_state_only": True,
            "fdm_rerun": False,
            "live_authority_isolated": True,
        }
        current = out.get("current")
        if current is None:
            return out

        try:
            owner = self._r4a_owner_locked()
        except Exception:
            owner = {
                "kind": "unknown",
                "actor_id": None,
                "station_id": None,
                "epoch": None,
            }

        state = str(current.get("state") or "").lower()
        station = self._student_station_status_locked()
        bridge = self._control_bridge()
        control_connected = False
        if bridge is not None and bridge.router is not None:
            try:
                c = bridge.router.connection_snapshot()
                control_connected = bool(
                    c.get("client_connected")
                    or int(c.get("client_count") or 0) > 0
                )
            except Exception:
                pass

        pending = None
        if self._r4a_pending is not None:
            remaining = max(
                0.0,
                self._r4a_pending["deadline_mono"]
                - time.monotonic(),
            )
            pending = {
                "id": self._r4a_pending["id"],
                "station_id": self._r4a_pending["station_id"],
                "actor_id": self._r4a_pending["actor_id"],
                "expires_in_s": remaining,
            }

        current["flight_authority_transfer"] = {
            "owner": owner,
            "pending_handback": pending,
            "handback_timeout_s": HANDBACK_TIMEOUT_SEC,
            "student_connected": bool(station.get("connected")),
            "student_control_connected": control_connected,
            "can_takeover": (
                state in {"active", "paused"}
                and owner.get("kind") == "student"
            ),
            "can_request_handback": (
                state in {"active", "paused"}
                and owner.get("kind") == "instructor"
                and pending is None
                and bool(station.get("connected"))
                and control_connected
            ),
            "can_cancel_handback": pending is not None,
            "instructor_command_path_wired": False,
            "student_accept_required": True,
        }
        return out


class Handler(r3f.r3a.r3.r2.Handler):
    def _r4a_localhost(self) -> bool:
        try:
            return ipaddress.ip_address(
                self.client_address[0]
            ).is_loopback
        except ValueError:
            return False

    def _r4a_json(
        self,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        raw = json.dumps(
            payload,
            separators=(",", ":"),
            default=str,
        ).encode()
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _r4a_review_static(self, path: str) -> bool:
        # DEV019_REVIEW_STATIC_ALLOWLIST
        # The legacy Instructor handler intentionally serves almost no static
        # files. Review adds only these exact localhost-only assets; there is
        # no generic filesystem route and no legacy Feather bus exposure.
        allowed = {
            "/review_ui.js": ("review_ui.js", "application/javascript; charset=utf-8"),
            "/review_renderer.js": ("review_renderer.js", "application/javascript; charset=utf-8"),
            "/review_assets/omni_plane.glb": ("review_assets/omni_plane.glb", "model/gltf-binary"),
        }
        item = allowed.get(path)
        feather_prefix = "/review_feather/"
        if item is None and path.startswith(feather_prefix):
            if not self._r4a_localhost():
                self._r4a_json(403, {"ok": False, "error": "instructor_api_loopback_only"})
                return True
            rel = path[len(feather_prefix):]
            parts = Path(rel).parts
            if (not rel or rel.startswith("/") or ".." in parts or any(part.startswith(".") for part in parts)):
                self.send_error(404)
                return True
            allowed_types = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".svg": "image/svg+xml",
                ".webp": "image/webp",
                ".wasm": "application/wasm",
                ".bin": "application/octet-stream",
                ".ktx2": "image/ktx2",
                ".xml": "application/xml; charset=utf-8",
                ".glb": "model/gltf-binary",
            }
            root = (self.web_root / "review_feather").resolve()
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self.send_error(404)
                return True
            content_type = allowed_types.get(target.suffix.lower())
            if content_type is None or not target.is_file():
                self.send_error(404)
                return True
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            with target.open("rb") as fp:
                shutil.copyfileobj(fp, self.wfile, length=1024 * 1024)
            return True
        if item is None:
            return False
        if not self._r4a_localhost():
            self._r4a_json(
                403,
                {"ok": False, "error": "instructor_api_loopback_only"},
            )
            return True
        relative, content_type = item
        root = self.web_root.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self.send_error(404)
            return True
        if not target.is_file():
            self.send_error(404)
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as fp:
            shutil.copyfileobj(fp, self.wfile, length=1024 * 1024)
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if self._r4a_review_static(path):
            return
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path

        # DEV019_REVIEW_CANONICAL_API
        prefix = REVIEW_API_BASE + "/"
        if path.startswith(prefix):
            if not self._r4a_localhost():
                return self._r4a_json(
                    403,
                    {
                        "ok": False,
                        "protocol": REVIEW_PROTOCOL,
                        "request_id": None,
                        "error": {
                            "code": "UNAUTHORIZED_REVIEW",
                            "message": "instructor_api_loopback_only",
                        },
                    },
                )
            body: dict[str, Any] = {}
            try:
                body = self._body()
                operation = path[len(prefix):]
                return self._r4a_json(
                    200,
                    self.runtime.review_request(operation, body),
                )
            except ReviewError as exc:
                return self._r4a_json(
                    exc.http_status,
                    {
                        "ok": False,
                        "protocol": REVIEW_PROTOCOL,
                        "request_id": body.get("request_id"),
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                        },
                    },
                )
            except (ValueError, json.JSONDecodeError) as exc:
                return self._r4a_json(
                    400,
                    {
                        "ok": False,
                        "protocol": REVIEW_PROTOCOL,
                        "request_id": body.get("request_id"),
                        "error": {
                            "code": "INVALID_REQUEST",
                            "message": str(exc),
                        },
                    },
                )

        actions = {
            "/api/authority/flight/takeover":
                self.runtime.takeover_flight,
            "/api/authority/flight/handback":
                self.runtime.request_flight_handback,
            "/api/authority/flight/cancel-handback":
                self.runtime.cancel_flight_handback,
        }
        if path not in actions:
            return super().do_POST()

        if not self._r4a_localhost():
            return self._r4a_json(
                403,
                {
                    "ok": False,
                    "error": "instructor_api_loopback_only",
                },
            )

        try:
            return self._r4a_json(200, actions[path]())
        except Exception as exc:
            return self._r4a_json(
                409,
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )


def probe(base: Path, source: Path) -> dict[str, Any]:
    out = r3f.probe(base, source)
    out.update(
        {
            "r4a": True,
            "flight_takeover": True,
            "explicit_student_handback_accept": True,
            "takeover_rotates_epoch": True,
            "handback_rotates_epoch": True,
            "pending_handback_cancel_on_pause": True,
            "pending_handback_cancel_on_interrupt": True,
            "student_reconnect_cannot_steal_instructor_authority":
                True,
            "instructor_command_path_wired": False,
            "payload_authority_wired": False,
            "dev019_review": True,
            "review_protocol": REVIEW_PROTOCOL,
            "review_api_base": REVIEW_API_BASE,
            "review_stored_state_only": True,
            "review_live_authority_isolated": True,
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
    Handler.runtime = runtime
    Handler.web_root = web_root
    server = r3f.r3a.r3.r2.rev6.ThreadingHTTPServer(
        (host, port),
        Handler,
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
        f"OMNI Instructor Console DEV-018 R4-A: "
        f"http://{host}:{port}/",
        flush=True,
    )
    print(
        "R4-A: Instructor takeover + explicit Student handback "
        "(authority transaction only; Instructor input is R4-B).",
        flush=True,
    )
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
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--web-root")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.base).expanduser().resolve()
    source = Path(args.source).expanduser().resolve()

    if args.probe:
        result = probe(base, source)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 70

    web_root = (
        Path(args.web_root).expanduser().resolve()
        if args.web_root
        else base / "web" / "instructor_console_v1"
    )
    return serve(
        base,
        source,
        args.host,
        args.port,
        web_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
