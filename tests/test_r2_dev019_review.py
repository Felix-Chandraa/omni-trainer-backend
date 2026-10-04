from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest

from src.training_core.review_service import (
    REVIEW_API_BASE,
    REVIEW_PROTOCOL,
    ReviewError,
    ReviewManager,
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): sha256(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def make_evidence(
    base: Path,
    attempt_id: str,
    *,
    final_state: str = "Ended",
    with_actuator: bool = True,
) -> Path:
    package = base / "runtime" / "test-run" / "evidence" / attempt_id
    package.mkdir(parents=True)
    identity = {
        "session_id": "session-1",
        "exercise_id": "exercise-1",
        "attempt_id": attempt_id,
        "aircraft_id": "omni-1",
        "generation": 1,
    }
    state_rows = []
    for i, elapsed in enumerate((100_000_000, 1_100_000_000, 2_100_000_000), 1):
        state = {
            "lat_deg": -7.3 + i / 1000,
            "lon_deg": 108.2 + i / 1000,
            "alt_msl_m": 400.0 + i,
            "agl_m": 10.0 + i,
            "roll_deg": float(i),
            "pitch_deg": float(i) * 2,
            "yaw_deg": 90.0 + i,
            "vcas_mps": 20.0 + i,
        }
        if with_actuator:
            state.update(
                {
                    "srv1": 1400 + i,
                    "srv2": 1500 + i,
                    "srv3": 1600 + i,
                    "srv4": 1700 + i,
                    "throttle": 30 + i,
                }
            )
        state_rows.append(
            {
                "schema_version": "omni.evidence.v1",
                "identity": identity,
                "sequence": i,
                "time": {
                    "elapsed_monotonic_ns": elapsed,
                    "server_utc_ns": 1_800_000_000_000_000_000 + elapsed,
                },
                "kind": "state_sample",
                "payload": {"state": state, "source": "FGNetFDM"},
            }
        )
    lifecycle_rows = [
        {
            "schema_version": "omni.evidence.v1",
            "identity": identity,
            "sequence": 1,
            "time": {"elapsed_monotonic_ns": 100_000_000},
            "kind": "lifecycle",
            "payload": {"state": "Setup", "revision": 0},
        },
        {
            "schema_version": "omni.evidence.v1",
            "identity": identity,
            "sequence": 2,
            "time": {"elapsed_monotonic_ns": 1_600_000_000},
            "kind": "lifecycle",
            "payload": {"state": final_state, "revision": 4},
        },
    ]
    state_path = package / "server" / "state.000001.jsonl"
    life_path = package / "server" / "lifecycle.000001.jsonl"
    write_jsonl(state_path, state_rows)
    write_jsonl(life_path, lifecycle_rows)
    manifest = {
        "schema_version": "omni.evidence.v1",
        "status": "finalized",
        "identity": identity,
        "configuration_snapshot": {"scenario": "MAN-LDG-004-v1.0"},
        "final_status": "complete",
        "overall_complete": True,
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    files = []
    for path in (state_path, life_path):
        files.append(
            {
                "path": str(path.relative_to(package)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    index = {
        "schema_version": "omni.evidence.v1",
        "status": "complete",
        "identity": identity,
        "duration_elapsed_ns": 2_100_000_000,
        "channels": {
            "server/state": {
                "segments": ["server/state.000001.jsonl"],
                "records": 3,
                "dropped_records": 0,
                "complete": True,
            },
            "server/lifecycle": {
                "segments": ["server/lifecycle.000001.jsonl"],
                "records": 2,
                "dropped_records": 0,
                "complete": True,
            },
        },
        "files": files,
        "overall_complete": True,
    }
    (package / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return package


def request(request_id: str, **extra) -> dict:
    return {"protocol": REVIEW_PROTOCOL, "request_id": request_id, **extra}


class Dev019ReviewTests(unittest.TestCase):
    def test_catalog_selects_ended_evidence_and_rejects_live_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            make_evidence(base, "ended-attempt", final_state="Ended")
            make_evidence(base, "live-attempt", final_state="Active")
            manager = ReviewManager(base)
            listed = manager.list(request("list-1"))["result"]["attempts"]
            by_id = {x["attempt_id"]: x for x in listed}
            self.assertTrue(by_id["ended-attempt"]["reviewable"])
            self.assertEqual(by_id["ended-attempt"]["state"], "Ended")
            self.assertFalse(by_id["live-attempt"]["reviewable"])
            self.assertEqual(by_id["live-attempt"]["error_code"], "ATTEMPT_NOT_ENDED")
            with self.assertRaises(ReviewError) as cm:
                manager.open(request("open-live", attempt_id="live-attempt"))
            self.assertEqual(cm.exception.code, "ATTEMPT_NOT_ENDED")

    def test_open_seek_speed_step_event_jump_and_close_are_cursor_only(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            package = make_evidence(base, "ended-attempt")
            before = tree_hash(package)
            manager = ReviewManager(base)
            opened = manager.open(request("open-1", attempt_id="ended-attempt"))["result"]
            review = opened["review"]
            snap = opened["snapshot"]
            self.assertTrue(review["read_only"])
            self.assertFalse(review["controls"]["flight_commands"])
            self.assertFalse(review["controls"]["authority"])
            self.assertEqual(snap["frame"]["source_sequence"], 1)
            self.assertEqual(snap["frame"]["actuator"]["srv3"], 1601)

            rid = review["review_id"]
            rev = review["review_revision"]
            sought = manager.seek(
                request("seek-1", review_id=rid, review_revision=rev, cursor_ns=1_500_000_000)
            )["result"]
            self.assertEqual(sought["frame"]["source_sequence"], 2)

            sped = manager.speed(
                request("speed-1", review_id=rid, review_revision=sought["review_revision"], speed=2.0)
            )["result"]
            self.assertEqual(sped["speed"], 2.0)

            stepped = manager.step(
                request("step-1", review_id=rid, review_revision=sped["review_revision"], direction="next")
            )["result"]
            self.assertEqual(stepped["frame"]["source_sequence"], 3)

            jumped = manager.event_jump(
                request("event-1", review_id=rid, review_revision=stepped["review_revision"], direction="previous")
            )["result"]
            self.assertEqual(jumped["event"]["channel"], "server/lifecycle")

            closed = manager.close(
                request("close-1", review_id=rid, review_revision=jumped["review_revision"])
            )["result"]
            self.assertTrue(closed["closed"])
            self.assertFalse(closed["live_attempt_modified"])
            self.assertFalse(closed["authority_modified"])
            self.assertEqual(before, tree_hash(package))

    def test_missing_actuator_is_explicit_degraded_and_never_fabricated(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            package = make_evidence(base, "no-actuator", with_actuator=False)
            before = tree_hash(package)
            manager = ReviewManager(base)
            result = manager.open(request("open-degraded", attempt_id="no-actuator"))["result"]
            self.assertIn("actuator", result["review"]["degraded_channels"])
            actuator = result["snapshot"]["frame"]["actuator"]
            self.assertEqual(actuator["availability"], "unavailable")
            self.assertIsNone(actuator["srv1"])
            self.assertIsNone(actuator["srv3"])
            self.assertTrue(result["snapshot"]["gaps"])
            self.assertEqual(before, tree_hash(package))


    def test_actuator_overlay_uses_external_cache_and_keeps_source_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            package = make_evidence(base, "overlay-attempt", with_actuator=False)
            tlog = package / "server" / "aircraft.tlog"
            tlog.write_bytes(b"recorded-tlog-placeholder")
            index_path = package / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["mavlink_tlog"] = {"path": "server/aircraft.tlog", "complete": True}
            index["files"].append({
                "path": "server/aircraft.tlog",
                "bytes": tlog.stat().st_size,
                "sha256": sha256(tlog),
            })
            index_path.write_text(json.dumps(index), encoding="utf-8")
            before = tree_hash(package)

            def fake_overlay_builder(source: Path, runtime_root: Path) -> Path:
                out = runtime_root / "derived"
                shutil.copytree(source, out)
                state_path = out / "server" / "state.000001.jsonl"
                rows = [json.loads(x) for x in state_path.read_text(encoding="utf-8").splitlines() if x]
                for i, row in enumerate(rows, 1):
                    row["payload"]["state"].update({
                        "srv1": 1410 + i,
                        "srv2": 1510 + i,
                        "srv3": 1610 + i,
                        "srv4": 1710 + i,
                        "throttle": 40 + i,
                    })
                write_jsonl(state_path, rows)
                overlay_index_path = out / "index.json"
                overlay_index = json.loads(overlay_index_path.read_text(encoding="utf-8"))
                for meta in overlay_index["files"]:
                    if meta["path"] == "server/state.000001.jsonl":
                        meta["bytes"] = state_path.stat().st_size
                        meta["sha256"] = sha256(state_path)
                overlay_index_path.write_text(json.dumps(overlay_index), encoding="utf-8")
                return out

            manager = ReviewManager(base, overlay_builder=fake_overlay_builder)
            result = manager.open(request("open-overlay", attempt_id="overlay-attempt"))["result"]
            self.assertEqual(result["review"]["actuator_source"], "derived_tlog_overlay")
            self.assertEqual(result["snapshot"]["frame"]["actuator"]["srv3"], 1611)
            self.assertEqual(before, tree_hash(package))
            cache = base / "runtime" / "dev019-review-cache"
            self.assertTrue(any(cache.rglob("review-cache.json")))

    def test_stale_review_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            make_evidence(base, "ended-attempt")
            manager = ReviewManager(base)
            opened = manager.open(request("open-1", attempt_id="ended-attempt"))["result"]
            rid = opened["review"]["review_id"]
            rev = opened["review"]["review_revision"]
            manager.seek(request("seek-1", review_id=rid, review_revision=rev, cursor_ns=0))
            with self.assertRaises(ReviewError) as cm:
                manager.pause(request("pause-stale", review_id=rid, review_revision=rev))
            self.assertEqual(cm.exception.code, "STALE_REVIEW_REVISION")

    def test_corrupt_evidence_is_catalogued_as_invalid_and_cannot_open(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            package = make_evidence(base, "broken-attempt")
            state_path = package / "server" / "state.000001.jsonl"
            state_path.write_text(state_path.read_text() + "{}\n", encoding="utf-8")
            manager = ReviewManager(base)
            listed = manager.list(request("list-bad"))["result"]["attempts"]
            entry = next(x for x in listed if x["attempt_id"] == "broken-attempt")
            self.assertFalse(entry["reviewable"])
            self.assertEqual(entry["error_code"], "EVIDENCE_INVALID")
            with self.assertRaises(ReviewError) as cm:
                manager.open(request("open-bad", attempt_id="broken-attempt"))
            self.assertEqual(cm.exception.code, "EVIDENCE_INVALID")

    def test_review_source_has_no_live_io_worker_or_legacy_bus_dependency(self):
        root = Path(__file__).resolve().parents[1]
        review = (root / "src/training_core/review_service.py").read_text(encoding="utf-8")
        for forbidden in (
            "AttemptWorkerCoordinator",
            "worker_argv",
            "FlightCommandRouter",
            "TelemetryBus",
            "ReplayGateway",
            "websockets",
            "recv_match(",
            "rc_channels_override_send",
            "command_long_send",
        ):
            self.assertNotIn(forbidden, review)

    def test_r4a_exposes_versioned_review_capability_without_new_host_or_port(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/instructor_console_r4a.py").read_text(encoding="utf-8")
        self.assertIn("DEV019_REVIEW_CANONICAL_API", text)
        self.assertIn("REVIEW_API_BASE", text)
        self.assertIn("REVIEW_PROTOCOL", text)
        self.assertNotIn("9200", text)
        self.assertEqual(REVIEW_API_BASE, "/api/review/v1")


if __name__ == "__main__":
    unittest.main()
