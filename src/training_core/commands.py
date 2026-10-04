"""Fail-closed command admission; never treats acceptance as aircraft application."""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass

from .models import Actor, Role, Scope, State
from .store import Store


@dataclass(frozen=True)
class Command:
    id: str
    attempt_id: str
    aircraft_id: str
    actor: Actor
    scope: Scope
    generation: int
    authority_epoch: int
    expected_revision: int
    expires_utc: float
    name: str
    payload: dict


@dataclass(frozen=True)
class CommandResult:
    status: str
    reason: str


class CommandValidator:
    """Stores accepted/rejected decisions. Adapter dispatch is deliberately absent.

    Caller identity must come from authenticated gateway, NOT from JSON supplied
    by a remote client. No capability/release/physical readiness is asserted.
    This engineering-only validator rejects all live dispatch at integration
    boundary until production auth, capability and adapter wiring is complete.
    """

    def __init__(self, store: Store):
        self.store = store

    def validate(self, command: Command, *, now_utc: float | None = None) -> CommandResult:
        now = time.time() if now_utc is None else now_utc
        fingerprint = hashlib.sha256(json.dumps({
            "attempt_id": command.attempt_id, "aircraft_id": command.aircraft_id,
            "actor_id": command.actor.id, "actor_role": command.actor.role.value,
            "scope": command.scope.value, "generation": command.generation,
            "epoch": command.authority_epoch, "revision": command.expected_revision,
            "expires_utc": command.expires_utc, "name": command.name, "payload": command.payload,
        }, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM commands WHERE command_id=?", (command.id,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    return CommandResult("rejected", "command ID reused with different content")
                return CommandResult(existing["result"], existing["reason"])

            row = db.execute("""SELECT a.*,s.student_id FROM attempts a
                 JOIN exercises e ON a.exercise_id=e.id
                 JOIN sessions s ON e.session_id=s.id WHERE a.id=?""", (command.attempt_id,)).fetchone()
            if row is None:
                return CommandResult("rejected", "unknown attempt")
            reason = self._reject_reason(row, command, now)
            result = CommandResult("rejected", reason) if reason else CommandResult("accepted", "admission only; NOT applied")
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)",
                       (command.id, command.attempt_id, fingerprint, result.status, result.reason, now))
            self.store.event(db, command.attempt_id, "command.admission", command_id=command.id,
                             status=result.status, reason=result.reason, command_name=command.name,
                             scope=command.scope.value, actor=command.actor.id)
            return result

    @staticmethod
    def _reject_reason(row, command: Command, now: float) -> str | None:
        if row is None:
            return "unknown attempt"
        if command.actor.role != Role.STUDENT or command.actor.id != row["student_id"]:
            return "no authenticated student assignment for this engineering policy"
        if State(row["state"]) != State.ACTIVE:
            return "attempt not Active"
        if command.aircraft_id != row["aircraft_id"] or command.generation != row["generation"]:
            return "stale generation or wrong aircraft"
        if command.authority_epoch != row["authority_epoch"]:
            return "stale authority epoch"
        if command.expected_revision != row["revision"]:
            return "stale attempt revision"
        if not math.isfinite(command.expires_utc) or command.expires_utc <= now:
            return "expired command"
        if not command.id.strip() or not command.name.strip():
            return "missing command identity/type"
        if command.scope == Scope.FLIGHT and command.actor.id != row["flight_owner"]:
            return "flight authority required"
        if command.scope == Scope.PAYLOAD and command.actor.id != row["payload_owner"]:
            return "payload authority required"
        if command.scope == Scope.BOTH and (command.actor.id != row["flight_owner"] or command.actor.id != row["payload_owner"]):
            return "both authorities required"
        # Demonstration probes only: no command is forwarded to an aircraft.
        if command.name == "flight_probe" and command.scope == Scope.FLIGHT and command.payload == {}:
            return None
        if command.name == "payload_probe" and command.scope == Scope.PAYLOAD and command.payload == {}:
            return None
        if command.name == "both_probe" and command.scope == Scope.BOTH and command.payload == {}:
            return None
        return "command not in engineering probe allowlist"
