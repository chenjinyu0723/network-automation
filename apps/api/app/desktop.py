"""Windows desktop entry point for the local single-user application."""

from __future__ import annotations

import socket
import sys
import threading
import time
from contextlib import closing

import uvicorn
import webview

from app.ingestion.worker import main as import_worker_main
from app.main import create_app
from app.retrieval.worker import main as embedding_worker_main

_desktop_window: webview.Window | None = None


class DesktopBridge:
    """Small native bridge used only for user-initiated local file selection."""

    def choose_export_path(self, suggested_filename: str, archive_kind: str) -> str | None:
        if not _desktop_window:
            return None
        filters = {
            "manual": ("手册归档 (*.manual.zip)",),
            "topology": ("拓扑归档 (*.topology.json)",),
            "template": ("模板归档 (*.template.json)",),
        }
        selected = _desktop_window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=suggested_filename,
            file_types=filters.get(archive_kind, ("所有文件 (*.*)",)),
        )
        return str(selected[0]) if selected else None


def _run_worker_mode() -> bool:
    """Dispatch background jobs when the frozen exe is launched by a worker."""

    if "--import-worker" in sys.argv:
        sys.argv.remove("--import-worker")
        import_worker_main()
        return True
    if "--embedding-worker" in sys.argv:
        sys.argv.remove("--embedding-worker")
        embedding_worker_main()
        return True
    return False


def _available_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_server(port: int) -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    threading.Thread(target=server.run, name="network-automation-api", daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("本地服务启动超时。请检查 data 目录的写入权限。")
    return server


def main() -> None:
    if _run_worker_mode():
        return
    port = _available_port()
    server = _start_server(port)

    bridge = DesktopBridge()
    window = webview.create_window(
        "AI Agent 工业交换机自动配置",
        f"http://127.0.0.1:{port}",
        width=1540,
        height=980,
        min_size=(1180, 720),
        background_color="#f4f7fb",
        js_api=bridge,
    )
    global _desktop_window
    _desktop_window = window
    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
