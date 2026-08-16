"""GOAI desktop terminal entry point.

The research engine remains the Python backend and the existing local HTTP
contract remains the single source of truth.  This module adds a native
desktop window with Qt WebEngine so the terminal behaves like an application:
no browser chrome, no remote page, no second data path.
"""

from __future__ import annotations

import argparse
import sys
import threading
from http.server import ThreadingHTTPServer

from PySide6.QtCore import QUrl, Qt
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from src.ui_server import Handler


class TerminalWindow(QMainWindow):
    """Native shell around the local GOAI terminal surface."""

    def __init__(self, server: ThreadingHTTPServer, *, maximized: bool = True) -> None:
        super().__init__()
        self._server = server
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="goai-ui-server",
            daemon=True,
        )
        self._server_thread.start()

        self.setWindowTitle("GOAI / 研究终端")
        self.setMinimumSize(1180, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        view = QWebEngineView(self)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        settings = view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            False,
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            False,
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled,
            True,
        )
        view.load(QUrl(self.url))
        self.setCentralWidget(view)

        if maximized:
            self.showMaximized()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API naming)
        self._server.shutdown()
        self._server.server_close()
        self._server_thread.join(timeout=2)
        event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 GOAI 原生桌面研究终端")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="本地回环服务端口；默认自动选择空闲端口",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="以普通窗口启动，而不是最大化",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    app = QApplication(sys.argv)
    app.setApplicationName("GOAI")
    app.setOrganizationName("GOAI")
    window = TerminalWindow(server, maximized=not args.windowed)
    window.show()
    print(f"GOAI desktop terminal: {window.url}", flush=True)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
