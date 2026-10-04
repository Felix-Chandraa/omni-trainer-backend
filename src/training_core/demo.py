"""Safe metadata-only demonstration: no simulator, sockets, Qt or aircraft commands."""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import Actor, Role, SessionManager, State, Store


def main():
    with tempfile.TemporaryDirectory(prefix="omni-r2-demo-") as tmp:
        store = Store(Path(tmp) / "metadata.sqlite")
        manager = SessionManager(store)
        instructor = Actor("instructor-demo", Role.INSTRUCTOR)
        session = manager.create_session(instructor, "student-demo")
        exercise = manager.create_exercise(instructor, session, "omni-manual-landing@draft")
        first = manager.create_attempt(instructor, exercise, "omni-1")
        print(f"created Session={session} Exercise={exercise} Attempt={first.id} generation={first.generation}")
        first = manager.transition(instructor, first.id, State.READY, expected_revision=first.revision,
                                   reason="engineering fixture", readiness_verified=True)
        first = manager.transition(instructor, first.id, State.ACTIVE, expected_revision=first.revision,
                                   reason="engineering fixture only")
        first = manager.transition(instructor, first.id, State.ENDED, expected_revision=first.revision,
                                   reason="engineering demonstration complete")
        second = manager.create_attempt(instructor, exercise, "omni-1")
        print(f"ended first={first.state.value}; new Attempt={second.id} generation={second.generation}")
        print(f"recorded events={len(store.events())} (no flight validation implied)")
        store.close()


if __name__ == "__main__":
    main()
