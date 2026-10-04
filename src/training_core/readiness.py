"""Trusted R2 readiness gate between an Attempt and its owned AircraftWorker.

This is an engineering readiness contract, not full training acceptance.
It proves only:
- exact Attempt / aircraft / generation / worker PID identity;
- owned worker is still running and healthy;
- a recent valid raw FGNetFDM observation exists;
- a recent receive-only MAVLink HEARTBEAT exists.

Only after those checks may trusted backend code ask SessionManager to perform
Setup -> Ready with readiness_verified=True.

No UI/client supplied boolean is accepted as evidence here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import time

from .models import Actor, CoreError, State
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator, WorkerHealth


@dataclass(frozen=True)
class ReadinessEvidence:
    attempt_id: str
    aircraft_id: str
    generation: int
    worker_pid: int

    fg_seen_monotonic: float
    fg_rx_total: int
    fg_bad_total: int
    fg_lat_deg: float
    fg_lon_deg: float
    fg_alt_msl_m: float

    mav_seen_monotonic: float
    mav_system_id: int
    mav_component_id: int
    mav_armed: bool
    mav_custom_mode: int

    created_monotonic: float

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ReadinessDecision:
    accepted: bool
    codes: tuple[str, ...]
    fg_age_s: float
    mav_age_s: float
    evidence_age_s: float
    evidence_sha256: str


class TrustedReadinessCoordinator:
    """Fail-closed backend gate for Setup -> Ready.

    This coordinator deliberately has no API that accepts a UI-provided
    `ready=True`. It consumes concrete backend evidence and worker ownership.
    """

    MAX_FG_AGE_S = 1.0
    MAX_MAV_AGE_S = 2.0
    MAX_EVIDENCE_AGE_S = 2.5
    FUTURE_SKEW_S = 0.05

    def __init__(self, store: Store, sessions: SessionManager,
                 workers: AttemptWorkerCoordinator):
        self.store = store
        self.sessions = sessions
        self.workers = workers

    @classmethod
    def _age_ok(cls, age: float, limit: float) -> bool:
        return math.isfinite(age) and -cls.FUTURE_SKEW_S <= age <= limit

    def check(self, actor: Actor, attempt_id: str, evidence: ReadinessEvidence,
              *, now_monotonic: float | None = None) -> ReadinessDecision:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        attempt = self.store.attempt(attempt_id)
        health: WorkerHealth = self.workers.health(actor, attempt_id)

        fg_age = now - evidence.fg_seen_monotonic
        mav_age = now - evidence.mav_seen_monotonic
        evidence_age = now - evidence.created_monotonic
        codes: list[str] = []

        if attempt.state != State.SETUP:
            codes.append("attempt_not_setup")
        if evidence.attempt_id != attempt.id:
            codes.append("attempt_mismatch")
        if evidence.aircraft_id != attempt.aircraft_id:
            codes.append("aircraft_mismatch")
        if evidence.generation != attempt.generation:
            codes.append("generation_mismatch")

        if not health.healthy or health.status != "running":
            codes.append("worker_unhealthy")
        if health.attempt_id != attempt.id:
            codes.append("worker_attempt_mismatch")
        if health.aircraft_id != attempt.aircraft_id:
            codes.append("worker_aircraft_mismatch")
        if health.generation != attempt.generation:
            codes.append("worker_generation_mismatch")
        if evidence.worker_pid != health.pid:
            codes.append("worker_pid_mismatch")

        if evidence.fg_rx_total < 1:
            codes.append("fg_no_valid_sample")
        if evidence.fg_bad_total < 0 or evidence.fg_bad_total > evidence.fg_rx_total:
            codes.append("fg_counter_invalid")
        if not all(math.isfinite(v) for v in (
            evidence.fg_lat_deg, evidence.fg_lon_deg, evidence.fg_alt_msl_m
        )):
            codes.append("fg_nonfinite")
        if not (-90 <= evidence.fg_lat_deg <= 90 and -180 <= evidence.fg_lon_deg <= 180):
            codes.append("fg_position_invalid")
        if not self._age_ok(fg_age, self.MAX_FG_AGE_S):
            codes.append("fg_stale")

        if evidence.mav_system_id <= 0:
            codes.append("mav_system_invalid")
        if evidence.mav_component_id < 0:
            codes.append("mav_component_invalid")
        if not self._age_ok(mav_age, self.MAX_MAV_AGE_S):
            codes.append("mav_stale")

        if not self._age_ok(evidence_age, self.MAX_EVIDENCE_AGE_S):
            codes.append("evidence_stale")

        return ReadinessDecision(
            accepted=not codes,
            codes=tuple(codes),
            fg_age_s=fg_age,
            mav_age_s=mav_age,
            evidence_age_s=evidence_age,
            evidence_sha256=evidence.fingerprint(),
        )

    def mark_ready(self, actor: Actor, attempt_id: str, evidence: ReadinessEvidence,
                   *, expected_revision: int,
                   reason: str = "trusted worker + raw FG + raw MAVLink readiness") -> tuple[object, ReadinessDecision]:
        """Verify backend evidence, then perform Setup -> Ready.

        The SessionManager remains authoritative for state transition rules and
        instructor assignment. This layer is the trusted source of the
        readiness_verified=True argument.
        """
        if not reason.strip():
            raise CoreError("readiness reason required")

        decision = self.check(actor, attempt_id, evidence)
        with self.store.transaction() as db:
            self.store.event(
                db, attempt_id, "readiness.checked",
                accepted=decision.accepted,
                codes=list(decision.codes),
                evidence_sha256=decision.evidence_sha256,
                fg_age_ms=round(decision.fg_age_s * 1000, 3),
                mav_age_ms=round(decision.mav_age_s * 1000, 3),
                evidence_age_ms=round(decision.evidence_age_s * 1000, 3),
            )

        if not decision.accepted:
            raise CoreError("readiness rejected: " + ",".join(decision.codes))

        updated = self.sessions.transition(
            actor,
            attempt_id,
            State.READY,
            expected_revision=expected_revision,
            reason=reason,
            readiness_verified=True,
        )
        with self.store.transaction() as db:
            self.store.event(
                db, attempt_id, "readiness.accepted",
                evidence_sha256=decision.evidence_sha256,
                generation=updated.generation,
                revision=updated.revision,
                worker_pid=evidence.worker_pid,
            )
        return updated, decision
