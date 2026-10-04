"""R2 DEV-004: attempt-scoped lifecycle around the existing AircraftWorker.

Engineering constraints:
- Only an authenticated/backend Actor object may reach this class.
- argv/cwd are trusted backend configuration, NEVER client supplied input.
- No global process search or kill. Stop targets only the process-group created
  by AircraftWorker(start_new_session=True).
- This module does not yet launch ArduPilot/JSBSim production, allocate ports,
  verify raw MAVLink/FGNetFDM, implement full pause, or claim training readiness.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .models import Actor, CoreError, Role, State
from .store import Store
from .worker import AircraftWorker, WorkerSpec


@dataclass(frozen=True)
class WorkerHealth:
    attempt_id: str
    aircraft_id: str
    generation: int
    pid: int
    status: str
    healthy: bool
    exit_code: int | None
    runtime_dir: str


@dataclass
class _Binding:
    attempt_id: str
    aircraft_id: str
    generation: int
    pid: int
    worker: AircraftWorker
    runtime_dir: Path
    manifest: Path
    exit_recorded: bool = False
    stop_recorded: bool = False


class AttemptWorkerCoordinator:
    """In-process coordinator for one or more isolated engineering workers.

    Persistence of metadata does NOT mean a coordinator restart can safely adopt
    an old process. Process adoption is intentionally fail-closed and belongs to
    a later recovery design.
    """

    def __init__(self, store: Store, runtime_root: str | Path):
        self.store = store
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._by_attempt: dict[str, _Binding] = {}
        self._aircraft_lease: dict[str, str] = {}

    def start(self, actor: Actor, attempt_id: str, *, argv: tuple[str, ...], cwd: str | Path) -> WorkerHealth:
        """Start exactly one owned child for an existing non-Ended Attempt."""
        if not argv or any((not isinstance(x, str) or not x) for x in argv):
            raise CoreError("trusted worker argv required")
        cwd = Path(cwd)
        if not cwd.is_dir():
            raise CoreError("worker cwd does not exist")

        with self._lock:
            row = self._attempt_row(actor, attempt_id)
            if State(row["state"]) == State.ENDED:
                raise CoreError("cannot start worker for Ended attempt")
            if attempt_id in self._by_attempt:
                raise CoreError("attempt already has an owned worker")
            leased = self._aircraft_lease.get(row["aircraft_id"])
            if leased is not None:
                raise CoreError(f"aircraft already leased by attempt {leased}")

            runtime_dir = self.runtime_root / row["aircraft_id"] / f"g{row['generation']}-{attempt_id}"
            runtime_dir.mkdir(parents=True, exist_ok=False)
            spec = WorkerSpec(
                aircraft_id=row["aircraft_id"],
                argv=tuple(argv),
                cwd=cwd,
                runtime_dir=runtime_dir,
            )
            worker = AircraftWorker(spec)
            argv_hash = hashlib.sha256(json.dumps(list(argv), ensure_ascii=False).encode()).hexdigest()
            with self.store.transaction() as db:
                self.store.event(db, attempt_id, "worker.start_requested",
                                 aircraft_id=row["aircraft_id"], generation=row["generation"],
                                 argv_sha256=argv_hash, cwd=str(cwd))
            try:
                pid = worker.start()
            except BaseException as exc:
                with self.store.transaction() as db:
                    self.store.event(db, attempt_id, "worker.start_failed",
                                     aircraft_id=row["aircraft_id"], generation=row["generation"],
                                     error_type=type(exc).__name__)
                raise

            manifest = runtime_dir / "worker.json"
            binding = _Binding(attempt_id, row["aircraft_id"], row["generation"], pid,
                               worker, runtime_dir, manifest)
            self._by_attempt[attempt_id] = binding
            self._aircraft_lease[row["aircraft_id"]] = attempt_id
            self._write_manifest(binding, status="running", healthy=True, exit_code=None)
            with self.store.transaction() as db:
                self.store.event(db, attempt_id, "worker.started", pid=pid,
                                 aircraft_id=row["aircraft_id"], generation=row["generation"],
                                 runtime_dir=str(runtime_dir), argv_sha256=argv_hash)
            return self._snapshot(binding, status="running", healthy=True, exit_code=None)

    def health(self, actor: Actor, attempt_id: str) -> WorkerHealth:
        with self._lock:
            self._attempt_row(actor, attempt_id)
            binding = self._by_attempt.get(attempt_id)
            if binding is None:
                raise CoreError("attempt has no worker in this coordinator")
            code = binding.worker.poll()
            if code is None:
                self._write_manifest(binding, status="running", healthy=True, exit_code=None)
                return self._snapshot(binding, status="running", healthy=True, exit_code=None)
            if not binding.exit_recorded:
                with self.store.transaction() as db:
                    self.store.event(db, attempt_id, "worker.exited", pid=binding.pid,
                                     aircraft_id=binding.aircraft_id, generation=binding.generation,
                                     exit_code=code, unexpected=not binding.stop_recorded)
                binding.exit_recorded = True
            self._write_manifest(binding, status="exited", healthy=False, exit_code=code)
            return self._snapshot(binding, status="exited", healthy=False, exit_code=code)

    def stop(self, actor: Actor, attempt_id: str, *, timeout: float = 3.0) -> WorkerHealth:
        if not (0.1 <= timeout <= 30.0):
            raise CoreError("stop timeout outside engineering limit")
        with self._lock:
            self._attempt_row(actor, attempt_id)
            binding = self._by_attempt.get(attempt_id)
            if binding is None:
                raise CoreError("attempt has no worker in this coordinator")
            if binding.stop_recorded:
                raise CoreError("worker stop already completed")
            with self.store.transaction() as db:
                self.store.event(db, attempt_id, "worker.stop_requested", pid=binding.pid,
                                 aircraft_id=binding.aircraft_id, generation=binding.generation)
            code = binding.worker.stop(timeout=timeout)
            binding.stop_recorded = True
            binding.exit_recorded = True
            self._write_manifest(binding, status="stopped", healthy=False, exit_code=code)
            with self.store.transaction() as db:
                self.store.event(db, attempt_id, "worker.stopped", pid=binding.pid,
                                 aircraft_id=binding.aircraft_id, generation=binding.generation,
                                 exit_code=code)
            self._aircraft_lease.pop(binding.aircraft_id, None)
            return self._snapshot(binding, status="stopped", healthy=False, exit_code=code)

    def release_after_exit(self, actor: Actor, attempt_id: str) -> WorkerHealth:
        """Reap an already-exited worker and release the in-memory aircraft lease."""
        with self._lock:
            health = self.health(actor, attempt_id)
            if health.status != "exited":
                raise CoreError("worker has not exited")
            binding = self._by_attempt[attempt_id]
            code = binding.worker.stop(timeout=0.1)  # poll() already returned, so no signal is sent.
            binding.stop_recorded = True
            self._aircraft_lease.pop(binding.aircraft_id, None)
            self._write_manifest(binding, status="reaped", healthy=False, exit_code=code)
            with self.store.transaction() as db:
                self.store.event(db, attempt_id, "worker.reaped", pid=binding.pid,
                                 aircraft_id=binding.aircraft_id, generation=binding.generation,
                                 exit_code=code)
            return self._snapshot(binding, status="reaped", healthy=False, exit_code=code)

    def _attempt_row(self, actor: Actor, attempt_id: str):
        with self.store._lock:  # same process; read only, Store serializes SQLite access.
            row = self.store.db.execute("""SELECT a.*,s.instructor_id FROM attempts a
                JOIN exercises e ON a.exercise_id=e.id
                JOIN sessions s ON e.session_id=s.id WHERE a.id=?""", (attempt_id,)).fetchone()
        if row is None:
            raise CoreError("unknown attempt")
        if actor.role != Role.INSTRUCTOR or actor.id != row["instructor_id"]:
            raise CoreError("assigned instructor required for worker lifecycle")
        return row

    def _snapshot(self, binding: _Binding, *, status: str, healthy: bool, exit_code: int | None) -> WorkerHealth:
        return WorkerHealth(binding.attempt_id, binding.aircraft_id, binding.generation,
                            binding.pid, status, healthy, exit_code, str(binding.runtime_dir))

    def _write_manifest(self, binding: _Binding, *, status: str, healthy: bool, exit_code: int | None):
        data = asdict(self._snapshot(binding, status=status, healthy=healthy, exit_code=exit_code))
        data.update({
            "schema": "omni.r2.worker-runtime.v1",
            "updated_utc": time.time(),
            "owner_pid": os.getpid(),
            "adoption_supported": False,
        })
        tmp = binding.manifest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, binding.manifest)
