"""DEV-014 evidence-capable LAN gateway.

Builds on DEV-013 but promotes telemetry_received into a dedicated
client/received_state evidence channel while preserving the audit event.
Aircraft commands remain disabled.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
import time

from .evidence_recorder import AttemptEvidenceRecorder
from .lan_protocol import (
    Assignment,
    HELLO_TIMEOUT_SEC,
    PROTOCOL,
    ProtocolError,
    LanTelemetryGateway,
    parse_hello,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DEV015_COMMAND_ROUTER = True
ALLOWED_ROLES = {"FLIGHT", "PAYLOAD", "OBSERVER"}
ALLOWED_SCOPES = {"flight", "payload", "ui", "observation", "connection"}


@dataclass(frozen=True)
class EvidencePrincipal:
    assignment: Assignment
    recorder: AttemptEvidenceRecorder
    station_id: str
    training_role: str

    @property
    def actor_id(self) -> str:
        return self.assignment.student_id

    @property
    def default_scope(self) -> str:
        return {
            "FLIGHT": "flight",
            "PAYLOAD": "payload",
        }.get(self.training_role, "observation")


@dataclass
class ClockPing:
    client_send_mono_ns: int
    server_recv_mono_ns: int
    server_send_mono_ns: int
    server_recv_utc_ns: int


class EvidenceLanTelemetryGateway(LanTelemetryGateway):
    """DEV-011 telemetry + authenticated, evidence-only client ingress."""

    def __init__(self, host: str = "0.0.0.0", port: int = 9100):
        super().__init__(host, port)
        self._principals: dict[str, EvidencePrincipal] = {}
        self._command_router = None

    def set_command_router(self, router) -> None:
        self._command_router = router

    def register_evidence_principal(
        self,
        assignment: Assignment,
        recorder: AttemptEvidenceRecorder,
        *,
        station_id: str,
        training_role: str,
    ) -> EvidencePrincipal:
        role = training_role.upper()
        if not SAFE_ID.match(station_id):
            raise ProtocolError("invalid_station_id")
        if role not in ALLOWED_ROLES:
            raise ProtocolError("invalid_training_role")
        if assignment.attempt_id in self._principals:
            raise ProtocolError("duplicate_attempt_principal")
        principal = EvidencePrincipal(
            assignment=assignment,
            recorder=recorder,
            station_id=station_id,
            training_role=role,
        )
        self._principals[assignment.attempt_id] = principal
        return principal

    async def _handler(self, ws) -> None:
        assignment = None
        principal = None
        pending: dict[str, ClockPing] = {}
        try:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=HELLO_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                await self._send_error(ws, "hello_timeout")
                return

            try:
                msg = parse_hello(raw)
                assignment = self._authenticate(
                    msg["student_id"], msg["token"]
                )
                principal = self._principals.get(assignment.attempt_id)
                if principal is None:
                    raise ProtocolError("evidence_principal_missing")
                if msg.get("station_id") not in (
                    None,
                    "",
                    principal.station_id,
                ):
                    raise ProtocolError("station_assignment_mismatch")
                requested_role = msg.get("training_role")
                if (
                    requested_role not in (None, "")
                    and str(requested_role).upper()
                    != principal.training_role
                ):
                    raise ProtocolError("role_assignment_mismatch")
            except ProtocolError as exc:
                await self._send_error(ws, str(exc))
                return

            self._clients[ws] = assignment
            principal.recorder.record_client_event(
                station_id=principal.station_id,
                actor_id=principal.actor_id,
                role=principal.training_role,
                scope="connection",
                action="connected",
                data={"source": "server_gateway"},
                client_mono_ns=None,
                source="server_gateway",
            )

            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "protocol": PROTOCOL,
                        "server_time_unix": time.time(),
                        "assignment": assignment.public(),
                        "evidence_principal": {
                            "actor_id": principal.actor_id,
                            "station_id": principal.station_id,
                            "training_role": principal.training_role,
                        },
                        "capabilities": {
                            "telemetry": True,
                            "commands": False,
                            "cesium_pose": True,
                            "client_evidence": True,
                            "clock_sync": True,
                            "received_state_evidence": True,
                        },
                    },
                    separators=(",", ":"),
                )
            )

            if self._command_router is not None:
                await self._command_router.on_connect(ws, principal)

            latest = self._latest.get(assignment.attempt_id)
            if latest is not None:
                await ws.send(
                    json.dumps(latest, separators=(",", ":"))
                )

            async for raw_in in ws:
                try:
                    incoming = json.loads(raw_in)
                except Exception:
                    await self._send_error(ws, "invalid_json")
                    continue
                if not isinstance(incoming, dict):
                    await self._send_error(ws, "message_not_object")
                    continue

                kind = incoming.get("type")

                if kind == "cmd":
                    if self._command_router is None:
                        await self._send_error(
                            ws, "commands_disabled_dev014"
                        )
                    else:
                        await self._command_router.handle(
                            ws, principal, incoming
                        )
                    continue

                if kind == "clock_ping":
                    ping_id = incoming.get("ping_id")
                    client_send = incoming.get(
                        "client_send_mono_ns"
                    )
                    if (
                        not isinstance(ping_id, str)
                        or not SAFE_ID.match(ping_id)
                        or not isinstance(client_send, int)
                    ):
                        await self._send_error(
                            ws, "invalid_clock_ping"
                        )
                        continue
                    recv_mono = time.monotonic_ns()
                    recv_utc = time.time_ns()
                    send_mono = time.monotonic_ns()
                    pending[ping_id] = ClockPing(
                        client_send_mono_ns=client_send,
                        server_recv_mono_ns=recv_mono,
                        server_send_mono_ns=send_mono,
                        server_recv_utc_ns=recv_utc,
                    )
                    if len(pending) > 16:
                        for old in list(pending)[:-16]:
                            pending.pop(old, None)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "clock_pong",
                                "protocol": PROTOCOL,
                                "ping_id": ping_id,
                                "client_send_mono_ns": client_send,
                                "server_recv_mono_ns": recv_mono,
                                "server_send_mono_ns": send_mono,
                                "server_recv_utc_ns": recv_utc,
                            },
                            separators=(",", ":"),
                        )
                    )
                    continue

                if kind == "clock_sample":
                    ping_id = incoming.get("ping_id")
                    client_recv = incoming.get(
                        "client_recv_mono_ns"
                    )
                    sample = pending.pop(str(ping_id), None)
                    if (
                        sample is None
                        or not isinstance(client_recv, int)
                    ):
                        await self._send_error(
                            ws, "unknown_clock_sample"
                        )
                        continue
                    principal.recorder.register_client_clock_sample(
                        station_id=principal.station_id,
                        client_send_mono_ns=sample.client_send_mono_ns,
                        server_recv_mono_ns=sample.server_recv_mono_ns,
                        server_send_mono_ns=sample.server_send_mono_ns,
                        client_recv_mono_ns=client_recv,
                        server_recv_utc_ns=sample.server_recv_utc_ns,
                    )
                    continue

                if kind != "client_event":
                    await self._send_error(
                        ws, "client_messages_read_only"
                    )
                    continue

                action = incoming.get("action")
                data = incoming.get("data", {})
                client_mono = incoming.get("client_mono_ns")
                client_utc_ms = incoming.get("client_utc_ms")
                requested_scope = incoming.get("scope")

                if (
                    not isinstance(action, str)
                    or not action
                    or len(action) > 64
                    or not re.match(
                        r"^[A-Za-z0-9._:-]+$", action
                    )
                ):
                    await self._send_error(
                        ws, "invalid_client_action"
                    )
                    continue
                if not isinstance(data, dict):
                    await self._send_error(
                        ws, "invalid_client_event_data"
                    )
                    continue
                if (
                    client_mono is not None
                    and not isinstance(client_mono, int)
                ):
                    await self._send_error(
                        ws, "invalid_client_time"
                    )
                    continue

                client_utc_ns = (
                    int(client_utc_ms) * 1_000_000
                    if isinstance(client_utc_ms, int)
                    else None
                )

                if action == "telemetry_received":
                    scope = "observation"
                    source_seq = data.get("seq")
                    state = {
                        key: data.get(key)
                        for key in (
                            "lat",
                            "lon",
                            "alt_msl",
                            "roll",
                            "pitch",
                            "yaw",
                        )
                        if key in data
                    }
                    principal.recorder.record_client_received_state(
                        station_id=principal.station_id,
                        actor_id=principal.actor_id,
                        state=state,
                        source_sequence=(
                            source_seq
                            if isinstance(source_seq, int)
                            else None
                        ),
                        client_mono_ns=client_mono,
                        client_utc_ns=client_utc_ns,
                    )
                elif action == "command_intent":
                    # DEV016_COMMAND_INTENT
                    # Non-authoritative client intent. actor/station remain
                    # server-owned via authenticated principal.
                    source_seq = data.get("sequence")
                    principal.recorder.record_client_input_sent(
                        station_id=principal.station_id,
                        actor_id=principal.actor_id,
                        state=dict(data),
                        source_sequence=(
                            source_seq if isinstance(source_seq, int) else None
                        ),
                        client_mono_ns=client_mono,
                        client_utc_ns=client_utc_ns,
                    )
                    scope = principal.default_scope
                elif (
                    action.startswith("ui_")
                    or action
                    in {
                        "key_down",
                        "visibility_changed",
                        "view_changed",
                    }
                ):
                    scope = "ui"
                else:
                    scope = principal.default_scope

                if requested_scope is not None:
                    if requested_scope not in ALLOWED_SCOPES:
                        await self._send_error(
                            ws, "invalid_scope"
                        )
                        continue
                    if (
                        requested_scope in {"flight", "payload"}
                        and requested_scope
                        != principal.default_scope
                    ):
                        await self._send_error(
                            ws, "scope_assignment_mismatch"
                        )
                        continue
                    scope = requested_scope

                principal.recorder.record_client_event(
                    station_id=principal.station_id,
                    actor_id=principal.actor_id,
                    role=principal.training_role,
                    scope=scope,
                    action=action,
                    data=data,
                    client_mono_ns=client_mono,
                    client_utc_ns=client_utc_ns,
                    source="authenticated_client",
                )
        except Exception:
            pass
        finally:
            self._clients.pop(ws, None)
            if principal is not None and self._command_router is not None:
                try:
                    await self._command_router.on_disconnect(principal)
                except Exception:
                    pass
            if principal is not None:
                try:
                    principal.recorder.record_client_event(
                        station_id=principal.station_id,
                        actor_id=principal.actor_id,
                        role=principal.training_role,
                        scope="connection",
                        action="disconnected",
                        data={"source": "server_gateway"},
                        client_mono_ns=None,
                        source="server_gateway",
                    )
                except Exception:
                    pass
