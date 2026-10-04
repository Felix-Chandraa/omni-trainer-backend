"""Owned child process lifecycle foundation, without PyQt or implicit startup.

Does not yet implement an authoritative JSBSim/ArduPilot adapter, MAVLink/FG
multiplexing, port allocation, persistent restart or full pause. Never pass
legacy run_demo.sh here: its cleanup may terminate other project processes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .models import CoreError


@dataclass(frozen=True)
class WorkerSpec:
    aircraft_id: str
    argv: tuple[str, ...]
    cwd: Path
    runtime_dir: Path


class AircraftWorker:
    def __init__(self, spec: WorkerSpec):
        self.spec = spec
        self._proc: subprocess.Popen | None = None
        self._log = None
        self._lock = threading.RLock()

    def start(self) -> int:
        with self._lock:
            if self._proc is not None:
                raise CoreError("worker already started; cannot launch duplicate child")
            if not self.spec.aircraft_id.strip() or not self.spec.argv or any(not x for x in self.spec.argv):
                raise CoreError("invalid worker spec")
            if not self.spec.cwd.is_dir():
                raise CoreError("working directory does not exist")
            self.spec.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._log = (self.spec.runtime_dir / "worker.log").open("ab", buffering=0)
            try:
                # Keep fd0 open without exposing an interactive control path.
                # Some managed children (notably MAVProxy launched by sim_vehicle)
                # interpret EOF on stdin as a request to exit. PIPE gives them a
                # blocking, owned stdin while AircraftWorker never writes to it.
                self._proc = subprocess.Popen(list(self.spec.argv), cwd=self.spec.cwd,
                                              stdin=subprocess.PIPE, stdout=self._log,
                                              stderr=subprocess.STDOUT, start_new_session=True,
                                              close_fds=True)
            except BaseException:
                self._log.close()
                self._log = None
                raise
            return self._proc.pid

    def poll(self) -> int | None:
        with self._lock:
            if self._proc is None:
                raise CoreError("worker not started")
            return self._proc.poll()

    def stop(self, timeout: float = 3.0) -> int:
        """Signal ONLY the process group created for this worker, then reap."""
        with self._lock:
            if self._proc is None:
                raise CoreError("worker not started")
            proc = self._proc
            if proc.poll() is None:
                # graceful EOF phase: close only the worker-owned stdin pipe.
                # This lets trusted wrappers such as sim_vehicle/MAVProxy run
                # their own scoped teardown before we signal the process group.
                if proc.stdin is not None and not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

                graceful_timeout = min(3.0, max(0.1, timeout * 0.6))
                try:
                    proc.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:
                    # start_new_session=True establishes the owned process group.
                    os.killpg(proc.pid, signal.SIGTERM)
                    term_timeout = max(0.1, timeout - graceful_timeout)
                    try:
                        proc.wait(timeout=term_timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)

            code = proc.wait()
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if self._log:
                self._log.close()
                self._log = None
            self._proc = None
            return code
