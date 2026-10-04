from __future__ import annotations

from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest

from src.training_core.worker import AircraftWorker, WorkerSpec


def free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class Dev006Rev9Tests(unittest.TestCase):
    def test_stop_allows_eof_sensitive_child_to_exit_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code = (
                "import sys\n"
                "sys.stdin.buffer.read(1)\n"
                "raise SystemExit(0)\n"
            )
            worker = AircraftWorker(WorkerSpec(
                aircraft_id="graceful-eof",
                argv=(sys.executable, "-c", code),
                cwd=root,
                runtime_dir=root / "runtime",
            ))
            worker.start()
            time.sleep(0.15)
            self.assertIsNone(worker.poll())
            self.assertEqual(worker.stop(timeout=1.0), 0)

    def test_graceful_child_releases_owned_listener(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            port = free_tcp_port()
            code = (
                "import socket,sys\n"
                f"s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
                f"s.bind(('127.0.0.1',{port})); s.listen(1)\n"
                "sys.stdin.buffer.read(1)\n"
                "s.close()\n"
            )
            worker = AircraftWorker(WorkerSpec(
                aircraft_id="listener-eof",
                argv=(sys.executable, "-c", code),
                cwd=root,
                runtime_dir=root / "runtime",
            ))
            worker.start()

            busy = False
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    probe.bind(("127.0.0.1", port))
                    busy = False
                except OSError:
                    busy = True
                finally:
                    probe.close()
                if busy:
                    break
                time.sleep(0.02)

            self.assertTrue(busy)
            self.assertEqual(worker.stop(timeout=1.0), 0)

            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", port))
            finally:
                probe.close()

    def test_cleanup_is_part_of_dev006_acceptance(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/omni_live_trial.py").read_text()
        self.assertIn("DEV-006 ACCEPTED", text)
        self.assertIn("return 33", text)
        self.assertIn('result["live_pass"] = bool(runtime_ok and cleanup_ok)', text)
        self.assertNotIn(
            'print("STATUS: DEV-006 LIVE MANAGED STARTUP VERIFIED (engineering only)")',
            text,
        )


if __name__ == "__main__":
    unittest.main()
