from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from src.training_core.models import Actor, CoreError, Role, State
from src.training_core.readiness import ReadinessEvidence, TrustedReadinessCoordinator
from src.training_core.session import SessionManager
from src.training_core.store import Store
from src.training_core.worker_lifecycle import WorkerHealth


class FakeWorkers:
    def __init__(self, health: WorkerHealth):
        self.current = health

    def health(self, actor, attempt_id):
        return self.current


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "r.sqlite")
        self.sessions = SessionManager(self.store)
        self.instructor = Actor("inst-1", Role.INSTRUCTOR)
        self.session_id = self.sessions.create_session(self.instructor, "student-1")
        self.exercise_id = self.sessions.create_exercise(
            self.instructor, self.session_id, "readiness-test-v1"
        )
        self.attempt = self.sessions.create_attempt(
            self.instructor, self.exercise_id, "omni-1"
        )
        self.health = WorkerHealth(
            attempt_id=self.attempt.id,
            aircraft_id=self.attempt.aircraft_id,
            generation=self.attempt.generation,
            pid=4242,
            status="running",
            healthy=True,
            exit_code=None,
            runtime_dir="/tmp/fixture",
        )
        self.workers = FakeWorkers(self.health)
        self.gate = TrustedReadinessCoordinator(
            self.store, self.sessions, self.workers
        )

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def evidence(self, **changes):
        now = time.monotonic()
        base = ReadinessEvidence(
            attempt_id=self.attempt.id,
            aircraft_id=self.attempt.aircraft_id,
            generation=self.attempt.generation,
            worker_pid=self.health.pid,
            fg_seen_monotonic=now - 0.05,
            fg_rx_total=100,
            fg_bad_total=0,
            fg_lat_deg=-7.346914,
            fg_lon_deg=108.246372,
            fg_alt_msl_m=349.35,
            mav_seen_monotonic=now - 0.05,
            mav_system_id=1,
            mav_component_id=0,
            mav_armed=False,
            mav_custom_mode=16,
            created_monotonic=now,
        )
        return replace(base, **changes)

    def test_fresh_owned_evidence_is_the_only_path_to_ready(self):
        ready, decision = self.gate.mark_ready(
            self.instructor,
            self.attempt.id,
            self.evidence(),
            expected_revision=self.attempt.revision,
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(ready.state, State.READY)
        self.assertEqual(ready.revision, 1)

        names = [e["event_type"] for e in self.store.events(self.attempt.id)]
        self.assertIn("readiness.checked", names)
        self.assertIn("readiness.accepted", names)
        self.assertIn("attempt.state_changed", names)

    def test_stale_fg_rejected_and_attempt_remains_setup(self):
        ev = self.evidence(fg_seen_monotonic=time.monotonic() - 5.0)
        with self.assertRaisesRegex(CoreError, "fg_stale"):
            self.gate.mark_ready(
                self.instructor, self.attempt.id, ev,
                expected_revision=self.attempt.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.SETUP)

    def test_stale_mav_rejected(self):
        ev = self.evidence(mav_seen_monotonic=time.monotonic() - 5.0)
        with self.assertRaisesRegex(CoreError, "mav_stale"):
            self.gate.mark_ready(
                self.instructor, self.attempt.id, ev,
                expected_revision=self.attempt.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.SETUP)

    def test_wrong_generation_pid_and_aircraft_fail_closed(self):
        cases = [
            self.evidence(generation=self.attempt.generation + 1),
            self.evidence(worker_pid=9999),
            self.evidence(aircraft_id="other-aircraft"),
        ]
        for ev in cases:
            with self.subTest(ev=ev):
                decision = self.gate.check(self.instructor, self.attempt.id, ev)
                self.assertFalse(decision.accepted)

    def test_unhealthy_worker_rejected(self):
        self.workers.current = replace(
            self.health, healthy=False, status="exited", exit_code=1
        )
        with self.assertRaisesRegex(CoreError, "worker_unhealthy"):
            self.gate.mark_ready(
                self.instructor, self.attempt.id, self.evidence(),
                expected_revision=self.attempt.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.SETUP)

    def test_ready_does_not_imply_active(self):
        ready, _ = self.gate.mark_ready(
            self.instructor,
            self.attempt.id,
            self.evidence(),
            expected_revision=self.attempt.revision,
        )
        self.assertEqual(ready.state, State.READY)
        self.assertNotEqual(ready.state, State.ACTIVE)

    def test_reusing_evidence_after_ready_is_rejected(self):
        ev = self.evidence()
        ready, _ = self.gate.mark_ready(
            self.instructor, self.attempt.id, ev,
            expected_revision=self.attempt.revision,
        )
        self.assertEqual(ready.state, State.READY)
        decision = self.gate.check(self.instructor, self.attempt.id, ev)
        self.assertFalse(decision.accepted)
        self.assertIn("attempt_not_setup", decision.codes)

    def test_student_cannot_mark_ready(self):
        student = Actor("student-1", Role.STUDENT)
        with self.assertRaises(CoreError):
            self.gate.mark_ready(
                student, self.attempt.id, self.evidence(),
                expected_revision=self.attempt.revision,
            )
        self.assertEqual(self.store.attempt(self.attempt.id).state, State.SETUP)


if __name__ == "__main__":
    unittest.main()
