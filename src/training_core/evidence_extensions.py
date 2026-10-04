"""DEV-016 evidence-completeness helpers.

Additive only: this module never changes the command path or aircraft physics.
It attaches immutable worker artifacts to an Attempt package before recorder
finalization builds integrity/index metadata.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import time
from typing import Any


class EvidenceExtensionError(RuntimeError):
    pass


def _candidate_paths_from_recorder(recorder: Any):
    # Prefer explicit path-like attributes, then inspect instance values.
    # getattr() intentionally supports properties/class attributes used by
    # adapters/test doubles that do not appear in vars(instance).
    values: list[Any] = []
    for name in (
        "package_dir", "package_root", "evidence_dir", "evidence_root",
        "root", "base_dir", "_package_dir", "_package_root",
        "_evidence_dir", "_root", "_base_dir",
    ):
        try:
            value = getattr(recorder, name)
        except Exception:
            continue
        if value is not None:
            values.append(value)

    try:
        values.extend(vars(recorder).values())
    except TypeError:
        pass

    seen: set[Path] = set()
    for value in values:
        if isinstance(value, Path):
            candidate = value.expanduser()
        elif isinstance(value, str) and ("/" in value or value.startswith(".")):
            try:
                candidate = Path(value).expanduser()
            except Exception:
                continue
        else:
            continue
        for q in (candidate, candidate.parent):
            try:
                q = q.resolve()
            except Exception:
                continue
            if q not in seen:
                seen.add(q)
                yield q


def evidence_root(recorder: Any) -> Path:
    """Find the current package root without assuming a private attr name."""
    attempt_id = str(
        getattr(recorder, "attempt_id", "")
        or getattr(recorder, "_attempt_id", "")
        or ""
    )
    candidates = list(_candidate_paths_from_recorder(recorder))
    for p in candidates:
        if p.is_dir() and (p / "server").is_dir() and (
            (p / "manifest.json").exists() or p.name == attempt_id
        ):
            return p
    for p in candidates:
        if not p.is_dir():
            continue
        if attempt_id:
            q = p / attempt_id
            if q.is_dir() and (q / "server").is_dir():
                return q
    raise EvidenceExtensionError(
        "cannot locate Attempt evidence root from recorder; refusing to place .BIN"
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _stable_size(path: Path, wait_s: float = 0.20) -> int:
    a = path.stat().st_size
    time.sleep(wait_s)
    b = path.stat().st_size
    if b < a:
        raise EvidenceExtensionError(f"BIN shrank while reading: {path}")
    return b


def _bin_candidates(runtime_root: Path, attempt_id: str) -> list[Path]:
    runtime_root = runtime_root.expanduser().resolve()
    if not runtime_root.is_dir():
        raise EvidenceExtensionError(f"runtime root missing: {runtime_root}")
    all_bins = [
        p.resolve()
        for p in runtime_root.rglob("*")
        if p.is_file()
        and p.suffix.lower() == ".bin"
        and "evidence" not in {x.lower() for x in p.parts}
    ]
    if not all_bins:
        return []
    owned = [p for p in all_bins if attempt_id and attempt_id in p.parts]
    if owned:
        return sorted(owned, key=lambda p: (p.stat().st_mtime_ns, str(p)))
    if len(all_bins) == 1:
        return all_bins
    raise EvidenceExtensionError(
        "multiple ArduPilot .BIN files found but none can be attributed "
        f"to attempt {attempt_id}; refusing ambiguous attachment"
    )


def attach_ardupilot_bins(
    recorder: Any,
    runtime_root: str | os.PathLike[str],
    attempt_id: str,
) -> list[dict[str, Any]]:
    """Copy all unambiguous ArduPilot DataFlash logs into server evidence.

    Call this before recorder.finalize() so normal index/integrity generation
    includes the copied .BIN files.
    """
    root = evidence_root(recorder)
    sources = _bin_candidates(Path(runtime_root), str(attempt_id))
    if not sources:
        raise EvidenceExtensionError(
            f"no ArduPilot .BIN found for attempt {attempt_id} under {runtime_root}"
        )
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for idx, src in enumerate(sources, 1):
        _stable_size(src)
        dst = server / ("aircraft.BIN" if len(sources) == 1 else f"aircraft-{idx:03d}.BIN")
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        records.append({
            "source": str(src),
            "evidence_path": str(dst.relative_to(root)),
            "size_bytes": dst.stat().st_size,
            "sha256": _sha256(dst),
        })
    meta = server / "ardupilot-bin.json"
    tmp_meta = meta.with_suffix(".json.tmp")
    tmp_meta.write_text(
        json.dumps({
            "schema": "omni.ardupilot-bin.v1",
            "attempt_id": str(attempt_id),
            "captured_utc_ns": time.time_ns(),
            "files": records,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_meta, meta)
    return records

# DEV016_STRICT_DATAFLASH_V2
def attach_ardupilot_bins(recorder, runtime_root, attempt_id):
    """Attach only ArduPilot DataFlash logs for exactly this Attempt.

    Contract:
    - source must be under direct/<attempt_id>/plane/logs/
    - source suffix must be exact uppercase .BIN,
    - eeprom.bin and arbitrary *.bin are never DataFlash evidence,
    - one source log becomes server/aircraft.BIN,
    - multiple logs remain separate immutable files,
    - metadata carries source path, size and SHA-256.
    """
    from pathlib import Path
    import hashlib
    import json
    import shutil

    root = evidence_root(recorder)
    runtime = Path(runtime_root).expanduser().resolve()
    attempt = str(attempt_id)

    preferred = runtime / "direct" / attempt / "plane" / "logs"
    candidates = []
    if preferred.is_dir():
        candidates = sorted(
            p.resolve() for p in preferred.glob("*.BIN")
            if p.is_file() and p.stat().st_size > 0
        )

    # Fail closed: never fall back to arbitrary .bin files such as eeprom.bin.
    if not candidates:
        raise EvidenceExtensionError(
            f"no ArduPilot DataFlash .BIN under {preferred}; refusing arbitrary .bin attachment"
        )

    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)

    # Remove only files created by THIS invocation before writing. Existing
    # finalized evidence packages are never passed here and remain immutable.
    output_paths = []
    records = []
    multiple = len(candidates) > 1

    for idx, src in enumerate(candidates, start=1):
        name = f"aircraft-{idx:03d}.BIN" if multiple else "aircraft.BIN"
        dst = server / name
        shutil.copy2(src, dst)
        sha = hashlib.sha256(dst.read_bytes()).hexdigest()
        records.append({
            "source_path": str(src),
            "evidence_path": str(dst.relative_to(root)),
            "size_bytes": dst.stat().st_size,
            "sha256": sha,
        })
        output_paths.append(dst)

    meta = {
        "schema_version": "omni.ardupilot-dataflash.v2",
        "attempt_id": attempt,
        "selection_policy": "direct/<attempt_id>/plane/logs/*.BIN",
        "files": records,
    }
    (server / "ardupilot-bin.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n"
    )
    return output_paths
