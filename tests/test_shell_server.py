"""Tests for the local shell server."""
from __future__ import annotations

import socket
import time
import urllib.request
from pathlib import Path

from agora_softplayer.shell_server import ShellServer, _render_placeholder


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_render_placeholder_includes_data_dir(tmp_path: Path) -> None:
    html = _render_placeholder(tmp_path)
    assert "milestone 1" in html
    assert str(tmp_path) in html
    # 100% in CSS should survive unscathed
    assert "height: 100%" in html


def test_shell_server_serves_root_and_healthz(tmp_path: Path) -> None:
    port = _free_port()
    server = ShellServer(host="127.0.0.1", port=port, data_dir=tmp_path)
    server.start()
    try:
        # uvicorn boots asynchronously; give it a moment
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("shell server never came up")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as r:
            assert r.status == 200

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            body = r.read().decode("utf-8")
            assert "milestone 1" in body
            assert str(tmp_path) in body
    finally:
        server.stop()
