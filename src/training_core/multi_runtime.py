"""DEV-009 multi-worker runtime allocation and direct-launch helpers.

Legacy run.sh/sim_vehicle.py remains a single-instance development path.
Concurrent managed workers use non-zero ArduPilot instances and deterministic
per-slot ports. A small file-lock-backed lease table prevents two Attempts from
claiming the same runtime slot.

No process cleanup is performed in this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
from typing import Iterable

from .models import CoreError


@dataclass(frozen=True)
class RuntimeSlot:
    slot: int
    instance: int
    sitl_tcp: int
    sitl_tcp_secondary: int
    rcin_udp: int
    fg_udp: int
    mav_client_udp: int
    mav_monitor_udp: int

    @classmethod
    def for_slot(cls, slot: int) -> "RuntimeSlot":
        if slot < 1:
            raise CoreError("multi-worker slot 0 is forbidden")
        if slot > 32:
            raise CoreError("multi-worker slot out of supported range")
        i = int(slot)
        return cls(
            slot=i,
            instance=i,
            sitl_tcp=5760 + 10 * i,
            sitl_tcp_secondary=5762 + 10 * i,
            rcin_udp=5501 + 10 * i,
            fg_udp=5503 + 10 * i,
            mav_client_udp=14550 + 10 * i,
            mav_monitor_udp=14555 + 10 * i,
        )


@dataclass(frozen=True)
class SlotLease:
    attempt_id: str
    session_id: str
    aircraft_id: str
    slot: RuntimeSlot
    claimed_unix: float


class RuntimeSlotAllocator:
    """Cross-process-safe lease table for a bounded set of runtime slots."""

    def __init__(self, root: Path, *, max_slots: int = 3):
        if max_slots < 1:
            raise CoreError("max_slots must be >= 1")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_slots = int(max_slots)
        self.state_path = self.root / "leases.json"
        self.lock_path = self.root / "leases.lock"
        self.lock_path.touch(exist_ok=True)

    def _read(self) -> dict:
        if not self.state_path.exists():
            return {"schema": "omni.r2.runtime-slots.v1", "leases": []}
        data = json.loads(self.state_path.read_text())
        if data.get("schema") != "omni.r2.runtime-slots.v1":
            raise CoreError("runtime slot lease schema mismatch")
        if not isinstance(data.get("leases"), list):
            raise CoreError("runtime slot lease file malformed")
        return data

    def _write(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(
            prefix=".leases-", suffix=".json", dir=str(self.root)
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _row_to_lease(row: dict) -> SlotLease:
        return SlotLease(
            attempt_id=row["attempt_id"],
            session_id=row["session_id"],
            aircraft_id=row["aircraft_id"],
            slot=RuntimeSlot.for_slot(int(row["slot"])),
            claimed_unix=float(row["claimed_unix"]),
        )

    def claim(self, *, attempt_id: str, session_id: str, aircraft_id: str,
              preferred_slot: int | None = None) -> SlotLease:
        if not attempt_id or not session_id or not aircraft_id:
            raise CoreError("attempt/session/aircraft identity required for slot lease")
        if preferred_slot is not None and not (1 <= preferred_slot <= self.max_slots):
            raise CoreError("preferred slot outside configured pool")

        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._read()

            for row in data["leases"]:
                if row["attempt_id"] == attempt_id:
                    if row["session_id"] != session_id or row["aircraft_id"] != aircraft_id:
                        raise CoreError("attempt already leased with different identity")
                    return self._row_to_lease(row)

            used = {int(row["slot"]) for row in data["leases"]}
            candidates: Iterable[int]
            if preferred_slot is not None:
                candidates = (preferred_slot,)
            else:
                candidates = range(1, self.max_slots + 1)

            chosen = next((s for s in candidates if s not in used), None)
            if chosen is None:
                raise CoreError("no free runtime slot")

            row = {
                "attempt_id": attempt_id,
                "session_id": session_id,
                "aircraft_id": aircraft_id,
                "slot": chosen,
                "claimed_unix": time.time(),
            }
            data["leases"].append(row)
            self._write(data)
            return self._row_to_lease(row)

    def release(self, attempt_id: str) -> bool:
        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._read()
            before = len(data["leases"])
            data["leases"] = [
                row for row in data["leases"] if row["attempt_id"] != attempt_id
            ]
            changed = len(data["leases"]) != before
            if changed:
                self._write(data)
            return changed

    def snapshot(self) -> tuple[SlotLease, ...]:
        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            data = self._read()
            return tuple(self._row_to_lease(row) for row in data["leases"])


def find_latest_working_worker_log(base: Path) -> Path:
    """Find newest DEV-008/007/006 worker log that contains ArduPlane launch."""
    base = Path(base)
    roots = [
        base / "runtime" / "dev008",
        base / "runtime" / "dev007",
        base / "runtime" / "dev006",
    ]
    candidates: list[Path] = []
    for root in roots:
        if root.exists():
            candidates.extend(root.rglob("worker.log"))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        text = path.read_text(errors="replace")
        if "Run ArduPlane" in text and "jsbsim:Omni-Trainer" in text:
            return path
    raise CoreError("no proven working ArduPlane worker.log found; run DEV-008 first")


def extract_arduplane_template(worker_log: Path) -> tuple[str, ...]:
    """Extract the exact direct ArduPlane argv printed by sim_vehicle.py."""
    text = Path(worker_log).read_text(errors="replace")
    for raw in text.splitlines():
        if not raw.startswith("SIM_VEHICLE: "):
            continue
        if "jsbsim:Omni-Trainer" not in raw:
            continue
        try:
            tokens = shlex.split(raw[len("SIM_VEHICLE: "):])
        except ValueError:
            continue

        # sim_vehicle often prints:
        # run_in_terminal_window.sh "ArduPlane" "/.../arduplane" <args...>
        # The first ArduPlane token is a terminal-window title, NOT the
        # executable. Prefer a path-like candidate, then the final matching
        # token as a conservative fallback.
        candidates = [
            (i, token)
            for i, token in enumerate(tokens)
            if Path(token).name.lower() in {"arduplane", "arduplane.elf"}
        ]
        if not candidates:
            continue
        pathlike = [
            (i, token) for i, token in candidates
            if "/" in token or token.startswith(".")
        ]
        idx = (pathlike[-1] if pathlike else candidates[-1])[0]
        cmd = tuple(tokens[idx:])
        if any(t == "jsbsim:Omni-Trainer" for t in cmd):
            return cmd
    raise CoreError("could not extract direct ArduPlane command from proven log")


def validate_template(argv: tuple[str, ...]) -> None:
    if not argv:
        raise CoreError("empty ArduPlane command template")
    binary = Path(argv[0])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CoreError(f"ArduPlane binary missing/not executable: {binary}")
    if "jsbsim:Omni-Trainer" not in argv:
        raise CoreError("template is not Omni-Trainer JSBSim")
    if not any(t.startswith("-I") for t in argv) and "-I" not in argv:
        raise CoreError("template does not contain SITL instance")
    if "--defaults" not in argv:
        raise CoreError("template does not contain resolved default parameters")


def adapt_arduplane_command(template: tuple[str, ...], *, instance: int,
                            autotest_dir: Path) -> tuple[str, ...]:
    if instance < 1:
        raise CoreError("direct multi-worker runtime forbids instance 0")
    validate_template(template)
    out = list(template)

    # Normalize instance syntax to one explicit token.
    replaced = False
    i = 1
    while i < len(out):
        if out[i] == "-I":
            if i + 1 >= len(out):
                raise CoreError("malformed -I in template")
            out[i:i+2] = [f"-I{instance}"]
            replaced = True
            break
        if out[i].startswith("-I") and out[i] != "-I":
            out[i] = f"-I{instance}"
            replaced = True
            break
        i += 1
    if not replaced:
        raise CoreError("unable to replace SITL instance")

    # Remove prior autotest-dir if one was present, then add isolated one.
    cleaned: list[str] = []
    skip = False
    for token in out:
        if skip:
            skip = False
            continue
        if token == "--autotest-dir":
            skip = True
            continue
        if token.startswith("--autotest-dir="):
            continue
        cleaned.append(token)
    cleaned += ["--autotest-dir", str(Path(autotest_dir).resolve())]
    return tuple(cleaned)

