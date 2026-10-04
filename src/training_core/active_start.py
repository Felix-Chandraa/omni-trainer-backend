"""Authorized Ready -> Active gate for R2.

The SessionManager owns state transitions. This module proves that a Ready
Attempt still belongs to the same healthy worker and still has fresh runtime
evidence before an instructor may enter Active.

It also provides an explicit trusted readiness invalidation path Ready -> Setup.
No worker is started here and no flight command is emitted here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from .models import Actor, CoreError, State
from .readiness import ReadinessEvidence, TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator, WorkerHealth


@dataclass(frozen=True)
class ActiveStartDecision:
    accepted: bool
    codes: tuple[str, ...]
    fg_age_s: float
    mav_age_s: float
    evidence_age_s: float
    evidence_sha256: str
    worker_pid: int


class AuthorizedActiveStart:
    """Fail-closed trusted gate for Ready -> Active."""

    def __init__(self, store: Store, sessions: SessionManager,
                 workers: AttemptWorkerCoordinator):
        self.store = store
        self.sessions = sessions
        self.workers = workers

    @staticmethod
    def _age_ok(age: float, limit: float) -> bool:
        return (
            math.isfinite(age)
            and -TrustedReadinessCoordinator.FUTURE_SKEW_S <= age <= limit
        )

    def check(self, actor: Actor, attempt_id: str, evidence: ReadinessEvidence,
              *, now_monotonic: float | None = None) -> ActiveStartDecision:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        attempt = self.store.attempt(attempt_id)
        health: WorkerHealth = self.workers.health(actor, attempt_id)

        fg_age = now - evidence.fg_seen_monotonic
        mav_age = now - evidence.mav_seen_monotonic
        evidence_age = now - evidence.created_monotonic
        codes: list[str] = []

        if attempt.state != State.READY:
            codes.append("attempt_not_ready")

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

        if not self._age_ok(
            fg_age, TrustedReadinessCoordinator.MAX_FG_AGE_S
        ):
            codes.append("fg_stale")
        if evidence.mav_system_id <= 0:
            codes.append("mav_system_invalid")
        if evidence.mav_component_id < 0:
            codes.append("mav_component_invalid")
        if not self._age_ok(
            mav_age, TrustedReadinessCoordinator.MAX_MAV_AGE_S
        ):
            codes.append("mav_stale")
        if not self._age_ok(
            evidence_age, TrustedReadinessCoordinator.MAX_EVIDENCE_AGE_S
        ):
            codes.append("evidence_stale")

        return ActiveStartDecision(
            accepted=not codes,
            codes=tuple(codes),
            fg_age_s=fg_age,
            mav_age_s=mav_age,
            evidence_age_s=evidence_age,
            evidence_sha256=evidence.fingerprint(),
            worker_pid=health.pid,
        )

    def start_active(self, actor: Actor, attempt_id: str,
                     evidence: ReadinessEvidence, *,
                     expected_revision: int,
                     reason: str = "authorized start with revalidated runtime evidence"):
        if not reason.strip():
            raise CoreError("active-start reason required")

        decision = self.check(actor, attempt_id, evidence)
        with self.store.transaction() as db:
            self.store.event(
                db, attempt_id, "active_start.checked",
                accepted=decision.accepted,
                codes=list(decision.codes),
                evidence_sha256=decision.evidence_sha256,
                worker_pid=decision.worker_pid,
                fg_age_ms=round(decision.fg_age_s * 1000, 3),
                mav_age_ms=round(decision.mav_age_s * 1000, 3),
                evidence_age_ms=round(decision.evidence_age_s * 1000, 3),
            )

        if not decision.accepted:
            raise CoreError("active start rejected: " + ",".join(decision.codes))

        updated = self.sessions.transition(
            actor,
            attempt_id,
            State.ACTIVE,
            expected_revision=expected_revision,
            reason=reason,
        )
        with self.store.transaction() as db:
            self.store.event(
                db, attempt_id, "active_start.accepted",
                evidence_sha256=decision.evidence_sha256,
                worker_pid=decision.worker_pid,
                generation=updated.generation,
                revision=updated.revision,
            )
        return updated, decision

    def invalidate_ready(self, actor: Actor, attempt_id: str, *,
                         expected_revision: int, codes: tuple[str, ...] | list[str],
                         reason: str):
        """Trusted monitor path for Ready -> Setup invalidation.

        This does not happen from an arbitrary UI boolean. Callers provide
        concrete monitor failure codes and a human-readable reason.
        """
        normalized = tuple(str(c).strip() for c in codes if str(c).strip())
        if not normalized:
            raise CoreError("readiness invalidation codes required")
        if not reason.strip():
            raise CoreError("readiness invalidation reason required")

        attempt = self.store.attempt(attempt_id)
        if attempt.state != State.READY:
            raise CoreError("readiness invalidation requires Ready attempt")

        updated = self.sessions.transition(
            actor,
            attempt_id,
            State.SETUP,
            expected_revision=expected_revision,
            reason=reason,
        )
        with self.store.transaction() as db:
            self.store.event(
                db, attempt_id, "readiness.invalidated",
                codes=list(normalized),
                reason=reason,
                revision=updated.revision,
                generation=updated.generation,
            )
        return updated
