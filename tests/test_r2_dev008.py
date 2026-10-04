from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from src.training_core.active_start import AuthorizedActiveStart
from src.training_core.models import Actor, CoreError, Role, State
from src.training_core.readiness import ReadinessEvidence, TrustedReadinessCoordinator
from src.training_core.session import SessionManager
from src.training_core.store import Store
from src.training_core.worker_lifecycle import WorkerHealth


class FakeWorkers:
    def __init__(self, health):
        self.current = health

    def health(self, actor, attempt_id):
        if actor.role != Role.INSTRUCTOR:
            raise CoreError("instructor not assigned to session")
        return self.current


class Dev008Tests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "db.sqlite")
        self.sessions = SessionManager(self.store)
        self.instructor = Actor("inst-8", Role.INSTRUCTOR)
        sid = self.sessions.create_session(self.instructor, "student-8")
        eid = self.sessions.create_exercise(self.instructor, sid, "dev008")
        self.attempt = self.sessions.create_attempt(self.instructor, eid, "omni-1")
        self.health = WorkerHealth(
            attempt_id=self.attempt.id,
            aircraft_id=self.attempt.aircraft_id,
            generation=self.attempt.generation,
            pid=8080,
            status="running",
            healthy=True,
            exit_code=None,
            runtime_dir="/tmp/dev008",
        )
        self.workers = FakeWorkers(self.health)
        self.readiness = TrustedReadinessCoordinator(
            self.store, self.sessions, self.workers
        )
        self.active = AuthorizedActiveStart(
            self.store, self.sessions, self.workers
        )

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def evidence(self, **changes):
        now = time.monotonic()
        ev = ReadinessEvidence(
            attempt_id=self.attempt.id,
            aircraft_id=self.attempt.aircraft_id,
            generation=self.attempt.generation,
            worker_pid=self.health.pid,
            fg_seen_monotonic=now - 0.03,
            fg_rx_total=50,
            fg_bad_total=0,
            fg_lat_deg=-7.346914,
            fg_lon_deg=108.246372,
            fg_alt_msl_m=349.35,
            mav_seen_monotonic=now - 0.03,
            mav_system_id=1,
            mav_component_id=0,
            mav_armed=False,
            mav_custom_mode=16,
            created_monotonic=now,
        )
        return replace(ev, **changes)

    def make_ready(self):
        ready, _ = self.readiness.mark_ready(
            self.instructor,
            self.attempt.id,
            self.evidence(),
            expected_revision=self.attempt.revision,
        )
        return ready

    def test_ready_to_active_requires_fresh_same_worker_evidence(self):
        ready = self.make_ready()
        active, decision = self.active.start_active(
            self.instructor,
            self.attempt.id,
            self.evidence(),
            expected_revision=ready.revision,
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(active.state, State.ACTIVE)
        names = [e["event_type"] for e in self.store.events(self.attempt.id)]
        self.assertIn("active_start.checked", names)
        self.assertIn("active_start.accepted", names)

    def test_setup_cannot_skip_ready(self):
        with self.assertRaisesRegex(CoreError, "attempt_not_ready"):
            self.active.start_active(
                self.instructor,
                self.attempt.id,
                self.evidence(),
                expected_revision=self.attempt.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.SETUP)

    def test_student_cannot_start_active(self):
        ready = self.make_ready()
        student = Actor("student-8", Role.STUDENT)
        with self.assertRaises(CoreError):
            self.active.start_active(
                student,
                self.attempt.id,
                self.evidence(),
                expected_revision=ready.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.READY)

    def test_stale_evidence_rejected_before_active(self):
        ready = self.make_ready()
        old = time.monotonic() - 10.0
        ev = self.evidence(
            fg_seen_monotonic=old,
            mav_seen_monotonic=old,
            created_monotonic=old,
        )
        with self.assertRaisesRegex(CoreError, "stale"):
            self.active.start_active(
                self.instructor,
                self.attempt.id,
                ev,
                expected_revision=ready.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.READY)

    def test_pid_or_generation_change_rejected(self):
        ready = self.make_ready()
        cases = (
            self.evidence(worker_pid=9999),
            self.evidence(generation=self.attempt.generation + 1),
        )
        for ev in cases:
            with self.subTest(ev=ev):
                decision = self.active.check(
                    self.instructor, self.attempt.id, ev
                )
                self.assertFalse(decision.accepted)
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.READY)

    def test_unhealthy_worker_rejected(self):
        ready = self.make_ready()
        self.workers.current = replace(
            self.health, healthy=False, status="exited", exit_code=1
        )
        with self.assertRaisesRegex(CoreError, "worker_unhealthy"):
            self.active.start_active(
                self.instructor,
                self.attempt.id,
                self.evidence(),
                expected_revision=ready.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.READY)

    def test_explicit_readiness_invalidation_returns_ready_to_setup(self):
        ready = self.make_ready()
        setup = self.active.invalidate_ready(
            self.instructor,
            self.attempt.id,
            expected_revision=ready.revision,
            codes=("worker_unhealthy",),
            reason="worker health lost before start",
        )
        self.assertEqual(setup.state, State.SETUP)
        names = [e["event_type"] for e in self.store.events(self.attempt.id)]
        self.assertIn("readiness.invalidated", names)

    def test_duplicate_start_cannot_reenter_active(self):
        ready = self.make_ready()
        active, _ = self.active.start_active(
            self.instructor,
            self.attempt.id,
            self.evidence(),
            expected_revision=ready.revision,
        )
        with self.assertRaisesRegex(CoreError, "attempt_not_ready"):
            self.active.start_active(
                self.instructor,
                self.attempt.id,
                self.evidence(),
                expected_revision=active.revision,
            )


if __name__ == "__main__":
    unittest.main()
