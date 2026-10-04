"""Canonical read-only Instructor Review service for stored Attempt evidence.

DEV-019 Review deliberately stays outside live vehicle I/O. It consumes finalized
Attempt evidence through ReplayPackage, keeps a server-side replay cursor, and
may derive an actuator-enriched cache outside the source evidence package.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
from typing import Any, Callable

from .replay_reader import ReplayError, ReplayFrame, ReplayPackage


REVIEW_PROTOCOL = "omni.instructor.review.v1"
REVIEW_API_BASE = "/api/review/v1"
REVIEW_CACHE_SCHEMA = "omni.instructor.review-cache.v1"
_ALLOWED_SPEEDS = (0.5, 1.0, 2.0)


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.http_status = int(http_status)


@dataclass(frozen=True)
class ReviewCatalogEntry:
    evidence_id: str
    attempt_id: str
    session_id: str | None
    exercise_id: str | None
    aircraft_id: str | None
    generation: int | None
    duration_ns: int
    frame_count: int
    source_index_sha256: str
    package_root: Path
    reviewable: bool
    state: str
    error_code: str | None = None
    error: str | None = None
    scenario: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "attempt_id": self.attempt_id,
            "session_id": self.session_id,
            "exercise_id": self.exercise_id,
            "aircraft_id": self.aircraft_id,
            "generation": self.generation,
            "duration_ns": self.duration_ns,
            "duration_s": self.duration_ns / 1_000_000_000,
            "frame_count": self.frame_count,
            "source_revision": self.source_index_sha256,
            "reviewable": self.reviewable,
            "state": self.state,
            "error_code": self.error_code,
            "error": self.error,
            "scenario": self.scenario,
            "read_only": True,
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _tree_sha256(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = _sha256(path)
    return result


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _state_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "unknown")


def _actuator_from_frame(frame: ReplayFrame) -> dict[str, Any]:
    t = frame.telemetry
    values = {name: t.get(name) for name in ("srv1", "srv2", "srv3", "srv4", "throttle")}
    present = any(value is not None for value in values.values())
    return {
        **values,
        "availability": "recorded" if present else "unavailable",
        "source": "stored_state" if present else None,
    }


def _has_actuator_data(package: ReplayPackage) -> bool:
    return any(_actuator_from_frame(frame)["availability"] == "recorded" for frame in package.frames)


class ReviewCatalog:
    """Server-owned mapping from review IDs to trusted evidence packages.

    The client never supplies a filesystem path. Only evidence found below the
    configured R2 runtime root can enter the catalog.
    """

    def __init__(self, base: Path, store: Any | None = None):
        self.base = Path(base).expanduser().resolve()
        self.runtime_root = (self.base / "runtime").resolve()
        self.store = store
        self._entries: dict[str, ReviewCatalogEntry] = {}
        self._by_attempt: dict[str, list[str]] = {}
        self._catalog_revision = "empty"

    @property
    def catalog_revision(self) -> str:
        return self._catalog_revision

    def _store_state(self, attempt_id: str) -> str | None:
        if self.store is None:
            return None
        try:
            return _state_text(self.store.attempt(attempt_id).state)
        except Exception:
            return None

    @staticmethod
    def _evidence_lifecycle_state(package: ReplayPackage) -> str:
        last = None
        for event in package.timeline_events():
            if event.get("channel") != "server/lifecycle":
                continue
            payload = event.get("payload") or {}
            if isinstance(payload, dict) and payload.get("state") is not None:
                last = str(payload.get("state"))
        return last or "unknown"

    def _scenario(self, root: Path) -> str | None:
        manifest = _safe_json(root / "manifest.json")
        cfg = manifest.get("configuration_snapshot") or {}
        if not isinstance(cfg, dict):
            return None
        value = cfg.get("scenario") or cfg.get("scenario_version")
        return str(value) if value else None

    def _candidate_indexes(self) -> list[Path]:
        if not self.runtime_root.is_dir():
            return []
        out = []
        for path in self.runtime_root.rglob("index.json"):
            parts = path.relative_to(self.runtime_root).parts
            if "dev019-review-cache" in parts:
                continue
            if "evidence" not in parts:
                continue
            out.append(path.resolve())
        return sorted(set(out))

    def refresh(self) -> list[ReviewCatalogEntry]:
        entries: dict[str, ReviewCatalogEntry] = {}
        by_attempt: dict[str, list[str]] = {}
        revision_material: list[str] = []

        for index_path in self._candidate_indexes():
            root = index_path.parent
            try:
                if os.path.commonpath((str(root), str(self.runtime_root))) != str(self.runtime_root):
                    continue
            except ValueError:
                continue

            index_sha = _sha256(index_path)
            raw_index = _safe_json(index_path)
            raw_identity = raw_index.get("identity") or {}
            attempt_id = str(raw_identity.get("attempt_id") or root.name)
            evidence_id = hashlib.sha256(
                f"{attempt_id}\0{index_sha}".encode("utf-8")
            ).hexdigest()[:24]

            try:
                package = ReplayPackage(root)
                identity = package.identity
                evidence_state = self._evidence_lifecycle_state(package)
                store_state = self._store_state(attempt_id)
                state = store_state or evidence_state
                ended = str(state).lower() == "ended"
                entry = ReviewCatalogEntry(
                    evidence_id=evidence_id,
                    attempt_id=str(identity.get("attempt_id") or attempt_id),
                    session_id=(str(identity.get("session_id")) if identity.get("session_id") else None),
                    exercise_id=(str(identity.get("exercise_id")) if identity.get("exercise_id") else None),
                    aircraft_id=(str(identity.get("aircraft_id")) if identity.get("aircraft_id") else None),
                    generation=(int(identity.get("generation")) if identity.get("generation") is not None else None),
                    duration_ns=package.duration_ns,
                    frame_count=len(package.frames),
                    source_index_sha256=index_sha,
                    package_root=root,
                    reviewable=ended,
                    state=state,
                    error_code=None if ended else "ATTEMPT_NOT_ENDED",
                    error=None if ended else "Attempt lifecycle is not Ended",
                    scenario=self._scenario(root),
                )
            except Exception as exc:
                identity = raw_identity if isinstance(raw_identity, dict) else {}
                entry = ReviewCatalogEntry(
                    evidence_id=evidence_id,
                    attempt_id=str(identity.get("attempt_id") or attempt_id),
                    session_id=(str(identity.get("session_id")) if identity.get("session_id") else None),
                    exercise_id=(str(identity.get("exercise_id")) if identity.get("exercise_id") else None),
                    aircraft_id=(str(identity.get("aircraft_id")) if identity.get("aircraft_id") else None),
                    generation=(int(identity.get("generation")) if identity.get("generation") is not None else None),
                    duration_ns=int(raw_index.get("duration_elapsed_ns") or 0),
                    frame_count=0,
                    source_index_sha256=index_sha,
                    package_root=root,
                    reviewable=False,
                    state=self._store_state(attempt_id) or "unknown",
                    error_code="EVIDENCE_INVALID",
                    error=str(exc),
                    scenario=self._scenario(root),
                )

            entries[evidence_id] = entry
            by_attempt.setdefault(entry.attempt_id, []).append(evidence_id)
            revision_material.append(f"{evidence_id}:{entry.state}:{entry.reviewable}")

        self._entries = entries
        self._by_attempt = by_attempt
        material = "\n".join(sorted(revision_material)).encode("utf-8")
        self._catalog_revision = hashlib.sha256(material).hexdigest()[:16]
        return sorted(
            entries.values(),
            key=lambda x: (x.reviewable, x.attempt_id, x.evidence_id),
            reverse=True,
        )

    def resolve(self, *, evidence_id: str | None, attempt_id: str | None) -> ReviewCatalogEntry:
        self.refresh()
        if evidence_id:
            entry = self._entries.get(str(evidence_id))
            if entry is None:
                raise ReviewError("EVIDENCE_NOT_FOUND", "Evidence selection is not in the server Review catalog", http_status=404)
        elif attempt_id:
            ids = self._by_attempt.get(str(attempt_id), [])
            if not ids:
                raise ReviewError("EVIDENCE_NOT_FOUND", "No catalogued evidence exists for this Attempt", http_status=404)
            candidates = [self._entries[x] for x in ids]
            candidates.sort(key=lambda x: (x.reviewable, x.source_index_sha256), reverse=True)
            entry = candidates[0]
        else:
            raise ReviewError("INVALID_REQUEST", "attempt_id or evidence_id is required", http_status=400)

        if not entry.reviewable:
            code = entry.error_code or "ATTEMPT_NOT_ENDED"
            raise ReviewError(code, entry.error or "Evidence is not reviewable")
        return entry


class ReviewSession:
    def __init__(
        self,
        package: ReplayPackage,
        entry: ReviewCatalogEntry,
        *,
        reviewer_actor_id: str,
        actuator_source: str,
        degraded_channels: list[str],
        source_integrity: dict[str, str],
    ):
        self.review_id = str(uuid.uuid4())
        self.package = package
        self.entry = entry
        self.reviewer_actor_id = reviewer_actor_id
        self.actuator_source = actuator_source
        self.degraded_channels = list(dict.fromkeys(degraded_channels))
        self.source_integrity = dict(source_integrity)
        self.cursor_ns = 0
        self.playback_state = "paused"
        self.speed = 1.0
        self.revision = 1
        self.opened_utc_ns = time.time_ns()
        self._anchor_cursor_ns = 0
        self._anchor_mono_ns = time.monotonic_ns()
        self._relative_frames = [frame.relative_ns for frame in self.package.frames]
        first_elapsed = self.package.frames[0].elapsed_ns
        events = []
        for i, event in enumerate(self.package.timeline_events()):
            rel = int(event["elapsed_ns"]) - first_elapsed
            if rel < 0:
                rel = 0
            events.append({**event, "event_index": i, "relative_ns": rel})
        self.events = sorted(events, key=lambda x: (x["relative_ns"], x["event_index"]))
        self._event_times = [x["relative_ns"] for x in self.events]

    def _advance(self) -> None:
        if self.playback_state != "playing":
            return
        delta = max(0, time.monotonic_ns() - self._anchor_mono_ns)
        cursor = self._anchor_cursor_ns + int(delta * self.speed)
        if cursor >= self.package.duration_ns:
            self.cursor_ns = self.package.duration_ns
            self.playback_state = "paused"
            self._anchor_cursor_ns = self.cursor_ns
            self._anchor_mono_ns = time.monotonic_ns()
        else:
            self.cursor_ns = cursor

    def _frame_index(self) -> int:
        self._advance()
        idx = bisect_right(self._relative_frames, self.cursor_ns) - 1
        return max(0, min(idx, len(self.package.frames) - 1))

    def _frame_public(self) -> dict[str, Any]:
        idx = self._frame_index()
        frame = self.package.frames[idx]
        telemetry = dict(frame.telemetry)
        actuator = _actuator_from_frame(frame)
        if actuator["availability"] == "recorded":
            actuator["source"] = self.actuator_source
        return {
            "frame_index": idx,
            "source_sequence": frame.source_sequence,
            "source_elapsed_ns": frame.elapsed_ns,
            "relative_ns": frame.relative_ns,
            "cursor_ns": self.cursor_ns,
            "pose": {
                "lat": telemetry.get("lat"),
                "lon": telemetry.get("lon"),
                "alt": telemetry.get("alt"),
                "alt_msl": telemetry.get("alt_msl"),
                "agl": telemetry.get("agl"),
                "roll": telemetry.get("roll"),
                "pitch": telemetry.get("pitch"),
                "yaw": telemetry.get("yaw"),
                "hdg": telemetry.get("hdg"),
                "as": telemetry.get("as"),
                "gs": telemetry.get("gs"),
                "pose_source": "stored_server_state",
            },
            # ReplayReader's legacy Feather adapter intentionally labels its
            # transport as REPLAY and historically defaulted armed=False. Those
            # are presentation defaults, not measured historical evidence. Do
            # not surface them as recorded aircraft truth in Instructor Review.
            "aircraft_state": {
                "armed": None,
                "mode": None,
                "availability": "unavailable",
            },
            "actuator": actuator,
            "interpolation": "measured_frame",
        }

    def metadata(self) -> dict[str, Any]:
        channels = []
        for name, meta in sorted((self.package.index.get("channels") or {}).items()):
            item = {"name": name}
            if isinstance(meta, dict):
                item.update({
                    "complete": bool(meta.get("complete", False)),
                    "records": meta.get("records"),
                    "dropped_records": meta.get("dropped_records"),
                })
            channels.append(item)
        return {
            "review_id": self.review_id,
            "review_revision": self.revision,
            "source_attempt_id": self.entry.attempt_id,
            "evidence_id": self.entry.evidence_id,
            "source_revision": self.entry.source_index_sha256,
            "reviewer_actor_id": self.reviewer_actor_id,
            "read_only": True,
            "source_state": "Ended",
            "duration_ns": self.package.duration_ns,
            "duration_s": self.package.duration_s,
            "frame_count": len(self.package.frames),
            "event_count": len(self.events),
            "channels": channels,
            "integrity": {
                "verified_files": self.package.integrity.verified_files,
                "warnings": list(self.package.integrity.warnings),
            },
            "degraded_channels": list(self.degraded_channels),
            "actuator_source": self.actuator_source,
            "controls": {
                "play": True,
                "pause": True,
                "seek": True,
                "speed": list(_ALLOWED_SPEEDS),
                "fine_step": True,
                "event_jump": True,
                "flight_commands": False,
                "payload_commands": False,
                "authority": False,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        frame = self._frame_public()
        return {
            "review_id": self.review_id,
            "review_revision": self.revision,
            "playback_state": self.playback_state,
            "speed": self.speed,
            "cursor_ns": self.cursor_ns,
            "duration_ns": self.package.duration_ns,
            "frame": frame,
            "gaps": [
                {
                    "channel": channel,
                    "kind": "channel_unavailable",
                    "message": f"{channel} evidence unavailable; no value was inferred",
                }
                for channel in self.degraded_channels
            ],
        }

    def _require_revision(self, expected: Any) -> None:
        try:
            expected_int = int(expected)
        except Exception as exc:
            raise ReviewError("INVALID_REQUEST", "review_revision is required", http_status=400) from exc
        if expected_int != self.revision:
            raise ReviewError(
                "STALE_REVIEW_REVISION",
                f"stale Review revision: expected {self.revision}, received {expected_int}",
            )

    def _changed(self) -> None:
        self.revision += 1
        self._anchor_cursor_ns = self.cursor_ns
        self._anchor_mono_ns = time.monotonic_ns()

    def play(self, expected_revision: Any, speed: Any | None = None) -> dict[str, Any]:
        self._require_revision(expected_revision)
        self._advance()
        if speed is not None:
            self._set_speed_value(speed)
        self.playback_state = "playing"
        self._changed()
        return self.snapshot()

    def pause(self, expected_revision: Any) -> dict[str, Any]:
        self._require_revision(expected_revision)
        self._advance()
        self.playback_state = "paused"
        self._changed()
        return self.snapshot()

    def _set_speed_value(self, speed: Any) -> None:
        try:
            value = float(speed)
        except Exception as exc:
            raise ReviewError("INVALID_SPEED", "Replay speed must be 0.5, 1, or 2") from exc
        if value not in _ALLOWED_SPEEDS:
            raise ReviewError("INVALID_SPEED", "Replay speed must be 0.5, 1, or 2")
        self.speed = value

    def set_speed(self, expected_revision: Any, speed: Any) -> dict[str, Any]:
        self._require_revision(expected_revision)
        self._advance()
        self._set_speed_value(speed)
        self._changed()
        return self.snapshot()

    def seek(self, expected_revision: Any, cursor_ns: Any) -> dict[str, Any]:
        self._require_revision(expected_revision)
        try:
            value = int(cursor_ns)
        except Exception as exc:
            raise ReviewError("INVALID_REQUEST", "cursor_ns must be an integer", http_status=400) from exc
        if value < 0 or value > self.package.duration_ns:
            raise ReviewError("CURSOR_OUT_OF_RANGE", "Replay cursor is outside the evidence timeline")
        self.cursor_ns = value
        self._changed()
        return self.snapshot()

    def step(self, expected_revision: Any, direction: Any) -> dict[str, Any]:
        self._require_revision(expected_revision)
        self._advance()
        self.playback_state = "paused"
        idx = self._frame_index()
        step = 1 if str(direction).lower() in {"1", "next", "forward"} else -1 if str(direction).lower() in {"-1", "prev", "previous", "back"} else 0
        if step == 0:
            raise ReviewError("INVALID_REQUEST", "direction must be next or previous", http_status=400)
        idx = max(0, min(idx + step, len(self.package.frames) - 1))
        self.cursor_ns = self.package.frames[idx].relative_ns
        self._changed()
        return self.snapshot()

    def event_jump(self, expected_revision: Any, direction: Any) -> dict[str, Any]:
        self._require_revision(expected_revision)
        if not self.events:
            raise ReviewError("CHANNEL_UNAVAILABLE", "No timeline events are available")
        self._advance()
        self.playback_state = "paused"
        key = str(direction).lower()
        if key in {"next", "forward", "1"}:
            idx = bisect_right(self._event_times, self.cursor_ns)
            idx = min(idx, len(self.events) - 1)
        elif key in {"prev", "previous", "back", "-1"}:
            idx = bisect_left(self._event_times, self.cursor_ns) - 1
            idx = max(0, idx)
        else:
            raise ReviewError("INVALID_REQUEST", "direction must be next or previous", http_status=400)
        event = self.events[idx]
        self.cursor_ns = max(0, min(int(event["relative_ns"]), self.package.duration_ns))
        self._changed()
        result = self.snapshot()
        result["event"] = event
        return result


class ReviewManager:
    """Read-only Review sessions, independent from live Attempt authority."""

    def __init__(
        self,
        base: Path,
        store: Any | None = None,
        *,
        reviewer_actor_id: str = "dev018-instructor",
        audit: Callable[[str, dict[str, Any]], None] | None = None,
        overlay_builder: Callable[[Path, Path], Path] | None = None,
    ):
        self.base = Path(base).expanduser().resolve()
        self.store = store
        self.reviewer_actor_id = reviewer_actor_id
        self.audit = audit
        self.catalog = ReviewCatalog(self.base, store)
        self.cache_root = self.base / "runtime" / "dev019-review-cache"
        self._overlay_builder = overlay_builder
        self._sessions: dict[str, ReviewSession] = {}
        self._lock = threading.RLock()

    def _audit(self, action: str, data: dict[str, Any]) -> None:
        if self.audit is not None:
            try:
                self.audit(action, data)
            except Exception:
                pass

    @staticmethod
    def _request(body: dict[str, Any]) -> str:
        if body.get("protocol") != REVIEW_PROTOCOL:
            raise ReviewError("PROTOCOL_MISMATCH", f"protocol must be {REVIEW_PROTOCOL}", http_status=400)
        request_id = str(body.get("request_id") or "").strip()
        if not request_id or len(request_id) > 128:
            raise ReviewError("INVALID_REQUEST", "request_id is required", http_status=400)
        return request_id

    @staticmethod
    def _envelope(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "protocol": REVIEW_PROTOCOL,
            "request_id": request_id,
            "result": result,
        }

    def _source_snapshot(self, root: Path) -> dict[str, str]:
        return _tree_sha256(root)

    def _cache_overlay(self, source: Path, entry: ReviewCatalogEntry) -> tuple[ReplayPackage, str, list[str]]:
        source_package = ReplayPackage(source)
        if _has_actuator_data(source_package):
            return source_package, "stored_state", []

        tlog_meta = source_package.index.get("mavlink_tlog") or {}
        tlog_rel = tlog_meta.get("path") if isinstance(tlog_meta, dict) else None
        tlog = source / (str(tlog_rel) if tlog_rel else "server/aircraft.tlog")
        if not tlog.is_file() or tlog.stat().st_size <= 0:
            return source_package, "unavailable", ["actuator"]

        cache_dir = self.cache_root / entry.source_index_sha256
        overlay_dir = cache_dir / "overlay"
        marker = cache_dir / "review-cache.json"
        if marker.is_file() and overlay_dir.is_dir():
            meta = _safe_json(marker)
            if meta.get("source_index_sha256") == entry.source_index_sha256:
                try:
                    package = ReplayPackage(overlay_dir)
                    if _has_actuator_data(package):
                        return package, "derived_tlog_overlay", []
                except Exception:
                    pass

        before = self._source_snapshot(source)
        tmp_parent = self.cache_root / f".build-{uuid.uuid4()}"
        tmp_parent.mkdir(parents=True, exist_ok=False)
        built = None
        try:
            builder = self._overlay_builder
            if builder is None:
                from .dev017_replay_overlay import build_overlay
                builder = build_overlay
            built = Path(builder(source, tmp_parent)).resolve()
            after = self._source_snapshot(source)
            if before != after:
                raise ReviewError("SOURCE_HASH_MISMATCH", "Source evidence changed while deriving Review overlay")
            package = ReplayPackage(built)
            if not _has_actuator_data(package):
                raise ReviewError("CHANNEL_UNAVAILABLE", "Derived overlay contains no recorded actuator data")

            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            os.replace(built, overlay_dir)
            marker.write_text(
                json.dumps(
                    {
                        "schema": REVIEW_CACHE_SCHEMA,
                        "source_attempt_id": entry.attempt_id,
                        "source_index_sha256": entry.source_index_sha256,
                        "overlay_index_sha256": _sha256(overlay_dir / "index.json"),
                        "created_utc_ns": time.time_ns(),
                        "immutable_source": True,
                        "fdm_rerun": False,
                        "actuator_fields": ["srv1", "srv2", "srv3", "srv4", "throttle"],
                    },
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
            return ReplayPackage(overlay_dir), "derived_tlog_overlay", []
        except ReviewError:
            raise
        except Exception as exc:
            after = self._source_snapshot(source)
            if before != after:
                raise ReviewError("SOURCE_HASH_MISMATCH", "Source evidence changed while actuator overlay failed") from exc
            self._audit("review_actuator_overlay_degraded", {"attempt_id": entry.attempt_id, "error": str(exc)})
            return source_package, "unavailable", ["actuator"]
        finally:
            if tmp_parent.exists():
                shutil.rmtree(tmp_parent, ignore_errors=True)

    def _session(self, review_id: Any) -> ReviewSession:
        key = str(review_id or "")
        session = self._sessions.get(key)
        if session is None:
            raise ReviewError("REVIEW_CLOSED", "Review session is closed or unknown", http_status=404)
        return session

    def list(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = self._request(body)
        entries = self.catalog.refresh()
        return self._envelope(request_id, {
            "catalog_revision": self.catalog.catalog_revision,
            "attempts": [entry.public() for entry in entries],
            "read_only": True,
        })

    def open(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = self._request(body)
        entry = self.catalog.resolve(
            evidence_id=(str(body.get("evidence_id")) if body.get("evidence_id") else None),
            attempt_id=(str(body.get("attempt_id")) if body.get("attempt_id") else None),
        )
        with self._lock:
            source_snapshot = self._source_snapshot(entry.package_root)
            package, actuator_source, degraded = self._cache_overlay(entry.package_root, entry)
            if self._source_snapshot(entry.package_root) != source_snapshot:
                raise ReviewError("SOURCE_HASH_MISMATCH", "Source evidence changed during Review open")
            session = ReviewSession(
                package,
                entry,
                reviewer_actor_id=self.reviewer_actor_id,
                actuator_source=actuator_source,
                degraded_channels=degraded,
                source_integrity=source_snapshot,
            )
            self._sessions[session.review_id] = session
        self._audit("review_opened", {
            "review_id": session.review_id,
            "attempt_id": entry.attempt_id,
            "evidence_id": entry.evidence_id,
            "source_revision": entry.source_index_sha256,
            "actuator_source": actuator_source,
            "degraded_channels": degraded,
        })
        result = {"review": session.metadata(), "snapshot": session.snapshot()}
        return self._envelope(request_id, result)

    def snapshot(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = self._request(body)
        with self._lock:
            result = self._session(body.get("review_id")).snapshot()
        return self._envelope(request_id, result)

    def _mutate(self, body: dict[str, Any], action: str) -> dict[str, Any]:
        request_id = self._request(body)
        with self._lock:
            session = self._session(body.get("review_id"))
            revision = body.get("review_revision")
            if action == "play":
                result = session.play(revision, body.get("speed"))
            elif action == "pause":
                result = session.pause(revision)
            elif action == "speed":
                result = session.set_speed(revision, body.get("speed"))
            elif action == "seek":
                result = session.seek(revision, body.get("cursor_ns"))
            elif action == "step":
                result = session.step(revision, body.get("direction"))
            elif action == "event-jump":
                result = session.event_jump(revision, body.get("direction"))
            else:
                raise ReviewError("INVALID_REQUEST", f"unknown Review operation: {action}", http_status=404)
        self._audit(f"review_{action}", {
            "review_id": session.review_id,
            "attempt_id": session.entry.attempt_id,
            "review_revision": result.get("review_revision"),
            "cursor_ns": result.get("cursor_ns"),
        })
        return self._envelope(request_id, result)

    def play(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(body, "play")

    def pause(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(body, "pause")

    def speed(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(body, "speed")

    def seek(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(body, "seek")

    def step(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(body, "step")

    def event_jump(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(body, "event-jump")

    def close(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = self._request(body)
        with self._lock:
            session = self._session(body.get("review_id"))
            session._require_revision(body.get("review_revision"))
            session.pause(session.revision)
            review_id = session.review_id
            attempt_id = session.entry.attempt_id
            self._sessions.pop(review_id, None)
        self._audit("review_closed", {"review_id": review_id, "attempt_id": attempt_id})
        return self._envelope(request_id, {
            "review_id": review_id,
            "closed": True,
            "source_attempt_id": attempt_id,
            "live_attempt_modified": False,
            "authority_modified": False,
        })

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
