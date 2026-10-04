from pathlib import Path
import unittest


class Dev015Rev7Tests(unittest.TestCase):
    def test_deadman_is_required_before_acceptance(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/dev015_live_trial.py"
        ).read_text()
        self.assertIn(
            "deadman_releases_observed = router.deadman_releases",
            text,
        )
        self.assertIn(
            "if deadman_releases_observed < 1:",
            text,
        )
        self.assertIn(
            "server deadman was not proven",
            text,
        )

    def test_deadman_metric_is_snapshotted_before_router_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/dev015_live_trial.py"
        ).read_text()
        snapshot = text.index("deadman_releases_observed = max(")
        shutdown = text.index("router.shutdown()", snapshot)
        nulled = text.index("router = None", shutdown)
        self.assertLess(snapshot, shutdown)
        self.assertLess(shutdown, nulled)
        self.assertNotIn(
            "router.deadman_releases",
            text[nulled:],
        )

    def test_existing_fg_settle_limitation_remains_visible(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/dev015_live_trial.py"
        ).read_text()
        self.assertIn("FG SETTLE BEFORE DISARM:", text)
        self.assertIn("DISARM DIAGNOSTIC:", text)


if __name__ == "__main__":
    unittest.main()
