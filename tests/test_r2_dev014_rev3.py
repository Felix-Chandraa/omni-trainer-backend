from __future__ import annotations

from pathlib import Path
import unittest


class Dev014Rev3Tests(unittest.TestCase):
    def test_accelerated_replay_explicitly_yields(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/replay_gateway.py"
        ).read_text()
        self.assertIn("await asyncio.sleep(0)", text)

    def test_replay_outgoing_frames_are_serialized(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/replay_gateway.py"
        ).read_text()
        compact = "".join(text.split())
        self.assertIn("send_lock=asyncio.Lock()", compact)
        self.assertIn("_send_json(self,ws,send_lock", compact)
        self.assertIn("_stream(self,ws,send_lock)", compact)

    def test_acceptance_uses_separate_readonly_and_stream_connections(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/dev014_live_trial.py"
        ).read_text()
        self.assertIn("async def readonly_probe(", text)
        self.assertIn("async def stream_probe(", text)
        self.assertIn("async def run_probes(", text)

        stream_start = text.index("async def stream_probe(")
        stream_end = text.index(
            "async def run_probes(", stream_start
        )
        stream_body = text[stream_start:stream_end]
        self.assertNotIn('"type": "cmd"', stream_body)

        readonly_start = text.index("async def readonly_probe(")
        readonly_end = text.index(
            "async def stream_probe(", readonly_start
        )
        readonly_body = text[readonly_start:readonly_end]
        self.assertIn('"type": "cmd"', readonly_body)
        self.assertIn("replay_read_only", text)

    def test_replay_still_has_no_fdm_worker_launch(self):
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "src/training_core/replay_gateway.py",
            "src/training_core/replay_reader.py",
        ):
            text = (root / rel).read_text()
            self.assertNotIn("worker_argv", text)
            self.assertNotIn("AttemptWorkerCoordinator", text)
            self.assertNotIn("subprocess", text)


if __name__ == "__main__":
    unittest.main()
