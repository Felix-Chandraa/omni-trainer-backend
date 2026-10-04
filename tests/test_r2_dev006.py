from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from src.training_core.models import CoreError
from src.training_core.omni_live_trial import (
    classify_log, require_clean_source, tail_text, tcp_port_free, udp_port_free,
)
from src.training_core.omni_launcher_adapter import source_from_root


class Dev006Tests(unittest.TestCase):
    def test_tcp_guard_detects_busy_listener_without_connecting(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertFalse(tcp_port_free("127.0.0.1", port))
        finally:
            s.close()
        self.assertTrue(tcp_port_free("127.0.0.1", port))

    def test_udp_guard_detects_exclusive_owner(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        try:
            self.assertFalse(udp_port_free("127.0.0.1", port))
        finally:
            s.close()
        self.assertTrue(udp_port_free("127.0.0.1", port))

    def test_tail_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"x.log"; p.write_text("\n".join(f"line-{i}" for i in range(100)))
            out=tail_text(p, max_lines=3)
            self.assertEqual(out.splitlines(), ["line-97", "line-98", "line-99"])

    def test_log_classifier_finds_panic_and_port_conflict(self):
        flags=classify_log("PANIC: JSBSim failed\nAddress already in use")
        self.assertIn("jsbsim_panic", flags)
        self.assertIn("port_conflict", flags)

    def test_log_classifier_clean(self):
        self.assertEqual(classify_log("normal startup heartbeat ok"), [])

    def test_live_source_model_mismatch_is_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"omni"
            ap=root/"omnitrainer-sitl/ardupilot"
            paths=[
                root/"run_demo.sh",
                ap/"Tools/autotest/sim_vehicle.py",
                root/".venv/bin/python3",
                ap/"Tools/autotest/aircraft/Omni-Trainer/omni_trainer_sitl.parm",
                root/"omnitrainer-sitl/assets/ardupilot/aircraft/Omni-Trainer/Omni-Trainer.xml",
                ap/"Tools/autotest/aircraft/Omni-Trainer/Omni-Trainer.xml",
                root/"omnitrainer-sitl/assets/missions/wiriadinata_default.txt",
                root/"omnitrainer-sitl/scripts/load_default_mission.py",
            ]
            for p in paths:
                p.parent.mkdir(parents=True, exist_ok=True); p.write_text("same")
            (root/"run_demo.sh").write_text("echo ok")
            paths[4].write_text("source-new"); paths[5].write_text("installed-old")
            jsb=Path(td)/"JSBSim"; jsb.write_text("x"); jsb.chmod(0o755)
            source=source_from_root(root)
            with self.assertRaises(CoreError): require_clean_source(source, jsb)

    def test_managed_entry_contract_contains_no_legacy_cleanup(self):
        here=Path(__file__).resolve().parents[1]
        text=(here/"src/training_core/omni_managed_entry.py").read_text()
        self.assertNotIn("run_demo.sh", text)
        # The DEV-005 docstring mentions forbidden legacy cleanup by name;
        # inspect executable constructs instead of rejecting documentation text.
        self.assertNotIn("subprocess.", text)
        self.assertNotIn("os.system(", text)
        self.assertNotIn("killall(", text)
        self.assertNotIn("\"-w\"", text)


if __name__ == "__main__": unittest.main()
