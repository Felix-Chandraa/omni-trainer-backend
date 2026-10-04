from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest

from src.training_core.worker import AircraftWorker, WorkerSpec


class Dev006Rev8Tests(unittest.TestCase):
    def test_worker_keeps_stdin_open_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            code = (
                "import sys\n"
                "b=sys.stdin.buffer.read(1)\n"
                "raise SystemExit(71 if b == b'' else 72)\n"
            )
            worker = AircraftWorker(WorkerSpec(
                aircraft_id="stdin-fixture",
                argv=(sys.executable, "-c", code),
                cwd=root,
                runtime_dir=runtime,
            ))
            worker.start()
            time.sleep(0.20)
            self.assertIsNone(
                worker.poll(),
                "child saw stdin EOF; managed interactive subprocesses would exit early",
            )
            rc = worker.stop(timeout=1.0)
            self.assertIn(rc, (71, -15, -9))

    def test_worker_source_has_no_devnull_stdin(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/worker.py").read_text()
        self.assertIn("stdin=subprocess.PIPE", text)
        self.assertNotIn("stdin=subprocess.DEVNULL", text)
        self.assertIn("start_new_session=True", text)

    def test_worker_does_not_expose_stdin_write_api(self):
        forbidden = {"write_stdin", "send_stdin", "command_stdin"}
        self.assertTrue(forbidden.isdisjoint(set(dir(AircraftWorker))))


if __name__ == "__main__":
    unittest.main()
