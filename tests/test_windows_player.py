"""Tests for the WindowsPlayer dispatch loop."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agora_softplayer.windows_player import WindowsPlayer, _resolve_asset


pytestmark = pytest.mark.usefixtures("shims_installed")


@pytest.fixture(scope="session")
def shims_installed(tmp_path_factory):
    """Apply shims once per test session so ``shared.models`` is importable.

    ``WindowsPlayer._poll_once`` does ``from shared.models import DesiredState``
    inside the loop, which only resolves after the agora submodule has been
    placed on ``sys.path`` by the shim layer.
    """
    from agora_softplayer import shims
    shims.configure(
        data_dir=tmp_path_factory.mktemp("shim-data"),
        available_slots=1,
    )
    shims.apply_shims()
    yield


def _seed_assets(data_dir: Path) -> Path:
    assets = data_dir / "assets"
    (assets / "images").mkdir(parents=True, exist_ok=True)
    (assets / "videos").mkdir(parents=True, exist_ok=True)
    (assets / "splash").mkdir(parents=True, exist_ok=True)
    img = assets / "images" / "hello.jpg"
    img.write_bytes(b"\xff\xd8\xff fake jpg")
    vid = assets / "videos" / "clip.mp4"
    vid.write_bytes(b"\x00\x00\x00 fake mp4")
    splash = assets / "splash" / "default.png"
    splash.write_bytes(b"\x89PNG fake")
    return assets


def _write_desired(data_dir: Path, payload: dict) -> None:
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "desired.json").write_text(json.dumps(payload), encoding="utf-8")


def _make_player(data_dir: Path) -> tuple[WindowsPlayer, MagicMock]:
    chromium = MagicMock(name="chromium")
    wp = WindowsPlayer(data_dir=data_dir, poll_interval_s=0.05)
    wp.attach_player(chromium)
    return wp, chromium


# ── _resolve_asset ──

def test_resolve_asset_walks_subdirs_in_order(tmp_path: Path) -> None:
    assets = _seed_assets(tmp_path)
    assert _resolve_asset("hello.jpg", assets) == assets / "images" / "hello.jpg"
    assert _resolve_asset("clip.mp4", assets) == assets / "videos" / "clip.mp4"
    assert _resolve_asset("default.png", assets) == assets / "splash" / "default.png"


def test_resolve_asset_returns_none_for_missing(tmp_path: Path) -> None:
    assets = _seed_assets(tmp_path)
    assert _resolve_asset("nope.jpg", assets) is None


# ── start/attach guards ──

def test_start_without_attach_raises(tmp_path: Path) -> None:
    wp = WindowsPlayer(data_dir=tmp_path)
    with pytest.raises(RuntimeError):
        wp.start()


# ── dispatch ──

def _wait_for(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_play_image_dispatches_show_image(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play",
        "asset": "hello.jpg",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
    finally:
        wp.stop()
    chromium.show_image.assert_called_once_with(
        tmp_path / "assets" / "images" / "hello.jpg"
    )


def test_stop_mode_dispatches_stop_playback(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "stop",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.stop_playback.called)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()
    chromium.show_video.assert_not_called()


def test_splash_mode_is_deferred_no_call(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "splash",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wp.start()
    try:
        # Give the loop several poll cycles; nothing should fire.
        time.sleep(0.4)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()
    chromium.show_video.assert_not_called()
    chromium.show_splash.assert_not_called()
    chromium.stop_playback.assert_not_called()


def test_video_is_deferred_in_m3a1(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play",
        "asset": "clip.mp4",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wp.start()
    try:
        time.sleep(0.4)
    finally:
        wp.stop()
    # Video rendering lands in M3a-3; M3a-1 only emits a log line.
    chromium.show_video.assert_not_called()
    chromium.show_image.assert_not_called()


def test_missing_asset_does_not_crash(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play",
        "asset": "absent.jpg",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wp.start()
    try:
        time.sleep(0.3)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()


def test_debounce_skips_identical_desired(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    ts = datetime.now(timezone.utc).isoformat()
    _write_desired(tmp_path, {"mode": "play", "asset": "hello.jpg", "timestamp": ts})
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        # Re-write same bytes with same mtime semantics; expect no extra calls.
        time.sleep(0.3)
    finally:
        wp.stop()
    assert chromium.show_image.call_count == 1


def test_desired_change_triggers_new_dispatch(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    (tmp_path / "assets" / "images" / "second.jpg").write_bytes(b"x")
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "hello.jpg",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        # Bump asset; mtime + content both change.
        time.sleep(0.05)
        _write_desired(tmp_path, {
            "mode": "play", "asset": "second.jpg",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        assert _wait_for(lambda: chromium.show_image.call_count >= 2)
    finally:
        wp.stop()
    assert chromium.show_image.call_count >= 2


def test_no_desired_file_is_silent(tmp_path: Path) -> None:
    wp, chromium = _make_player(tmp_path)
    wp.start()
    try:
        time.sleep(0.3)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()
    chromium.stop_playback.assert_not_called()


# ── on_shell_event ──

def test_on_shell_event_logs_only_in_m3a1(tmp_path: Path, caplog) -> None:
    wp, _ = _make_player(tmp_path)
    with caplog.at_level("DEBUG", logger="agora_softplayer.windows_player"):
        wp.on_shell_event({"event": "ready"})
    assert any("shell event" in r.message for r in caplog.records)
