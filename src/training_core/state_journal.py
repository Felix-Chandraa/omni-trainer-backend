"""Append-only diagnostic stream with explicit provenance and timing.

This is not Assessment evidence, an authoritative simulator clock, or durable
production recording. A unique file is created per invocation; no overwrites.
"""
from __future__ import annotations
import json
import os
import time
import uuid
from pathlib import Path


class StateJournal:
    def __init__(self, root: Path, attempt_id: str, aircraft_id: str):
        if not attempt_id.strip() or not aircraft_id.strip():
            raise ValueError('attempt_id/aircraft_id required')
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f'diagnostic-{uuid.uuid4().hex}.jsonl'
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.file = os.fdopen(fd, 'w', encoding='utf-8')
        self.attempt_id = attempt_id
        self.aircraft_id = aircraft_id
        self.seq = 0

    def record(self, source: str, payload: dict, *, synthetic: bool = False):
        if source not in {'fg_pose', 'mavlink_state'}:
            raise ValueError('invalid journal source')
        self.seq += 1
        row = {'schema': 'omni.r2.diagnostic.v1', 'seq': self.seq,
               'attempt_id': self.attempt_id, 'aircraft_id': self.aircraft_id,
               'source': source, 'synthetic': bool(synthetic),
               'monotonic_ns': time.monotonic_ns(), 'utc_ns': time.time_ns(),
               'payload': payload}
        self.file.write(json.dumps(row, allow_nan=False, sort_keys=True) + '\n')
        self.file.flush()
        return row

    def close(self):
        if not self.file.closed:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
