from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest

from src.training_core.omni_launcher_adapter import build_initial_plan, source_from_root
from src.training_core.omni_managed_entry import build_exec


class Dev006Rev5Tests(unittest.TestCase):
    def fixture(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name) / "omni_flight-main"
        ap = root / "omnitrainer-sitl" / "ardupilot"

        files = [
            root / "run_demo.sh",
            ap / "Tools/autotest/sim_vehicle.py",
            ap / "Tools/autotest/aircraft/Omni-Trainer/omni_trainer_sitl.parm",
            root / "omnitrainer-sitl/assets/ardupilot/aircraft/Omni-Trainer/Omni-Trainer.xml",
            ap / "Tools/autotest/aircraft/Omni-Trainer/Omni-Trainer.xml",
            root / "omnitrainer-sitl/assets/missions/wiriadinata_default.txt",
            root / "omnitrainer-sitl/scripts/load_default_mission.py",
        ]
        for p in files:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fixture")

        venv_py = root / ".venv" / "bin" / "python3"
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.symlink_to(Path(sys.executable).resolve())

        entry = Path(td.name) / "entry.py"
        entry.write_text("# fixture")

        jsb = Path(td.name) / "jsbsim" / "JSBSim"
        jsb.parent.mkdir(parents=True, exist_ok=True)
        jsb.write_text("fixture")
        jsb.chmod(0o755)

        return td, root, source_from_root(root), entry, jsb, venv_py

    def test_plan_carries_explicit_venv_python(self):
        td, root, source, entry, jsb, venv_py = self.fixture()
        try:
            plan = build_initial_plan(source, entry_script=entry, jsbsim=jsb)
            argv = list(plan.entry_argv)
            i = argv.index("--venv-python")
            self.assertEqual(argv[i + 1], str(venv_py))
            self.assertEqual(argv[0], str(venv_py))
        finally:
            td.cleanup()

    def test_symlinked_venv_is_not_resolved_to_system_python(self):
        td, root, source, entry, jsb, venv_py = self.fixture()
        try:
            a = argparse.Namespace(
                ap_dir=str(source.ap_dir),
                venv_python=str(venv_py),
                jsbsim=str(jsb),
                vehicle="ArduPlane",
                frame="jsbsim:Omni-Trainer",
                location="Wiriadinata",
                param_file=str(source.omni_param),
                enable_fgview=True,
                out=["udp:127.0.0.1:14550"],
                print_plan=False,
            )
            argv, env, _ = build_exec(a)
            self.assertEqual(argv[0], str(venv_py))
            self.assertEqual(env["VIRTUAL_ENV"], str(root / ".venv"))
            self.assertEqual(env["PATH"].split(os.pathsep)[0], str(root / ".venv" / "bin"))
            self.assertNotEqual(argv[0], str(Path(sys.executable).resolve()))
        finally:
            td.cleanup()

    def test_nonloopback_still_fails_closed(self):
        td, root, source, entry, jsb, venv_py = self.fixture()
        try:
            a = argparse.Namespace(
                ap_dir=str(source.ap_dir),
                venv_python=str(venv_py),
                jsbsim=str(jsb),
                vehicle="ArduPlane",
                frame="jsbsim:Omni-Trainer",
                location="Wiriadinata",
                param_file=str(source.omni_param),
                enable_fgview=True,
                out=["udp:192.168.1.2:14550"],
                print_plan=False,
            )
            with self.assertRaises(SystemExit):
                build_exec(a)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
