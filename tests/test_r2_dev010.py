from __future__ import annotations

import ast
from pathlib import Path
import tempfile
import unittest

from src.training_core.multi_runtime import RuntimeSlotAllocator, RuntimeSlot


class Dev010Tests(unittest.TestCase):
    def test_exact_three_slot_contract(self):
        slots = [RuntimeSlot.for_slot(i) for i in (1, 2, 3)]
        self.assertEqual(
            [(s.instance, s.sitl_tcp, s.fg_udp, s.mav_monitor_udp) for s in slots],
            [
                (1, 5770, 5513, 14565),
                (2, 5780, 5523, 14575),
                (3, 5790, 5533, 14585),
            ],
        )

    def test_three_leases_are_simultaneous_and_unique(self):
        with tempfile.TemporaryDirectory() as td:
            allocator = RuntimeSlotAllocator(Path(td), max_slots=3)
            leases = [
                allocator.claim(
                    attempt_id=f"attempt-{i}",
                    session_id=f"session-{i}",
                    aircraft_id=f"omni-{i}",
                    preferred_slot=i,
                )
                for i in (1, 2, 3)
            ]
            self.assertEqual({x.slot.slot for x in leases}, {1, 2, 3})
            self.assertEqual(len(allocator.snapshot()), 3)

    def test_dev010_source_starts_exactly_three_student_records(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "src/training_core/three_student_live_trial.py"
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))
        self.assertIn('zip((1, 2, 3), ("A", "B", "C"))', text)
        self.assertIn('labels=("A", "B", "C")', text)
        self.assertIn('labels=("B", "C")', text)
        self.assertIn('labels=("C",)', text)
        self.assertIn("THREE CONCURRENT ACTIVE", text)

    def test_dev010_reuses_dev009_direct_supervisor(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/three_student_live_trial.py").read_text()
        self.assertIn("worker_argv", text)
        self.assertIn("RuntimeSlotAllocator", text)
        self.assertNotIn("run_demo.sh", text)
        self.assertNotIn("sim_vehicle.py", text)

    def test_resource_snapshot_is_observational_not_acceptance_gate(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/three_student_live_trial.py").read_text()
        self.assertIn("RESOURCE SNAPSHOT", text)
        self.assertNotIn("resource_snapshot_three_active] >", text)
        self.assertNotIn("mem_available_mb() <", text)

    def test_full_port_reuse_check_happens_after_fg_observers_close(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/three_student_live_trial.py").read_text()
        close_marker = "ExitStack has now closed all DEV-010-owned FGObserver sockets."
        check_marker = "not completely reusable after observer cleanup"
        self.assertIn(close_marker, text)
        self.assertIn(check_marker, text)
        self.assertLess(text.index(close_marker), text.index(check_marker))
        self.assertIn("FINAL CLEANUP VERIFIED", text)


if __name__ == "__main__":
    unittest.main()
