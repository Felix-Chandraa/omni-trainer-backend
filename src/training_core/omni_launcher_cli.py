"""Read-only CLI for DEV-005 source reconciliation and managed launch dry-run."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .models import CoreError
from .omni_launcher_adapter import (
    audit_legacy_run_demo, bounded_discover, build_initial_plan, choose_jsbsim,
    probe_tcp_port, source_from_root, validate_source,
)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="")
    p.add_argument("--home", default=str(Path.home()))
    p.add_argument("--entry", required=True)
    a = p.parse_args(argv)
    try:
        if a.source:
            root = Path(a.source).expanduser().resolve()
        else:
            found = bounded_discover(a.home)
            if not found:
                print("STATUS: OMNI SOURCE NOT FOUND")
                print("Gunakan --source /path/yang/berisi/run_demo.sh")
                return 2
            if len(found) > 1:
                print("STATUS: MULTIPLE OMNI SOURCES FOUND; pilih eksplisit dengan --source")
                for x in found: print(" -", x)
                return 2
            root = found[0]
        source = source_from_root(root)
        jsb = choose_jsbsim(source, home=a.home)
    except CoreError as exc:
        print(f"STATUS: PREFLIGHT BLOCKED: {exc}")
        return 3

    print("SOURCE:", source.root)
    print("JSBSim selected:", jsb)
    print("\n=== Source checks ===")
    fatal = False
    for c in validate_source(source, jsb):
        label = "OK" if c.ok else c.severity.upper()
        print(f"[{label}] {c.name}: {c.detail}")
        if not c.ok and c.severity == "error": fatal = True

    audit = audit_legacy_run_demo(source)
    print("\n=== Legacy launcher audit ===")
    print("checkout-wide pkill:", audit.uses_project_wide_pkill)
    print("startup model mutation:", audit.mutates_model_on_start)
    print("mission loader companion:", audit.starts_mission_loader)
    print("cleanup trap:", audit.has_cleanup_trap)
    print("Decision: R2 worker will NOT execute run_demo.sh directly.")

    print("\n=== Existing initial ports (read-only probe) ===")
    for port in (5760, 5762, 8000, 8765):
        print(f"tcp {port}: {'OPEN' if probe_tcp_port('127.0.0.1', port) else 'free/not-listening'}")

    if fatal:
        print("STATUS: PREFLIGHT BLOCKED: required source file missing")
        return 4
    try:
        plan = build_initial_plan(source, entry_script=a.entry, jsbsim=jsb)
    except CoreError as exc:
        print(f"STATUS: PLAN BLOCKED: {exc}")
        return 5
    print("\n=== Managed launch plan (DRY RUN ONLY) ===")
    print("cwd:", plan.cwd)
    print("vehicle/frame/location:", plan.vehicle, plan.frame, plan.location)
    print("JSBSim:", plan.jsbsim)
    print("MAVLink outputs:", ", ".join(plan.mavlink_outputs))
    print("mission companion deferred:", plan.mission_companion_deferred)
    print("argv:")
    for i, value in enumerate(plan.entry_argv): print(f"  [{i:02d}] {value}")
    print("STATUS: DEV-005 PREFLIGHT READY; NO SIMULATOR WAS STARTED OR STOPPED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
