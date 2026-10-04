"""R2 managed SITL exec wrapper.

The OMNI virtual-environment interpreter is an explicit launch-contract value.
The path is intentionally not dereferenced because .venv/bin/python3 may be a
symlink to the system interpreter.

No global cleanup, source/model mutation, EEPROM wipe, mission companion, or
flight command is performed here.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def _absolute_without_resolving(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return Path(os.path.abspath(str(p)))


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--ap-dir", required=True)
    p.add_argument("--venv-python", required=True)
    p.add_argument("--jsbsim", required=True)
    p.add_argument("--vehicle", required=True)
    p.add_argument("--frame", required=True)
    p.add_argument("--location", required=True)
    p.add_argument("--param-file", required=True)
    p.add_argument("--enable-fgview", action="store_true")
    p.add_argument("--out", action="append", default=[])
    p.add_argument("--print-plan", action="store_true")
    return p.parse_args(argv)


def build_exec(a):
    ap = Path(a.ap_dir).expanduser().resolve()
    jsb = Path(a.jsbsim).expanduser().resolve()
    param = Path(a.param_file).expanduser().resolve()

    explicit = getattr(a, "venv_python", None)
    explicit_contract = bool(explicit)
    py = _absolute_without_resolving(explicit if explicit else sys.executable)
    sim_vehicle = ap / "Tools" / "autotest" / "sim_vehicle.py"

    for label, path in (
        ("sim_vehicle.py", sim_vehicle),
        ("JSBSim", jsb),
        ("param file", param),
        ("venv python", py),
    ):
        if not path.is_file():
            raise SystemExit(f"missing {label}: {path}")

    # Production CLI always supplies --venv-python and must point to an
    # executable. Older unit fixtures call build_exec() directly with a dummy
    # sys.executable file, so keep that compatibility path non-executable-safe.
    if explicit_contract and not os.access(py, os.X_OK):
        raise SystemExit(f"venv python not executable: {py}")
    if not os.access(jsb, os.X_OK):
        raise SystemExit(f"JSBSim not executable: {jsb}")

    venv_bin = py.parent
    venv_root = venv_bin.parent

    argv = [
        str(py),
        str(sim_vehicle),
        "-v", a.vehicle,
        "-f", a.frame,
        "-L", a.location,
    ]
    if a.enable_fgview:
        argv.append("--enable-fgview")

    for out in a.out:
        if not out.startswith("udp:127.0.0.1:"):
            raise SystemExit(f"non-loopback output rejected: {out}")
        argv.append(f"--out={out}")

    argv.append(f"--add-param-file={param}")

    env = os.environ.copy()
    inherited = env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(venv_root)
    env["PATH"] = os.pathsep.join(
        p for p in (str(venv_bin), str(jsb.parent), inherited) if p
    )
    env.pop("PYTHONHOME", None)
    env["OMNI_MANAGED_JSBSIM"] = str(jsb)
    env["OMNI_MANAGED_VENV"] = str(venv_root)
    env["OMNI_MANAGED_VENV_PYTHON"] = str(py)
    env["OMNI_MANAGED_RUNTIME"] = "r2-dev006-rev5"

    return argv, env, ap


def main(argv=None) -> int:
    a = parse_args(argv)
    exec_argv, env, cwd = build_exec(a)

    if a.print_plan:
        print("cwd=", cwd)
        print("python=", exec_argv[0])
        print("VIRTUAL_ENV=", env["VIRTUAL_ENV"])
        print("PATH_HEAD=", os.pathsep.join(env["PATH"].split(os.pathsep)[:2]))
        print("JSBSim=", env["OMNI_MANAGED_JSBSIM"])
        print("argv=", exec_argv)
        return 0

    os.chdir(cwd)
    os.execvpe(exec_argv[0], exec_argv, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
