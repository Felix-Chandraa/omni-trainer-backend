from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.training_core.omni_managed_entry import build_exec


class Dev006Rev4Tests(unittest.TestCase):
    def fixture(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        ap = root / "ap"
        sim = ap / "Tools" / "autotest" / "sim_vehicle.py"
        jsb = root / "jsb" / "JSBSim"
        param = root / "omni.parm"
        py = root / ".venv" / "bin" / "python3"

        for p in (sim, jsb, param, py):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# fixture\n")
        jsb.chmod(0o755)

        a = argparse.Namespace(
            ap_dir=str(ap),
            jsbsim=str(jsb),
            vehicle="ArduPlane",
            frame="jsbsim:Omni-Trainer",
            location="Wiriadinata",
            param_file=str(param),
            enable_fgview=True,
            out=["udp:127.0.0.1:14550", "udp:127.0.0.1:14555"],
            print_plan=False,
        )
        return td, root, sim, jsb, param, py, a

    def test_effective_environment_matches_project_venv_precedence(self):
        td, root, sim, jsb, param, py, a = self.fixture()
        try:
            with patch("src.training_core.omni_managed_entry.sys.executable", str(py)):
                with patch.dict(os.environ, {"PATH": "/usr/bin", "PYTHONHOME": "/bad"}, clear=False):
                    argv, env, cwd = build_exec(a)
            self.assertEqual(argv[:2], [str(py), str(sim)])
            parts = env["PATH"].split(os.pathsep)
            self.assertEqual(parts[0], str(root / ".venv" / "bin"))
            self.assertEqual(parts[1], str(jsb.parent))
            self.assertEqual(env["VIRTUAL_ENV"], str(root / ".venv"))
            self.assertNotIn("PYTHONHOME", env)
        finally:
            td.cleanup()

    def test_raw_fg_and_loopback_outputs_remain_requested(self):
        td, root, sim, jsb, param, py, a = self.fixture()
        try:
            with patch("src.training_core.omni_managed_entry.sys.executable", str(py)):
                argv, _, _ = build_exec(a)
            self.assertIn("--enable-fgview", argv)
            self.assertIn("--out=udp:127.0.0.1:14550", argv)
            self.assertIn("--out=udp:127.0.0.1:14555", argv)
            self.assertIn(f"--add-param-file={param}", argv)
        finally:
            td.cleanup()

    def test_nonloopback_output_fails_closed(self):
        td, root, sim, jsb, param, py, a = self.fixture()
        a.out = ["udp:10.0.0.2:14550"]
        try:
            with patch("src.training_core.omni_managed_entry.sys.executable", str(py)):
                with self.assertRaises(SystemExit):
                    build_exec(a)
        finally:
            td.cleanup()

    def test_managed_entry_ast_has_no_cleanup_calls(self):
        p = Path(__file__).resolve().parents[1] / "src/training_core/omni_managed_entry.py"
        tree = ast.parse(p.read_text())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                name = f"{f.value.id}.{f.attr}"
                if name.startswith("subprocess.") or name in {
                    "os.system", "os.kill", "os.killpg", "shutil.rmtree"
                }:
                    found.append(name)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
