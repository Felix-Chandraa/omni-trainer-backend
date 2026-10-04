from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from src.training_core.models import CoreError
from src.training_core.omni_launcher_adapter import (
    audit_legacy_run_demo, build_initial_plan, choose_jsbsim, probe_tcp_port,
    source_from_root, validate_source,
)


class Dev005Tests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "omni_flight-main"
        self.home = Path(self.td.name) / "home"
        ap = self.root / "omnitrainer-sitl/ardupilot"
        files = [
            self.root / "run_demo.sh",
            ap / "Tools/autotest/sim_vehicle.py",
            self.root / ".venv/bin/python3",
            ap / "Tools/autotest/aircraft/Omni-Trainer/omni_trainer_sitl.parm",
            self.root / "omnitrainer-sitl/assets/ardupilot/aircraft/Omni-Trainer/Omni-Trainer.xml",
            ap / "Tools/autotest/aircraft/Omni-Trainer/Omni-Trainer.xml",
            self.root / "omnitrainer-sitl/assets/missions/wiriadinata_default.txt",
            self.root / "omnitrainer-sitl/scripts/load_default_mission.py",
        ]
        for p in files:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        (self.root / "run_demo.sh").write_text('install -m 0644 "$MODEL_SRC" "$MODEL_DST"\npkill -f "$AP_DIR"\nLOADER_PID=1\ntrap cleanup EXIT INT TERM\n')
        for p in [self.root / ".venv/bin/python3", ap / "Tools/autotest/sim_vehicle.py"]:
            p.chmod(0o755)
        self.source = source_from_root(self.root)
        self.entry = Path(self.td.name) / "entry.py"
        self.entry.write_text("# entry")

    def tearDown(self): self.td.cleanup()

    def _exe(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text("bin"); path.chmod(0o755); return path

    def test_source_contract_detected(self):
        self.assertEqual(self.source.root, self.root.resolve())
        self.assertTrue(self.source.sim_vehicle.is_file())

    def test_invalid_source_rejected(self):
        with self.assertRaises(CoreError): source_from_root(Path(self.td.name)/"nope")

    def test_standalone_jsbsim_preferred_over_venv(self):
        standalone=self._exe(self.home/"jsbsim-source/build/src/JSBSim")
        self._exe(self.root/".venv/bin/JSBSim")
        self.assertEqual(choose_jsbsim(self.source, home=self.home), standalone.resolve())

    def test_jsbsim_env_override_has_priority(self):
        override=self._exe(Path(self.td.name)/"special/JSBSim")
        self._exe(self.home/"jsbsim-source/build/src/JSBSim")
        with patch.dict(os.environ, {"OMNI_JSBSIM_BIN": str(override)}):
            self.assertEqual(choose_jsbsim(self.source, home=self.home), override.resolve())

    def test_legacy_launcher_risks_are_detected(self):
        a=audit_legacy_run_demo(self.source)
        self.assertTrue(a.uses_project_wide_pkill)
        self.assertTrue(a.mutates_model_on_start)
        self.assertTrue(a.starts_mission_loader)
        self.assertTrue(a.has_cleanup_trap)

    def test_model_mismatch_is_warning_not_silent_copy(self):
        self.source.model_src.write_text("new")
        self.source.model_dst.write_text("old")
        checks=validate_source(self.source)
        item=next(x for x in checks if x.name=="model source == installed copy")
        self.assertFalse(item.ok); self.assertEqual(item.severity,"warning")

    def test_plan_never_calls_legacy_run_demo(self):
        jsb=self._exe(self.home/"jsbsim-source/build/src/JSBSim")
        p=build_initial_plan(self.source, entry_script=self.entry, jsbsim=jsb)
        joined=" ".join(p.entry_argv)
        self.assertNotIn("run_demo.sh", joined)
        self.assertNotIn("pkill", joined)
        self.assertIn("jsbsim:Omni-Trainer", joined)
        self.assertIn("udp:127.0.0.1:14550", joined)
        self.assertTrue(p.mission_companion_deferred)

    def test_plan_rejects_non_loopback_output(self):
        jsb=self._exe(self.home/"jsbsim-source/build/src/JSBSim")
        with self.assertRaises(CoreError):
            build_initial_plan(self.source, entry_script=self.entry, jsbsim=jsb,
                               mavlink_outputs=("udp:0.0.0.0:14550",))

    def test_tcp_probe_is_read_only(self):
        s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(1)
        port=s.getsockname()[1]
        try: self.assertTrue(probe_tcp_port("127.0.0.1",port))
        finally: s.close()


if __name__ == "__main__": unittest.main()
