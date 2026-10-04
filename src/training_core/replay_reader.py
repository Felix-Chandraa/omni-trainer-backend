"""Read-only evidence-package reader for DEV-014 replay."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayFrame:
    source_sequence: int
    elapsed_ns: int
    relative_ns: int
    telemetry: dict[str, Any]


@dataclass(frozen=True)
class IntegrityReport:
    verified_files: int
    warnings: tuple[str, ...]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            h.update(block)
    return h.hexdigest()


class ReplayPackage:
    """Immutable view of one finalized Attempt evidence package.

    Replay reads stored trajectory only. It never re-runs flight inputs.
    """

    def __init__(
        self,
        package: Path,
        *,
        allow_incomplete: bool = False,
    ):
        package = Path(package).expanduser().resolve()
        if package.is_file() and package.name == "index.json":
            package = package.parent
        self.root = package
        self.index_path = self.root / "index.json"
        self.manifest_path = self.root / "manifest.json"
        if not self.index_path.is_file():
            raise ReplayError(
                f"index.json missing: {self.index_path}"
            )
        self.index = json.loads(
            self.index_path.read_text(encoding="utf-8")
        )
        self.identity = self.index.get("identity") or {}
        if not self.identity.get("attempt_id"):
            raise ReplayError("index identity missing attempt_id")
        if (
            not allow_incomplete
            and not self.index.get("overall_complete", False)
        ):
            raise ReplayError(
                "evidence package is incomplete; "
                "use allow_incomplete only for diagnostic review"
            )
        self.integrity = self.verify_integrity()
        self.frames = self._load_state_frames()
        if not self.frames:
            raise ReplayError("no server/state frames")

    @property
    def duration_ns(self) -> int:
        if not self.frames:
            return 0
        return self.frames[-1].relative_ns

    @property
    def duration_s(self) -> float:
        return self.duration_ns / 1_000_000_000

    def verify_integrity(self) -> IntegrityReport:
        verified = 0
        warnings: list[str] = []
        for meta in self.index.get("files", []):
            rel = meta.get("path")
            expected = meta.get("sha256")
            expected_size = meta.get("bytes")
            if not isinstance(rel, str):
                raise ReplayError("invalid index file entry")
            path = self.root / rel
            if not path.is_file():
                raise ReplayError(
                    f"indexed evidence missing: {rel}"
                )
            if (
                isinstance(expected_size, int)
                and path.stat().st_size != expected_size
            ):
                # DEV-012 v1 updated manifest after building index,
                # so its final manifest size/hash can differ. Preserve
                # compatibility for that one known metadata file only.
                if rel == "manifest.json":
                    warnings.append(
                        "legacy DEV-012 manifest metadata was "
                        "mutated after index hashing; non-manifest "
                        "evidence remains strictly verified"
                    )
                    continue
                raise ReplayError(
                    f"size mismatch: {rel}"
                )
            if isinstance(expected, str):
                actual = _sha256(path)
                if actual != expected:
                    if rel == "manifest.json":
                        warnings.append(
                            "legacy DEV-012 manifest hash mismatch "
                            "accepted for backward compatibility"
                        )
                        continue
                    raise ReplayError(
                        f"SHA-256 mismatch: {rel}"
                    )
            verified += 1
        return IntegrityReport(
            verified_files=verified,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _channel_segments(
        self, channel: str
    ) -> list[Path]:
        meta = (self.index.get("channels") or {}).get(
            channel
        )
        if not isinstance(meta, dict):
            return []
        result = []
        for rel in meta.get("segments", []):
            p = self.root / rel
            if not p.is_file():
                raise ReplayError(
                    f"channel segment missing: {rel}"
                )
            result.append(p)
        return result

    def _iter_jsonl(
        self, channel: str
    ) -> Iterable[dict[str, Any]]:
        for path in self._channel_segments(channel):
            with path.open(
                "r", encoding="utf-8"
            ) as f:
                for line_no, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ReplayError(
                            f"invalid JSONL {path}:{line_no}: "
                            f"{exc}"
                        ) from exc
                    yield row

    def _load_state_frames(self) -> list[ReplayFrame]:
        raw_rows = list(self._iter_jsonl("server/state"))
        if not raw_rows:
            return []

        expected_attempt = self.identity["attempt_id"]
        previous_elapsed = None
        previous_seq = None
        parsed: list[
            tuple[int, int, dict[str, Any]]
        ] = []

        for row in raw_rows:
            identity = row.get("identity") or {}
            if (
                identity.get("attempt_id")
                != expected_attempt
            ):
                raise ReplayError(
                    "state record crosses Attempt identity"
                )
            seq = row.get("sequence")
            elapsed = (
                (row.get("time") or {}).get(
                    "elapsed_monotonic_ns"
                )
            )
            payload = row.get("payload") or {}
            state = payload.get("state") or {}
            if not isinstance(seq, int):
                raise ReplayError(
                    "state sequence missing"
                )
            if not isinstance(elapsed, int):
                raise ReplayError(
                    "state elapsed time missing"
                )
            if (
                previous_seq is not None
                and seq <= previous_seq
            ):
                raise ReplayError(
                    "state sequence not increasing"
                )
            if (
                previous_elapsed is not None
                and elapsed < previous_elapsed
            ):
                raise ReplayError(
                    "state time moved backwards"
                )
            parsed.append((seq, elapsed, state))
            previous_seq = seq
            previous_elapsed = elapsed

        first_elapsed = parsed[0][1]
        frames = []
        for seq, elapsed, state in parsed:
            yaw = state.get("yaw_deg")
            vcas = state.get("vcas_mps")
            telemetry = {
                "lat": state.get("lat_deg"),
                "lon": state.get("lon_deg"),
                "alt": state.get("agl_m"),
                "alt_msl": state.get("alt_msl_m"),
                "agl": state.get("agl_m"),
                "agl_raw": state.get("agl_m"),
                "roll": state.get("roll_deg"),
                "pitch": state.get("pitch_deg"),
                "yaw": yaw,
                "hdg": (
                    float(yaw) % 360.0
                    if isinstance(yaw, (int, float))
                    else None
                ),
                "as": vcas,
                "gs": None,
                "pose_source": "replay_fg",
                "vehicle_type": 1,
                "armed": False,
                "mode": "REPLAY",
                            "srv1": state.get("srv1"),
                            "srv2": state.get("srv2"),
                            "srv3": state.get("srv3"),
                            "srv4": state.get("srv4"),
                            "throttle": state.get("throttle"),
                        }
            frames.append(
                ReplayFrame(
                    source_sequence=seq,
                    elapsed_ns=elapsed,
                    relative_ns=elapsed - first_elapsed,
                    telemetry=telemetry,
                )
            )
        return frames

    def timeline_events(
        self,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        channels = self.index.get("channels") or {}
        for channel in sorted(channels):
            if channel == "server/state":
                continue
            if not (
                channel.startswith("server/")
                or channel.startswith("clients/")
            ):
                continue
            for row in self._iter_jsonl(channel):
                elapsed = (
                    (row.get("time") or {}).get(
                        "elapsed_monotonic_ns"
                    )
                )
                if not isinstance(elapsed, int):
                    continue
                result.append(
                    {
                        "channel": channel,
                        "elapsed_ns": elapsed,
                        "kind": row.get("kind"),
                        "payload": row.get("payload"),
                        "identity": row.get("identity"),
                    }
                )
        result.sort(key=lambda x: x["elapsed_ns"])
        return result
