"""Supervised 1+1 session lifecycle; pure Python and durable SQLite state.

R2 engineering slice only. Real identity, device readiness, and authoritative
simulation hold require later adapters/approvals; never expose directly to LAN.
"""
from __future__ import annotations

import time
import uuid

from .models import Actor, Attempt, CoreError, Role, State
from .store import Store


_ALLOWED = {
    State.SETUP: {State.READY, State.ENDED},
    State.READY: {State.SETUP, State.ACTIVE, State.ENDED},
    State.ACTIVE: {State.PAUSED, State.INTERRUPTED, State.ENDED},
    State.PAUSED: {State.ACTIVE, State.INTERRUPTED, State.ENDED},
    State.INTERRUPTED: {State.PAUSED, State.ENDED},
    State.ENDED: set(),
}


def _id() -> str:
    return str(uuid.uuid4())


class SessionManager:
    def __init__(self, store: Store):
        self.store = store

    def create_session(self, actor: Actor, student_id: str) -> str:
        if actor.role != Role.INSTRUCTOR or not actor.id or not student_id or actor.id == student_id:
            raise CoreError("individual instructor and assigned student are required")
        sid = _id()
        with self.store.transaction() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (sid, actor.id, student_id, time.time()))
            self.store.event(db, None, "session.created", session_id=sid, instructor_id=actor.id,
                             student_id=student_id)
        return sid

    def create_exercise(self, actor: Actor, session_id: str, scenario_version: str) -> str:
        if not scenario_version.strip():
            raise CoreError("scenario version is required")
        eid = _id()
        with self.store.transaction() as db:
            self._require_instructor(db, actor, session_id=session_id)
            db.execute("INSERT INTO exercises VALUES(?,?,?,?)", (eid, session_id, scenario_version, time.time()))
            self.store.event(db, None, "exercise.created", session_id=session_id,
                             exercise_id=eid, scenario_version=scenario_version)
        return eid

    def create_attempt(self, actor: Actor, exercise_id: str, aircraft_id: str) -> Attempt:
        if not aircraft_id.strip():
            raise CoreError("aircraft ID required")
        aid = _id()
        with self.store.transaction() as db:
            ids = db.execute("SELECT s.id AS sid,s.student_id FROM exercises e JOIN sessions s ON e.session_id=s.id WHERE e.id=?",
                             (exercise_id,)).fetchone()
            if ids is None:
                raise CoreError("unknown exercise")
            self._require_instructor(db, actor, session_id=ids["sid"])
            prior = db.execute("SELECT MAX(generation) AS n FROM attempts WHERE exercise_id=?",
                               (exercise_id,)).fetchone()["n"] or 0
            if db.execute("SELECT 1 FROM attempts WHERE exercise_id=? AND state!='Ended'", (exercise_id,)).fetchone():
                raise CoreError("previous attempt must end before restart")
            now = time.time()
            db.execute("""INSERT INTO attempts
                (id,exercise_id,aircraft_id,generation,state,flight_owner,payload_owner,
                 authority_epoch,revision,created_utc,updated_utc)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (aid, exercise_id, aircraft_id, prior + 1, State.SETUP.value,
                        ids["student_id"], ids["student_id"], 1, 0, now, now))
            self.store.event(db, aid, "attempt.created", generation=prior + 1,
                             aircraft_id=aircraft_id)
        return self.store.attempt(aid)

    def transition(self, actor: Actor, attempt_id: str, target: State, *,
                   expected_revision: int, reason: str, readiness_verified: bool = False,
                   continuity_verified: bool = False) -> Attempt:
        """State bookkeeping ONLY; does not pause physics, verify devices or resume SITL.

        readiness_verified / continuity_verified MUST be provided by a trusted
        backend verifier. They are NOT evidence from the UI or student.
        """
        if not isinstance(target, State):
            raise CoreError("unknown state")
        if not reason.strip():
            raise CoreError("transition reason is required")
        with self.store.transaction() as db:
            row = db.execute("""SELECT a.*,s.id AS sid FROM attempts a
                    JOIN exercises e ON a.exercise_id=e.id JOIN sessions s ON e.session_id=s.id
                    WHERE a.id=?""", (attempt_id,)).fetchone()
            if row is None:
                raise CoreError("unknown attempt")
            self._require_instructor(db, actor, session_id=row["sid"])
            current = State(row["state"])
            if expected_revision != row["revision"]:
                raise CoreError("stale state revision")
            if target not in _ALLOWED[current]:
                raise CoreError(f"invalid transition: {current.value} -> {target.value}")
            if target == State.READY and not readiness_verified:
                raise CoreError("trusted readiness evidence missing")
            if current == State.INTERRUPTED and target == State.PAUSED and not continuity_verified:
                raise CoreError("continuity not verified; end and create new attempt")
            # A real coordinator must hold/resume authoritative aircraft BEFORE
            # claiming a pause/resume transition. Not connected in this slice.
            db.execute("UPDATE attempts SET state=?,revision=revision+1,updated_utc=?,ended_reason=? WHERE id=?",
                       (target.value, time.time(), reason if target == State.ENDED else None, attempt_id))
            self.store.event(db, attempt_id, "attempt.state_changed", previous=current.value,
                             target=target.value, actor=actor.id, reason=reason,
                             revision=row["revision"] + 1)
        return self.store.attempt(attempt_id)

    @staticmethod
    def _require_instructor(db, actor: Actor, *, session_id: str):
        row = db.execute("SELECT instructor_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None or actor.role != Role.INSTRUCTOR or actor.id != row["instructor_id"]:
            raise CoreError("instructor not assigned to session")
