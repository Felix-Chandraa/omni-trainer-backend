"""DEV-004 safe demo: launches only a tiny Python fixture child, never the simulator."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time

from .models import Actor, Role
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator


def main():
    with tempfile.TemporaryDirectory(prefix="omni-r2-dev004-") as td:
        root = Path(td)
        store = Store(root / "metadata.sqlite")
        manager = SessionManager(store)
        instructor = Actor("instructor-dev004", Role.INSTRUCTOR)
        sid = manager.create_session(instructor, "student-dev004")
        eid = manager.create_exercise(instructor, sid, "worker-lifecycle@engineering")
        attempt = manager.create_attempt(instructor, eid, "omni-1")
        coord = AttemptWorkerCoordinator(store, root / "workers")
        health = coord.start(instructor, attempt.id,
                             argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                             cwd=root)
        print(f"started attempt={attempt.id} aircraft={attempt.aircraft_id} pid={health.pid} healthy={health.healthy}")
        health = coord.health(instructor, attempt.id)
        print(f"health status={health.status} healthy={health.healthy}")
        manifest = Path(health.runtime_dir) / "worker.json"
        print(f"manifest={manifest}")
        print(f"manifest_schema={json.loads(manifest.read_text())['schema']}")
        stopped = coord.stop(instructor, attempt.id, timeout=1.0)
        print(f"stopped status={stopped.status} exit_code={stopped.exit_code}")
        names = [e["event_type"] for e in store.events(attempt.id)]
        print("events=" + ",".join(names))
        store.close()


if __name__ == "__main__":
    main()
