"""Tests for the aux server (/healthz, /about)."""
from __future__ import annotations

import json
import socket
import time
import urllib.request
from pathlib import Path

from agora_softplayer.aux_server import AuxServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_and_wait(server: AuxServer, port: int) -> None:
    server.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=1
            ) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError("aux server never came up")


def test_healthz_returns_ok(tmp_path: Path) -> None:
    port = _free_port()
    server = AuxServer(
        host="127.0.0.1", port=port, data_dir=tmp_path,
        cms_url=None, available_slots=1,
    )
    try:
        _start_and_wait(server, port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as r:
            assert json.loads(r.read())["status"] == "ok"
    finally:
        server.stop()


def test_about_reports_configured_fields(tmp_path: Path) -> None:
    (tmp_path / "device_id").write_text("dev-XYZ\n", encoding="utf-8")
    port = _free_port()
    server = AuxServer(
        host="127.0.0.1", port=port, data_dir=tmp_path,
        cms_url="http://cms.example/", available_slots=2,
    )
    try:
        _start_and_wait(server, port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/about") as r:
            data = json.loads(r.read())
        assert data["device_id"] == "dev-XYZ"
        assert data["cms_url"] == "http://cms.example/"
        assert data["available_slots"] == 2
        assert data["data_dir"] == str(tmp_path)
        assert "version" in data
    finally:
        server.stop()


def test_about_handles_missing_device_id(tmp_path: Path) -> None:
    port = _free_port()
    server = AuxServer(
        host="127.0.0.1", port=port, data_dir=tmp_path,
        cms_url=None, available_slots=1,
    )
    try:
        _start_and_wait(server, port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/about") as r:
            data = json.loads(r.read())
        assert data["device_id"] is None
        assert data["cms_url"] is None
    finally:
        server.stop()
