"""R2 DEV-005: read-only reconciliation of the existing OMNI launch chain.

This module deliberately does NOT execute run_demo.sh. The legacy launcher has
single-checkout cleanup semantics and startup-time source mutation that are not
safe contracts for future multi-aircraft workers. DEV-005 extracts an explicit,
headless launch plan instead.

No function in this module starts/stops a process or modifies the OMNI checkout.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import socket
from typing import Iterable

from .models import CoreError


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"


@dataclass(frozen=True)
class OmniSource:
    root: Path
    run_demo: Path
    ap_dir: Path
    sim_vehicle: Path
    venv_python: Path
    omni_param: Path
    model_src: Path
    model_dst: Path
    mission: Path
    mission_loader: Path


@dataclass(frozen=True)
class LegacyAudit:
    uses_project_wide_pkill: bool
    mutates_model_on_start: bool
    starts_mission_loader: bool
    has_cleanup_trap: bool


@dataclass(frozen=True)
class LaunchPlan:
    cwd: Path
    entry_argv: tuple[str, ...]
    jsbsim: Path
    mavlink_outputs: tuple[str, ...]
    location: str
    frame: str
    vehicle: str
    mission_companion_deferred: bool


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def source_from_root(root: str | Path) -> OmniSource:
    root = _resolved(root)
    ap = root / "omnitrainer-sitl" / "ardupilot"
    source = OmniSource(
        root=root,
        run_demo=root / "run_demo.sh",
        ap_dir=ap,
        sim_vehicle=ap / "Tools" / "autotest" / "sim_vehicle.py",
        venv_python=root / ".venv" / "bin" / "python3",
        omni_param=ap / "Tools" / "autotest" / "aircraft" / "Omni-Trainer" / "omni_trainer_sitl.parm",
        model_src=root / "omnitrainer-sitl" / "assets" / "ardupilot" / "aircraft" / "Omni-Trainer" / "Omni-Trainer.xml",
        model_dst=ap / "Tools" / "autotest" / "aircraft" / "Omni-Trainer" / "Omni-Trainer.xml",
        mission=root / "omnitrainer-sitl" / "assets" / "missions" / "wiriadinata_default.txt",
        mission_loader=root / "omnitrainer-sitl" / "scripts" / "load_default_mission.py",
    )
    if not source.run_demo.is_file() or not source.sim_vehicle.is_file():
        raise CoreError(f"not an OMNI source root: {root}")
    return source


def bounded_discover(home: str | Path) -> list[Path]:
    """Find likely roots without scanning arbitrary mounts or hidden caches."""
    home = _resolved(home)
    seeds = [home / "omni", home / "Downloads", home]
    found: list[Path] = []
    seen: set[Path] = set()
    for seed in seeds:
        if not seed.is_dir():
            continue
        # Expected project locations first.
        candidates = [seed, *(p for p in seed.glob("omni_flight*")), *(p for p in seed.glob("*omni*"))]
        for c in candidates:
            try: c = c.resolve()
            except OSError: continue
            if c in seen or not c.is_dir(): continue
            seen.add(c)
            if (c / "run_demo.sh").is_file() and (c / "omnitrainer-sitl" / "ardupilot" / "Tools" / "autotest" / "sim_vehicle.py").is_file():
                found.append(c)
        if found:
            break
    return sorted(set(found))


def audit_legacy_run_demo(source: OmniSource) -> LegacyAudit:
    text = source.run_demo.read_text(errors="replace")
    return LegacyAudit(
        uses_project_wide_pkill=("pkill -f" in text and "AP_DIR" in text),
        mutates_model_on_start=("install -m" in text and "MODEL_SRC" in text and "MODEL_DST" in text),
        starts_mission_loader=("load_default_mission.py" in text or "LOADER_PID" in text),
        has_cleanup_trap=("trap cleanup" in text),
    )


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def choose_jsbsim(source: OmniSource, *, home: str | Path | None = None,
                   override: str | Path | None = None) -> Path:
    """Resolve an executable deterministically; do not execute it here."""
    candidates: list[Path] = []
    explicit = override or os.environ.get("OMNI_JSBSIM_BIN")
    if explicit:
        candidates.append(_resolved(explicit))
    if home is not None:
        h = _resolved(home)
        candidates.extend([
            h / "jsbsim-source" / "build" / "src" / "JSBSim",
            h / "jsbsim-source" / "build" / "src" / "jsbsim",
        ])
    candidates.extend([
        source.root / ".venv" / "bin" / "JSBSim",
        source.root / ".venv" / "bin" / "jsbsim",
    ])
    for name in ("JSBSim", "jsbsim"):
        hit = shutil.which(name)
        if hit:
            candidates.append(Path(hit).resolve())
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    raise CoreError("JSBSim executable not found; set OMNI_JSBSIM_BIN or install/build a known executable")


def validate_source(source: OmniSource, jsbsim: Path | None = None) -> list[Check]:
    required = [
        ("run_demo.sh", source.run_demo),
        ("sim_vehicle.py", source.sim_vehicle),
        ("venv python", source.venv_python),
        ("OMNI parameter file", source.omni_param),
        ("model source", source.model_src),
        ("model installed", source.model_dst),
        ("default mission", source.mission),
        ("mission loader", source.mission_loader),
    ]
    checks = [Check(name, p.is_file(), str(p)) for name, p in required]
    if jsbsim is not None:
        checks.append(Check("JSBSim executable", jsbsim.is_file() and os.access(jsbsim, os.X_OK), str(jsbsim)))
    if source.model_src.is_file() and source.model_dst.is_file():
        same = sha256(source.model_src) == sha256(source.model_dst)
        checks.append(Check("model source == installed copy", same,
                            "hash match" if same else "hash mismatch; managed worker must not silently overwrite checkout",
                            "warning"))
    audit = audit_legacy_run_demo(source)
    checks.append(Check("legacy cleanup suitable for worker isolation", not audit.uses_project_wide_pkill,
                        "run_demo.sh uses pkill scoped to the whole ArduPilot checkout" if audit.uses_project_wide_pkill else "no checkout-wide pkill found",
                        "warning"))
    checks.append(Check("legacy startup is read-only", not audit.mutates_model_on_start,
                        "run_demo.sh copies model into ArduPilot tree at startup" if audit.mutates_model_on_start else "no startup model copy found",
                        "warning"))
    return checks


def probe_tcp_port(host: str, port: int, timeout: float = 0.08) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_initial_plan(source: OmniSource, *, entry_script: str | Path, jsbsim: Path,
                       mavlink_outputs: Iterable[str] = ("udp:127.0.0.1:14550", "udp:127.0.0.1:14555"),
                       location: str = "Wiriadinata") -> LaunchPlan:
    """Build the 1+1 managed launch contract without running it.

    The project venv interpreter is carried explicitly. Linux venv Python
    executables are commonly symlinks to the system interpreter, so resolving
    sys.executable is not a safe way to identify the venv.
    """
    entry = _resolved(entry_script)
    if not entry.is_file():
        raise CoreError("managed entry script missing")

    venv_python = source.venv_python
    if not venv_python.is_file() or not os.access(venv_python, os.X_OK):
        raise CoreError(f"OMNI venv python missing/not executable: {venv_python}")

    outs = tuple(mavlink_outputs)
    if not outs or any(not o.startswith("udp:127.0.0.1:") for o in outs):
        raise CoreError("DEV-005 permits explicit loopback UDP outputs only")

    args = [
        str(venv_python),
        str(entry),
        "--ap-dir", str(source.ap_dir),
        "--venv-python", str(venv_python),
        "--jsbsim", str(jsbsim),
        "--vehicle", "ArduPlane",
        "--frame", "jsbsim:Omni-Trainer",
        "--location", location,
        "--param-file", str(source.omni_param),
        "--enable-fgview",
    ]
    for out in outs:
        args += ["--out", out]

    return LaunchPlan(
        cwd=source.ap_dir,
        entry_argv=tuple(args),
        jsbsim=jsbsim,
        mavlink_outputs=outs,
        location=location,
        frame="jsbsim:Omni-Trainer",
        vehicle="ArduPlane",
        mission_companion_deferred=True,
    )
