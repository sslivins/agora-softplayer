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


def _start_and_wait(server: ShellServer, port: int) -> None:
    server.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError("shell server never came up")


def test_shell_server_falls_back_to_placeholder(tmp_path: Path) -> None:
    # Force placeholder mode by pointing shell_dir at an empty directory.
    empty = tmp_path / "no-shell"
    empty.mkdir()
    port = _free_port()
    server = ShellServer(
        host="127.0.0.1", port=port, data_dir=tmp_path,
        cms_url=None, available_slots=1, shell_dir=empty,
    )
    try:
        _start_and_wait(server, port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            body = r.read().decode("utf-8")
            assert "milestone 1" in body
            assert str(tmp_path) in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/about") as r:
            import json
            data = json.loads(r.read())
            assert data["shell_source"] == "placeholder"
            assert data["available_slots"] == 1
            assert data["cms_url"] is None
    finally:
        server.stop()


def test_shell_server_serves_real_shell_when_present(tmp_path: Path) -> None:
    # Synthesize a minimal shell dir to prove the real-shell branch.
    shell = tmp_path / "shell"
    shell.mkdir()
    (shell / "index.html").write_text(
        "<!doctype html><html><body><h1>real shell here</h1></body></html>",
        encoding="utf-8",
    )
    (shell / "player.js").write_text("// stub", encoding="utf-8")

    port = _free_port()
    server = ShellServer(
        host="127.0.0.1", port=port, data_dir=tmp_path,
        cms_url="ws://example/ws", available_slots=2, shell_dir=shell,
    )
    try:
        _start_and_wait(server, port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            body = r.read().decode("utf-8")
            assert "real shell here" in body
        # static asset accessible via the / mount
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/player.js") as r:
            assert b"// stub" in r.read()
        # /about reports the real-shell source + the configured CMS / slots
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/about") as r:
            import json
            data = json.loads(r.read())
            assert data["shell_source"] == "agora-real"
            assert data["available_slots"] == 2
            assert data["cms_url"] == "ws://example/ws"
    finally:
        server.stop()
