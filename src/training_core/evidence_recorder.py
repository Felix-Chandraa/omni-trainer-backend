"""DEV-012 Attempt evidence recording foundation.

Design goals:
- one durable package per Attempt;
- authoritative server truth separate from student-visible/client experience;
- all evidence carries Attempt/Aircraft/Generation identity;
- monotonic elapsed and UTC are both stored; simulation time may remain null
  until an authoritative simulation clock is available;
- append-only, segmented JSONL with bounded asynchronous I/O;
- recording overload creates an explicit gap/incompleteness marker rather than
  blocking the flight/control loop;
- raw MAVLink is captured as a standard timestamped .tlog on the server;
- final index includes sizes, SHA-256 checksums, counts and completeness.

DEV-012 does not decide assessment outcomes and does not replay the FDM.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import socket
import struct
import threading
import time
from typing import Any, Iterable

SCHEMA_VERSION = "omni.evidence.v1"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class RecorderError(RuntimeError):
    pass


def _utc_iso_from_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def _atomic_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    tmp.write_text(data + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _slug(value: str) -> str:
    x = _SLUG_RE.sub("-", value.strip()).strip("-")
    return x or "unknown"


@dataclass(frozen=True)
class AttemptIdentity:
    session_id: str
    exercise_id: str
    attempt_id: str
    aircraft_id: str
    generation: int

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClockMapping:
    station_id: str
    sample_count: int = 0
    offset_ns: int | None = None
    uncertainty_ns: int | None = None
    last_server_monotonic_ns: int | None = None
    last_server_utc_ns: int | None = None

    def update_ntp_sample(
        self,
        *,
        client_send_mono_ns: int,
        server_recv_mono_ns: int,
        server_send_mono_ns: int,
        client_recv_mono_ns: int,
        server_recv_utc_ns: int,
    ) -> None:
        # Map client monotonic to server monotonic using NTP midpoint formula.
        # offset = server - client
        off = (
            (server_recv_mono_ns - client_send_mono_ns)
            + (server_send_mono_ns - client_recv_mono_ns)
        ) // 2
        rtt = max(
            0,
            (client_recv_mono_ns - client_send_mono_ns)
            - (server_send_mono_ns - server_recv_mono_ns),
        )
        uncertainty = max(1, rtt // 2)

        # Keep the best (lowest uncertainty) recent sample as the active mapping.
        if self.uncertainty_ns is None or uncertainty <= self.uncertainty_ns:
            self.offset_ns = int(off)
            self.uncertainty_ns = int(uncertainty)
        self.sample_count += 1
        self.last_server_monotonic_ns = int(server_recv_mono_ns)
        self.last_server_utc_ns = int(server_recv_utc_ns)

    def map_client_monotonic(self, client_mono_ns: int) -> tuple[int | None, int | None]:
        if self.offset_ns is None:
            return None, self.uncertainty_ns
        return int(client_mono_ns) + self.offset_ns, self.uncertainty_ns


@dataclass
class ChannelStats:
    channel: str
    records_enqueued: int = 0
    records_written: int = 0
    dropped_records: int = 0
    first_elapsed_ns: int | None = None
    last_elapsed_ns: int | None = None
    segments: list[str] = field(default_factory=list)
    gap_intervals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.dropped_records == 0


class _SegmentedJsonlWriter:
    def __init__(self, package_root: Path, channel: str, rotate_bytes: int):
        self.package_root = package_root
        self.channel = channel
        self.rotate_bytes = max(64 * 1024, int(rotate_bytes))
        self._index = 0
        self._fp = None
        self._path: Path | None = None
        self._bytes = 0
        self.segment_paths: list[Path] = []

    def _open_next(self) -> None:
        if self._fp is not None:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()
        self._index += 1
        base = self.package_root / self.channel
        base.parent.mkdir(parents=True, exist_ok=True)
        path = Path(f"{base}.{self._index:06d}.jsonl")
        self._fp = path.open("ab", buffering=0)
        self._path = path
        self._bytes = 0
        self.segment_paths.append(path)

    def write(self, obj: dict[str, Any]) -> None:
        raw = (
            json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
            + "\n"
        ).encode("utf-8")
        if self._fp is None or (self._bytes and self._bytes + len(raw) > self.rotate_bytes):
            self._open_next()
        assert self._fp is not None
        self._fp.write(raw)
        self._bytes += len(raw)

    def close(self) -> None:
        if self._fp is None:
            return
        self._fp.flush()
        os.fsync(self._fp.fileno())
        self._fp.close()
        self._fp = None


class _AsyncEvidenceWriter:
    def __init__(
        self,
        package_root: Path,
        *,
        queue_size: int,
        rotate_bytes: int,
    ):
        self.package_root = package_root
        self.rotate_bytes = rotate_bytes
        self.q: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(
            maxsize=max(32, int(queue_size))
        )
        self.stats: dict[str, ChannelStats] = {}
        self._writers: dict[str, _SegmentedJsonlWriter] = {}
        self._lock = threading.Lock()
        self._open_gaps: dict[str, dict[str, Any]] = {}
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="OmniEvidenceWriter"
        )
        self._thread.start()

    def enqueue(self, channel: str, record: dict[str, Any]) -> bool:
        elapsed = record.get("time", {}).get("elapsed_monotonic_ns")
        with self._lock:
            stat = self.stats.setdefault(channel, ChannelStats(channel=channel))
            stat.records_enqueued += 1
            if isinstance(elapsed, int):
                if stat.first_elapsed_ns is None:
                    stat.first_elapsed_ns = elapsed
                stat.last_elapsed_ns = elapsed
        try:
            self.q.put_nowait((channel, record))
            with self._lock:
                gap = self._open_gaps.pop(channel, None)
                if gap is not None:
                    gap["end_elapsed_ns"] = elapsed
                    self.stats[channel].gap_intervals.append(gap)
            return True
        except queue.Full:
            with self._lock:
                stat = self.stats[channel]
                stat.dropped_records += 1
                gap = self._open_gaps.get(channel)
                if gap is None:
                    gap = {
                        "reason": "writer_queue_overflow",
                        "start_elapsed_ns": elapsed,
                        "end_elapsed_ns": None,
                        "dropped_records": 0,
                    }
                    self._open_gaps[channel] = gap
                gap["dropped_records"] += 1
                if isinstance(elapsed, int):
                    gap["end_elapsed_ns"] = elapsed
            return False

    def _run(self) -> None:
        try:
            while True:
                item = self.q.get()
                try:
                    if item is None:
                        return
                    channel, record = item
                    writer = self._writers.get(channel)
                    if writer is None:
                        writer = _SegmentedJsonlWriter(
                            self.package_root, channel, self.rotate_bytes
                        )
                        self._writers[channel] = writer
                    writer.write(record)
                    with self._lock:
                        self.stats[channel].records_written += 1
                finally:
                    self.q.task_done()
        except Exception as exc:
            self._error = exc

    def close(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.q.unfinished_tasks:
            raise RecorderError(
                f"evidence writer did not drain: {self.q.unfinished_tasks} pending"
            )
        self.q.put(None)
        self._thread.join(timeout=max(0.1, deadline - time.monotonic()))
        if self._thread.is_alive():
            raise RecorderError("evidence writer did not stop")
        for channel, writer in self._writers.items():
            writer.close()
            with self._lock:
                stat = self.stats[channel]
                stat.segments = [
                    str(p.relative_to(self.package_root))
                    for p in writer.segment_paths
                ]
        with self._lock:
            for channel, gap in list(self._open_gaps.items()):
                self.stats[channel].gap_intervals.append(gap)
            self._open_gaps.clear()
        if self._error is not None:
            raise RecorderError(f"evidence writer failed: {self._error}")


def _split_mavlink_frames(datagram: bytes) -> tuple[list[bytes], int]:
    """Split MAVLink v1/v2 packets from one UDP datagram.

    Returns (valid frames, skipped byte count). The checksum is preserved but
    not re-computed here; this recorder's job is byte-preserving evidence.
    """
    frames: list[bytes] = []
    skipped = 0
    i = 0
    n = len(datagram)
    while i < n:
        magic = datagram[i]
        if magic == 0xFE:  # MAVLink v1
            if i + 2 > n:
                skipped += n - i
                break
            payload_len = datagram[i + 1]
            total = 8 + payload_len
        elif magic == 0xFD:  # MAVLink v2
            if i + 3 > n:
                skipped += n - i
                break
            payload_len = datagram[i + 1]
            incompat = datagram[i + 2]
            total = 12 + payload_len + (13 if (incompat & 0x01) else 0)
        else:
            skipped += 1
            i += 1
            continue

        if i + total > n:
            skipped += n - i
            break
        frames.append(datagram[i : i + total])
        i += total
    return frames, skipped


class MavlinkTlogRecorder:
    """Owns one server-side MAVProxy UDP output and writes MAVProxy-style .tlog.

    Each record is:
      8-byte big-endian UTC microseconds + one complete MAVLink frame.

    This is intentionally on the server. Future GCS integration can fan out
    from the backend/gateway instead of exposing raw ArduPilot control ports.
    """

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        path: Path,
        identity: AttemptIdentity,
    ):
        self.bind_host = bind_host
        self.port = int(port)
        self.path = path
        self.identity = identity
        self.frames = 0
        self.datagrams = 0
        self.skipped_bytes = 0
        self.first_utc_ns: int | None = None
        self.last_utc_ns: int | None = None
        self._sock: socket.socket | None = None
        self._fp = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind_host, self.port))
        except Exception:
            sock.close()
            raise
        sock.settimeout(0.2)
        self._sock = sock
        self._fp = self.path.open("wb", buffering=0)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"OmniTlog:{self.port}"
        )
        self._thread.start()

    def _run(self) -> None:
        assert self._sock is not None and self._fp is not None
        try:
            while not self._stop.is_set():
                try:
                    data, _peer = self._sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                utc_ns = time.time_ns()
                self.datagrams += 1
                frames, skipped = _split_mavlink_frames(data)
                self.skipped_bytes += skipped
                for frame in frames:
                    # MAVProxy / pymavlink tlog timestamp format: UTC usec BE.
                    self._fp.write(struct.pack(">Q", utc_ns // 1000))
                    self._fp.write(frame)
                    self.frames += 1
                    if self.first_utc_ns is None:
                        self.first_utc_ns = utc_ns
                    self.last_utc_ns = utc_ns
        except Exception as exc:
            self._error = exc

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._fp is not None:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()
            self._fp = None
        if self._error is not None:
            raise RecorderError(f"MAVLink tlog recorder failed: {self._error}")


class AttemptEvidenceRecorder:
    def __init__(
        self,
        root: Path,
        identity: AttemptIdentity,
        *,
        configuration: dict[str, Any] | None = None,
        queue_size: int = 8192,
        rotate_bytes: int = 16 * 1024 * 1024,
    ):
        self.identity = identity
        self.root = Path(root)
        self.package_root = self.root / identity.attempt_id
        if self.package_root.exists():
            raise RecorderError(f"attempt evidence already exists: {self.package_root}")
        self.package_root.mkdir(parents=True, exist_ok=False)

        self.start_mono_ns = time.monotonic_ns()
        self.start_utc_ns = time.time_ns()
        self.configuration = configuration or {}
        self._writer = _AsyncEvidenceWriter(
            self.package_root, queue_size=queue_size, rotate_bytes=rotate_bytes
        )
        self._seq: dict[str, int] = {}
        self._seq_lock = threading.Lock()
        self._clock: dict[str, ClockMapping] = {}
        self._finalized = False
        self._tlog: MavlinkTlogRecorder | None = None

        _atomic_json(self.package_root / "manifest.json", {
            "schema_version": SCHEMA_VERSION,
            "status": "recording",
            "identity": identity.public(),
            "started": {
                "server_utc_ns": self.start_utc_ns,
                "server_utc": _utc_iso_from_ns(self.start_utc_ns),
                "server_monotonic_ns": self.start_mono_ns,
            },
            "configuration_snapshot": self.configuration,
            "recording_policy": {
                "server_truth_separate_from_client_experience": True,
                "bounded_async_writer": True,
                "queue_size": queue_size,
                "segment_rotate_bytes": rotate_bytes,
                "simulation_time_nullable": True,
            },
        })

    def _time(
        self,
        *,
        simulation_time_s: float | None = None,
        active_time_s: float | None = None,
        utc_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> dict[str, Any]:
        utc_ns = int(utc_ns if utc_ns is not None else time.time_ns())
        mono_ns = int(mono_ns if mono_ns is not None else time.monotonic_ns())
        return {
            "server_utc_ns": utc_ns,
            "server_utc": _utc_iso_from_ns(utc_ns),
            "server_monotonic_ns": mono_ns,
            "elapsed_monotonic_ns": max(0, mono_ns - self.start_mono_ns),
            "simulation_time_s": simulation_time_s,
            "active_time_s": active_time_s,
        }

    def _next_seq(self, channel: str) -> int:
        with self._seq_lock:
            nxt = self._seq.get(channel, 0) + 1
            self._seq[channel] = nxt
            return nxt

    def _record(
        self,
        channel: str,
        kind: str,
        payload: dict[str, Any],
        *,
        simulation_time_s: float | None = None,
        active_time_s: float | None = None,
        utc_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> bool:
        if self._finalized:
            raise RecorderError("attempt evidence already finalized")
        rec = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "sequence": self._next_seq(channel),
            "identity": self.identity.public(),
            "time": self._time(
                simulation_time_s=simulation_time_s,
                active_time_s=active_time_s,
                utc_ns=utc_ns,
                mono_ns=mono_ns,
            ),
            "payload": payload,
        }
        return self._writer.enqueue(channel, rec)

    def start_mavlink_tlog(self, *, bind_host: str, port: int) -> Path:
        if self._tlog is not None:
            raise RecorderError("MAVLink tlog already started")
        path = self.package_root / "server" / "aircraft.tlog"
        rec = MavlinkTlogRecorder(
            bind_host=bind_host,
            port=port,
            path=path,
            identity=self.identity,
        )
        rec.start()
        self._tlog = rec
        self.record_event(
            "mavlink_tlog_started",
            {"bind_host": bind_host, "port": int(port), "path": "server/aircraft.tlog"},
        )
        return path

    def record_state(
        self,
        state: dict[str, Any],
        *,
        source: str,
        simulation_time_s: float | None = None,
        active_time_s: float | None = None,
        validity: str = "valid",
        freshness_ms: float | None = None,
    ) -> bool:
        return self._record(
            "server/state",
            "state_sample",
            {
                "source": source,
                "validity": validity,
                "freshness_ms": freshness_ms,
                "state": state,
            },
            simulation_time_s=simulation_time_s,
            active_time_s=active_time_s,
        )

    def record_lifecycle(
        self,
        state: str,
        *,
        revision: int | None = None,
        reason: str | None = None,
    ) -> bool:
        return self._record(
            "server/lifecycle",
            "lifecycle",
            {"state": state, "revision": revision, "reason": reason},
        )

    def record_event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_id: str | None = None,
        station_id: str | None = None,
        visibility: str = "server",
    ) -> bool:
        return self._record(
            "server/events",
            "event",
            {
                "name": name,
                "actor_id": actor_id,
                "station_id": station_id,
                "visibility": visibility,
                "data": payload or {},
            },
        )

    def record_command(
        self,
        *,
        command_id: str,
        actor_id: str,
        station_id: str,
        scope: str,
        name: str,
        payload: dict[str, Any],
        disposition: str,
        request_utc_ns: int | None = None,
        applied_utc_ns: int | None = None,
        authority_epoch: int | None = None,
        reason: str | None = None,
    ) -> bool:
        return self._record(
            "server/commands",
            "command",
            {
                "command_id": command_id,
                "actor_id": actor_id,
                "station_id": station_id,
                "scope": scope,
                "name": name,
                "command_payload": payload,
                "disposition": disposition,
                "request_utc_ns": request_utc_ns,
                "applied_utc_ns": applied_utc_ns,
                "authority_epoch": authority_epoch,
                "reason": reason,
            },
        )

    def record_authority(
        self,
        *,
        scope: str,
        previous_actor_id: str | None,
        new_actor_id: str | None,
        epoch: int,
        reason: str,
    ) -> bool:
        return self._record(
            "server/authority",
            "authority",
            {
                "scope": scope,
                "previous_actor_id": previous_actor_id,
                "new_actor_id": new_actor_id,
                "epoch": int(epoch),
                "reason": reason,
            },
        )

    def register_client_clock_sample(
        self,
        *,
        station_id: str,
        client_send_mono_ns: int,
        server_recv_mono_ns: int,
        server_send_mono_ns: int,
        client_recv_mono_ns: int,
        server_recv_utc_ns: int,
    ) -> ClockMapping:
        mapping = self._clock.setdefault(station_id, ClockMapping(station_id))
        mapping.update_ntp_sample(
            client_send_mono_ns=client_send_mono_ns,
            server_recv_mono_ns=server_recv_mono_ns,
            server_send_mono_ns=server_send_mono_ns,
            client_recv_mono_ns=client_recv_mono_ns,
            server_recv_utc_ns=server_recv_utc_ns,
        )
        self._record(
            f"clients/{_slug(station_id)}/clock",
            "clock_sync",
            {
                "station_id": station_id,
                "offset_ns": mapping.offset_ns,
                "uncertainty_ns": mapping.uncertainty_ns,
                "sample_count": mapping.sample_count,
            },
        )
        return mapping

    def record_client_received_state(
        self,
        *,
        station_id: str,
        actor_id: str,
        state: dict[str, Any],
        source_sequence: int | None = None,
        client_mono_ns: int | None = None,
        client_utc_ns: int | None = None,
    ) -> bool:
        mapping = self._clock.get(station_id)
        mapped_server_mono_ns = None
        uncertainty_ns = None
        if mapping is not None and client_mono_ns is not None:
            mapped_server_mono_ns, uncertainty_ns = mapping.map_client_monotonic(
                client_mono_ns
            )
        return self._record(
            f"clients/{_slug(station_id)}/received_state",
            "client_received_state",
            {
                "station_id": station_id,
                "actor_id": actor_id,
                "source_sequence": source_sequence,
                "client_time": {
                    "client_monotonic_ns": client_mono_ns,
                    "client_utc_ns": client_utc_ns,
                    "mapped_server_monotonic_ns": mapped_server_mono_ns,
                    "mapping_uncertainty_ns": uncertainty_ns,
                },
                "state": state,
            },
        )

    def record_client_input_sent(
        self,
        *,
        station_id: str,
        actor_id: str,
        state: dict[str, Any],
        source_sequence: int | None = None,
        client_mono_ns: int | None = None,
        client_utc_ns: int | None = None,
    ) -> bool:
        mapping = self._clock.get(station_id)
        mapped_server_mono_ns = None
        uncertainty_ns = None
        if mapping is not None and client_mono_ns is not None:
            mapped_server_mono_ns, uncertainty_ns = mapping.map_client_monotonic(
                client_mono_ns
            )
        return self._record(
            f"clients/{_slug(station_id)}/input_sent",
            "client_input_sent",
            {
                "station_id": station_id,
                "actor_id": actor_id,
                "source_sequence": source_sequence,
                "client_time": {
                    "client_monotonic_ns": client_mono_ns,
                    "client_utc_ns": client_utc_ns,
                    "mapped_server_monotonic_ns": mapped_server_mono_ns,
                    "mapping_uncertainty_ns": uncertainty_ns,
                },
                "state": state,
            },
        )

    def record_client_event(
        self,
        *,
        station_id: str,
        actor_id: str,
        role: str,
        action: str,
        data: dict[str, Any],
        client_mono_ns: int | None,
        client_utc_ns: int | None = None,
        scope: str | None = None,
        source: str = "client",
    ) -> bool:
        mapping = self._clock.get(station_id)
        mapped_server_mono_ns = None
        uncertainty_ns = None
        if mapping is not None and client_mono_ns is not None:
            mapped_server_mono_ns, uncertainty_ns = mapping.map_client_monotonic(
                client_mono_ns
            )
        return self._record(
            f"clients/{_slug(station_id)}/events",
            "client_event",
            {
                "station_id": station_id,
                "actor_id": actor_id,
                "role": role,
                "scope": scope,
                "action": action,
                "source": source,
                "client_time": {
                    "client_monotonic_ns": client_mono_ns,
                    "client_utc_ns": client_utc_ns,
                    "mapped_server_monotonic_ns": mapped_server_mono_ns,
                    "mapping_uncertainty_ns": uncertainty_ns,
                },
                "data": data,
            },
            # Use server receive time as authoritative evidence timestamp.
            mono_ns=time.monotonic_ns(),
            utc_ns=time.time_ns(),
        )

    def finalize(self, *, status: str = "complete", reason: str | None = None) -> Path:
        if self._finalized:
            return self.package_root / "index.json"

        tlog_meta: dict[str, Any] | None = None
        if self._tlog is not None:
            self._tlog.stop()
            tlog_meta = {
                "path": "server/aircraft.tlog",
                "frames": self._tlog.frames,
                "datagrams": self._tlog.datagrams,
                "skipped_bytes": self._tlog.skipped_bytes,
                "first_utc_ns": self._tlog.first_utc_ns,
                "last_utc_ns": self._tlog.last_utc_ns,
                "complete": self._tlog.frames > 0 and self._tlog.skipped_bytes == 0,
            }

        self._writer.close()
        self._finalized = True

        files = []
        for p in sorted(self.package_root.rglob("*")):
            if not p.is_file() or p.name in {"index.json"}:
                continue
            rel = str(p.relative_to(self.package_root))
            files.append({
                "path": rel,
                "bytes": p.stat().st_size,
                "sha256": _sha256(p),
            })

        channels = {}
        overall_complete = True
        for channel, stat in sorted(self._writer.stats.items()):
            d = asdict(stat)
            d["complete"] = stat.complete
            channels[channel] = d
            overall_complete = overall_complete and stat.complete

        if tlog_meta is not None:
            overall_complete = overall_complete and bool(tlog_meta["complete"])

        ended_utc_ns = time.time_ns()
        index = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "reason": reason,
            "identity": self.identity.public(),
            "started_utc_ns": self.start_utc_ns,
            "ended_utc_ns": ended_utc_ns,
            "duration_elapsed_ns": max(0, time.monotonic_ns() - self.start_mono_ns),
            "channels": channels,
            "mavlink_tlog": tlog_meta,
            "clock_mappings": {
                station: asdict(mapping)
                for station, mapping in sorted(self._clock.items())
            },
            "files": files,
            "overall_complete": overall_complete,
        }
        index_path = self.package_root / "index.json"
        _atomic_json(index_path, index)

        manifest_path = self.package_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({
            "status": "finalized",
            "final_status": status,
            "final_reason": reason,
            "ended_utc_ns": ended_utc_ns,
            "index": "index.json",
            "overall_complete": overall_complete,
        })
        _atomic_json(manifest_path, manifest)
        return index_path

    def abort(self, reason: str) -> Path:
        return self.finalize(status="aborted", reason=reason)
