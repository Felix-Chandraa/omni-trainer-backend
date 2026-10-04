"""DEV-011 remote Student Feather/Cesium launcher."""
from __future__ import annotations

import argparse
import sys
import threading
from urllib.parse import urlencode

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtWebEngineWidgets import QWebEngineView

from config import MAP_FILENAME
from src.server import start_server


class RemoteCesiumWindow(QMainWindow):
    def __init__(self, *, server: str, student_id: str, token: str, http_port: int):
        super().__init__()
        self.setWindowTitle(f"OMNI Remote Student — {student_id}")
        self.resize(1600, 950)
        web = QWebEngineView()
        self.setCentralWidget(web)
        fragment = urlencode({
            "omni_remote": "1",
            "server": server,
            "student_id": student_id,
            "token": token,
        })
        web.load(QUrl(f"http://127.0.0.1:{http_port}/{MAP_FILENAME}#{fragment}"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="e.g. ws://192.168.10.10:9100")
    p.add_argument("--student", required=True, help="e.g. student-1")
    p.add_argument("--token", required=True, help="server-issued token")
    p.add_argument("--http-port", type=int, default=8010)
    a = p.parse_args(argv)
    if not (a.server.startswith("ws://") or a.server.startswith("wss://")):
        p.error("--server must begin with ws:// or wss://")

    threading.Thread(
        target=start_server,
        kwargs={"port": a.http_port},
        daemon=True,
        name="RemoteStaticFileServer",
    ).start()

    app = QApplication(sys.argv)
    app.setApplicationName("OMNI Remote Student")
    window = RemoteCesiumWindow(
        server=a.server,
        student_id=a.student,
        token=a.token,
        http_port=a.http_port,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
