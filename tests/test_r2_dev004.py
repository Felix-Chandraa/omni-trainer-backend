import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from src.training_core.models import Actor, CoreError, Role, State
from src.training_core.session import SessionManager
from src.training_core.store import Store
from src.training_core.worker_lifecycle import AttemptWorkerCoordinator


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "metadata.sqlite")
        self.manager = SessionManager(self.store)
        self.inst = Actor("inst-004", Role.INSTRUCTOR)
        self.other = Actor("other-004", Role.INSTRUCTOR)
        sid = self.manager.create_session(self.inst, "student-004")
        eid = self.manager.create_exercise(self.inst, sid, "dev004@fixture")
        self.attempt = self.manager.create_attempt(self.inst, eid, "omni-1")
        self.coord = AttemptWorkerCoordinator(self.store, self.root / "workers")
        self.started = []

    def tearDown(self):
        # Clean only workers this test created, never global processes.
        for attempt_id in list(self.started):
            try:
                h = self.coord.health(self.inst, attempt_id)
                if h.status == "running":
                    self.coord.stop(self.inst, attempt_id, timeout=0.5)
                elif h.status == "exited":
                    self.coord.release_after_exit(self.inst, attempt_id)
            except Exception:
                pass
        self.store.close()
        self.tmp.cleanup()

    def start_sleep(self, attempt=None):
        attempt = attempt or self.attempt
        h = self.coord.start(self.inst, attempt.id,
                             argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                             cwd=self.root)
        self.started.append(attempt.id)
        return h

    def test_start_health_manifest_and_events(self):
        h = self.start_sleep()
        self.assertTrue(h.healthy)
        self.assertEqual(h.status, "running")
        manifest = json.loads((Path(h.runtime_dir) / "worker.json").read_text())
        self.assertEqual(manifest["attempt_id"], self.attempt.id)
        self.assertEqual(manifest["generation"], 1)
        self.assertFalse(manifest["adoption_supported"])
        names = [e["event_type"] for e in self.store.events(self.attempt.id)]
        self.assertIn("worker.start_requested", names)
        self.assertIn("worker.started", names)

    def test_duplicate_start_rejected(self):
        self.start_sleep()
        with self.assertRaises(CoreError):
            self.start_sleep()

    def test_student_or_wrong_instructor_cannot_manage_worker(self):
        student = Actor("student-004", Role.STUDENT)
        with self.assertRaises(CoreError):
            self.coord.start(student, self.attempt.id,
                             argv=(sys.executable, "-c", "pass"), cwd=self.root)
        with self.assertRaises(CoreError):
            self.coord.start(self.other, self.attempt.id,
                             argv=(sys.executable, "-c", "pass"), cwd=self.root)

    def test_ended_attempt_cannot_start_worker(self):
        ended = self.manager.transition(self.inst, self.attempt.id, State.ENDED,
                                        expected_revision=self.attempt.revision, reason="test end")
        self.assertEqual(ended.state, State.ENDED)
        with self.assertRaises(CoreError):
            self.coord.start(self.inst, self.attempt.id,
                             argv=(sys.executable, "-c", "pass"), cwd=self.root)

    def test_same_aircraft_cannot_be_leased_to_second_attempt(self):
        self.start_sleep()
        sid2 = self.manager.create_session(self.inst, "student-B")
        eid2 = self.manager.create_exercise(self.inst, sid2, "dev004@other")
        second = self.manager.create_attempt(self.inst, eid2, "omni-1")
        with self.assertRaises(CoreError):
            self.coord.start(self.inst, second.id,
                             argv=(sys.executable, "-c", "import time; time.sleep(30)"), cwd=self.root)

    def test_unexpected_exit_is_unhealthy_and_recorded_once(self):
        h = self.coord.start(self.inst, self.attempt.id,
                             argv=(sys.executable, "-c", "import sys; sys.exit(7)"), cwd=self.root)
        self.started.append(self.attempt.id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            health = self.coord.health(self.inst, self.attempt.id)
            if health.status == "exited":
                break
            time.sleep(0.02)
        self.assertEqual(health.status, "exited")
        self.assertFalse(health.healthy)
        self.assertEqual(health.exit_code, 7)
        self.coord.health(self.inst, self.attempt.id)
        exits = [e for e in self.store.events(self.attempt.id) if e["event_type"] == "worker.exited"]
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["payload"]["unexpected"])

    def test_stop_is_scoped_and_does_not_kill_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                     start_new_session=True)
        try:
            self.start_sleep()
            h = self.coord.stop(self.inst, self.attempt.id, timeout=0.5)
            self.assertEqual(h.status, "stopped")
            self.started.remove(self.attempt.id)
            self.assertIsNone(unrelated.poll())
            names = [e["event_type"] for e in self.store.events(self.attempt.id)]
            self.assertIn("worker.stop_requested", names)
            self.assertIn("worker.stopped", names)
        finally:
            if unrelated.poll() is None:
                os.killpg(unrelated.pid, 15)
                unrelated.wait(timeout=2)

    def test_crashed_worker_can_be_reaped_then_aircraft_released(self):
        h = self.coord.start(self.inst, self.attempt.id,
                             argv=(sys.executable, "-c", "raise SystemExit(3)"), cwd=self.root)
        self.started.append(self.attempt.id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            health = self.coord.health(self.inst, self.attempt.id)
            if health.status == "exited":
                break
            time.sleep(0.02)
        self.assertEqual(health.exit_code, 3)
        reaped = self.coord.release_after_exit(self.inst, self.attempt.id)
        self.started.remove(self.attempt.id)
        self.assertEqual(reaped.status, "reaped")
        # A different attempt may lease the aircraft after explicit reap.
        sid2 = self.manager.create_session(self.inst, "student-C")
        eid2 = self.manager.create_exercise(self.inst, sid2, "dev004@after-reap")
        second = self.manager.create_attempt(self.inst, eid2, "omni-1")
        h2 = self.coord.start(self.inst, second.id,
                              argv=(sys.executable, "-c", "import time; time.sleep(30)"), cwd=self.root)
        self.started.append(second.id)
        self.assertTrue(h2.healthy)


if __name__ == "__main__":
    unittest.main()
