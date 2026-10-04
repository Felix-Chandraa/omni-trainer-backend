from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from src.training_core.multi_runtime import (
    RuntimeSlot,
    RuntimeSlotAllocator,
    adapt_arduplane_command,
    extract_arduplane_template,
)


class Dev009Tests(unittest.TestCase):
    def test_slot_map_uses_nonzero_instance_and_unique_ports(self):
        s1 = RuntimeSlot.for_slot(1)
        s2 = RuntimeSlot.for_slot(2)
        s3 = RuntimeSlot.for_slot(3)
        self.assertEqual((s1.sitl_tcp, s1.fg_udp, s1.mav_client_udp, s1.mav_monitor_udp),
                         (5770, 5513, 14560, 14565))
        self.assertEqual((s2.sitl_tcp, s2.fg_udp, s2.mav_client_udp, s2.mav_monitor_udp),
                         (5780, 5523, 14570, 14575))
        self.assertEqual((s3.sitl_tcp, s3.fg_udp, s3.mav_client_udp, s3.mav_monitor_udp),
                         (5790, 5533, 14580, 14585))
        all_ports = set()
        for s in (s1, s2, s3):
            for p in (s.sitl_tcp, s.sitl_tcp_secondary, s.rcin_udp, s.fg_udp,
                      s.mav_client_udp, s.mav_monitor_udp):
                self.assertNotIn(p, all_ports)
                all_ports.add(p)

    def test_slot_zero_is_forbidden(self):
        with self.assertRaises(Exception):
            RuntimeSlot.for_slot(0)

    def test_allocator_supports_three_and_rejects_fourth(self):
        with tempfile.TemporaryDirectory() as td:
            a = RuntimeSlotAllocator(Path(td), max_slots=3)
            leases = [
                a.claim(attempt_id=f"a{i}", session_id=f"s{i}", aircraft_id=f"omni-{i}")
                for i in range(1, 4)
            ]
            self.assertEqual([x.slot.slot for x in leases], [1, 2, 3])
            with self.assertRaisesRegex(Exception, "no free runtime slot"):
                a.claim(attempt_id="a4", session_id="s4", aircraft_id="omni-4")

    def test_allocator_is_idempotent_and_release_is_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a1 = RuntimeSlotAllocator(root, max_slots=3)
            x = a1.claim(attempt_id="a", session_id="s", aircraft_id="omni-a")
            y = a1.claim(attempt_id="b", session_id="s2", aircraft_id="omni-b")
            a2 = RuntimeSlotAllocator(root, max_slots=3)
            self.assertEqual(a2.claim(attempt_id="a", session_id="s", aircraft_id="omni-a"), x)
            self.assertTrue(a2.release("a"))
            remaining = a1.snapshot()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].attempt_id, "b")
            self.assertEqual(remaining[0].slot.slot, y.slot.slot)

    def test_extract_proven_arduplane_command(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "worker.log"
            p.write_text(
                'SIM_VEHICLE: Run ArduPlane\n'
                'SIM_VEHICLE: "/ap/Tools/autotest/run_in_terminal_window.sh" "ArduPlane" '
                '"/ap/build/sitl/bin/arduplane" "-S" "-I0" "--home" '
                '"-7.3,108.2,350,327" "--model" "jsbsim:Omni-Trainer" '
                '"--defaults" "/ap/default.parm"\n'
            )
            got = extract_arduplane_template(p)
            self.assertEqual(got[0], "/ap/build/sitl/bin/arduplane")
            self.assertNotEqual(got[0], "ArduPlane")
            self.assertIn("jsbsim:Omni-Trainer", got)
            self.assertIn("-I0", got)

    def test_adapt_command_changes_instance_and_autotest_dir_only(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "arduplane"
            binary.write_text("fixture")
            binary.chmod(0o755)
            template = (
                str(binary), "-S", "-I0", "--home", "-7,108,350,327",
                "--model", "jsbsim:Omni-Trainer",
                "--defaults", "/tmp/default.parm",
            )
            auto = Path(td) / "autotest"
            got = adapt_arduplane_command(template, instance=2, autotest_dir=auto)
            self.assertIn("-I2", got)
            self.assertNotIn("-I0", got)
            self.assertIn("--autotest-dir", got)
            self.assertIn(str(auto.resolve()), got)

    def test_direct_supervisor_has_no_global_name_based_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "src/training_core/multi_worker_entry.py"
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))

        forbidden_call_names = {"pkill", "killall", "kill_tasks"}
        forbidden_command_literals = {"pkill", "killall"}

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Direct calls such as kill_tasks(...), pkill(...), killall(...).
            if isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_call_names)
            elif isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_call_names)

            # Shell/subprocess execution must not contain a literal global
            # name-based killer. Docstrings/comments are intentionally ignored.
            func_name = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name)
                else ""
            )
            if func_name in {
                "system", "run", "call", "Popen",
                "check_call", "check_output",
            }:
                literal_parts = []
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        literal_parts.append(arg.value)
                    elif isinstance(arg, (ast.List, ast.Tuple)):
                        for elt in arg.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                literal_parts.append(elt.value)
                words = " ".join(literal_parts).lower().split()
                self.assertTrue(
                    forbidden_command_literals.isdisjoint(words),
                    f"global name-based cleanup command found: {literal_parts}",
                )

        self.assertIn("scoped_signal", text)
        self.assertIn("starttime", text)
        self.assertIn("same_process", text)

    def test_multi_live_path_does_not_execute_sim_vehicle(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "src/training_core/multi_live_trial.py"
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))

        worker_argv_fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "worker_argv"
        )
        literals = {
            n.value
            for n in ast.walk(worker_argv_fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        self.assertFalse(
            any("sim_vehicle.py" in value for value in literals),
            "worker_argv must not contain sim_vehicle.py as an executable/literal",
        )
        self.assertIn("src.training_core.multi_worker_entry", literals)
        self.assertIn("max_slots=3", text)

    def test_dev009_mav_probe_is_local_and_per_slot(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/multi_live_trial.py").read_text()
        self.assertNotIn("from .omni_live_trial import raw_mav_probe", text)
        self.assertIn("def raw_mav_probe_slot", text)
        self.assertIn('"--conn", conn', text)
        self.assertIn("lease.slot.mav_monitor_udp", text)


if __name__ == "__main__":
    unittest.main()

