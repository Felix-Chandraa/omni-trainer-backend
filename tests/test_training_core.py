"""Offline R2 contract checks: zero SITL, zero network, zero PyQt dependencies."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.training_core import (Actor, AircraftWorker, Command, CommandValidator, CoreError,
                               Role, Scope, SessionManager, State, Store, WorkerSpec)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "metadata.sqlite")
        self.manager = SessionManager(self.store)
        self.inst = Actor("instructor-A", Role.INSTRUCTOR)
        self.student = Actor("student-A", Role.STUDENT)
        self.sid = self.manager.create_session(self.inst, self.student.id)
        self.eid = self.manager.create_exercise(self.inst, self.sid, "manual@draft-1")
        self.first = self.manager.create_attempt(self.inst, self.eid, "omni-1")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def step(self, attempt, target, *, reason="test", **flags):
        return self.manager.transition(self.inst, attempt.id, target,
                                       expected_revision=attempt.revision, reason=reason, **flags)

    def active(self):
        ready = self.step(self.first, State.READY, readiness_verified=True)
        return self.step(ready, State.ACTIVE)

    def cmd(self, attempt, **updates):
        data = dict(id="cmd-1", attempt_id=attempt.id, aircraft_id=attempt.aircraft_id,
                    actor=self.student, scope=Scope.FLIGHT, generation=attempt.generation,
                    authority_epoch=attempt.authority_epoch, expected_revision=attempt.revision,
                    expires_utc=1000.0, name="flight_probe", payload={})
        data.update(updates)
        return Command(**data)

    def test_student_cannot_create_session_or_attempt(self):
        with self.assertRaises(CoreError):
            self.manager.create_session(self.student, "someone")
        with self.assertRaises(CoreError):
            self.manager.create_attempt(self.student, self.eid, "omni-1")

    def test_unassigned_instructor_rejected(self):
        with self.assertRaises(CoreError):
            self.manager.transition(Actor("other", Role.INSTRUCTOR), self.first.id,
                                    State.ENDED, expected_revision=0, reason="wrong")

    def test_ready_requires_trusted_check(self):
        with self.assertRaises(CoreError):
            self.step(self.first, State.READY)
        self.assertEqual(self.store.attempt(self.first.id).state, State.SETUP)

    def test_start_pause_resume_end_and_immutable_end(self):
        a = self.active()
        a = self.step(a, State.PAUSED)
        a = self.step(a, State.ACTIVE)
        a = self.step(a, State.ENDED)
        with self.assertRaises(CoreError):
            self.step(a, State.ACTIVE)
        self.assertEqual([e["event_type"] for e in self.store.events(a.id)].count("attempt.state_changed"), 5)

    def test_restart_new_generation_preserves_original(self):
        with self.assertRaises(CoreError):
            self.manager.create_attempt(self.inst, self.eid, "omni-1")
        ended = self.step(self.first, State.ENDED)
        nxt = self.manager.create_attempt(self.inst, self.eid, "omni-1")
        self.assertEqual((ended.generation, nxt.generation), (1, 2))
        self.assertNotEqual(ended.id, nxt.id)
        self.assertEqual(self.store.attempt(ended.id).state, State.ENDED)

    def test_interrupted_recovery_requires_continuity(self):
        a = self.step(self.active(), State.INTERRUPTED)
        with self.assertRaises(CoreError):
            self.step(a, State.PAUSED)
        a = self.step(a, State.PAUSED, continuity_verified=True)
        self.assertEqual(a.state, State.PAUSED)

    def test_stale_revision_rejected(self):
        a = self.active()
        with self.assertRaises(CoreError):
            self.manager.transition(self.inst, a.id, State.PAUSED,
                                    expected_revision=0, reason="stale")

    def test_command_accepted_is_not_applied_and_idempotent(self):
        a = self.active()
        c = self.cmd(a)
        validator = CommandValidator(self.store)
        one = validator.validate(c, now_utc=10)
        two = validator.validate(c, now_utc=11)
        self.assertEqual((one.status, two.status), ("accepted", "accepted"))
        self.assertIn("NOT applied", one.reason)
        self.assertEqual([e["event_type"] for e in self.store.events(a.id)].count("command.admission"), 1)
        self.assertEqual(validator.validate(self.cmd(a, name="payload_probe"), now_utc=10).status, "rejected")

    def test_command_rejects_wrong_owner_and_stale(self):
        a = self.active()
        v = CommandValidator(self.store)
        cases = [
            dict(actor=Actor("other", Role.STUDENT)), dict(actor=self.inst),
            dict(generation=0), dict(aircraft_id="another"),
            dict(authority_epoch=0), dict(expected_revision=0), dict(expires_utc=5),
            dict(name="arm"), dict(scope=Scope.PAYLOAD),
        ]
        for i, change in enumerate(cases):
            with self.subTest(change=change):
                self.assertEqual(v.validate(self.cmd(a, id=f"negative-{i}", **change), now_utc=10).status,
                                 "rejected")

    def test_command_rejected_during_pause_and_after_restart(self):
        a = self.step(self.active(), State.PAUSED)
        v = CommandValidator(self.store)
        self.assertEqual(v.validate(self.cmd(a), now_utc=10).status, "rejected")
        a = self.step(a, State.ENDED)
        b = self.manager.create_attempt(self.inst, self.eid, "omni-1")
        self.assertEqual(v.validate(self.cmd(a, id="ended"), now_utc=10).status, "rejected")
        self.assertEqual(b.generation, a.generation + 1)

    def test_durable_reopen(self):
        a = self.active()
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.attempt(a.id).state, State.ACTIVE)
        self.assertGreaterEqual(len(self.store.events()), 5)

    def test_owned_worker_stop_without_killing_unrelated_process(self):
        import subprocess
        outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                    start_new_session=True)
        spec = WorkerSpec("test-aircraft", (sys.executable, "-c", "import time; time.sleep(30)"),
                          Path(self.tmp.name), Path(self.tmp.name) / "worker")
        worker = AircraftWorker(spec)
        try:
            pid = worker.start()
            self.assertGreater(pid, 0)
            with self.assertRaises(CoreError):
                worker.start()
            worker.stop(timeout=1)
            self.assertIsNone(outsider.poll())
            self.assertTrue((spec.runtime_dir / "worker.log").is_file())
        finally:
            if outsider.poll() is None:
                outsider.terminate()
            outsider.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
