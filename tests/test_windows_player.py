"""Tests for the WindowsPlayer dispatch loop."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
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


def _read_current(data_dir: Path) -> dict | None:
    path = data_dir / "state" / "current.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _make_player(data_dir: Path) -> tuple[WindowsPlayer, MagicMock]:
    chromium = MagicMock(name="chromium")
    wp = WindowsPlayer(data_dir=data_dir, poll_interval_s=0.05)
    wp.attach_player(chromium)
    return wp, chromium


def _wait_for(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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

def test_play_image_dispatches_show_image(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play",
        "asset": "hello.jpg",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
    finally:
        wp.stop()
    chromium.show_image.assert_called_once_with(
        tmp_path / "assets" / "images" / "hello.jpg"
    )


def test_play_image_writes_current_playing(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "hello.jpg",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        assert _wait_for(lambda: _read_current(tmp_path) is not None)
    finally:
        wp.stop()
    current = _read_current(tmp_path)
    assert current["pipeline_state"] == "PLAYING"
    assert current["asset"] == "hello.jpg"
    assert current["mode"] == "play"
    assert current["started_at"] is not None
    assert current["error"] is None


def test_stop_mode_dispatches_stop_playback(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "stop",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.stop_playback.called)
        assert _wait_for(lambda: _read_current(tmp_path) is not None)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()
    chromium.show_video.assert_not_called()
    current = _read_current(tmp_path)
    assert current["pipeline_state"] == "READY"
    assert current["asset"] is None
    assert current["mode"] == "splash"


def test_splash_mode_with_no_config_is_no_op(tmp_path: Path) -> None:
    """SPLASH with no persist/splash file configured does nothing."""
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "splash",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        time.sleep(0.4)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()
    chromium.show_video.assert_not_called()
    chromium.show_splash.assert_not_called()
    chromium.stop_playback.assert_not_called()
    # No current.json update because we never dispatched.
    assert _read_current(tmp_path) is None


def test_splash_mode_dispatches_show_splash_with_config(tmp_path: Path) -> None:
    """SPLASH with persist/splash naming a present asset -> show_splash."""
    _seed_assets(tmp_path)
    persist = tmp_path / "persist"
    persist.mkdir(parents=True, exist_ok=True)
    (persist / "splash").write_text("default.png", encoding="utf-8")

    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "splash",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_splash.called)
        assert _wait_for(lambda: _read_current(tmp_path) is not None)
    finally:
        wp.stop()
    chromium.show_splash.assert_called_once_with(
        tmp_path / "assets" / "splash" / "default.png"
    )
    current = _read_current(tmp_path)
    assert current["mode"] == "splash"
    assert current["asset"] == "default.png"
    assert current["pipeline_state"] == "NULL"
    assert current["started_at"] is None


def test_splash_asset_not_on_disk_is_no_op(tmp_path: Path) -> None:
    """SPLASH config referencing a missing file does nothing (waits)."""
    _seed_assets(tmp_path)
    persist = tmp_path / "persist"
    persist.mkdir(parents=True, exist_ok=True)
    (persist / "splash").write_text("absent.png", encoding="utf-8")

    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "splash",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        time.sleep(0.4)
    finally:
        wp.stop()
    chromium.show_splash.assert_not_called()
    assert _read_current(tmp_path) is None


def test_play_video_dispatches_show_video(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play",
        "asset": "clip.mp4",
        "loop": True,
        "loop_count": 3,
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_video.called)
        assert _wait_for(lambda: _read_current(tmp_path) is not None)
    finally:
        wp.stop()
    chromium.show_video.assert_called_once_with(
        tmp_path / "assets" / "videos" / "clip.mp4",
        loop=True,
        muted=False,
        loop_count=3,
    )
    current = _read_current(tmp_path)
    assert current["mode"] == "play"
    assert current["asset"] == "clip.mp4"
    assert current["pipeline_state"] == "PLAYING"
    assert current["started_at"] is not None



def test_missing_asset_does_not_crash(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play",
        "asset": "absent.jpg",
        "timestamp": datetime.now(UTC).isoformat(),
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
    ts = datetime.now(UTC).isoformat()
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
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        # Bump asset; mtime + content both change.
        time.sleep(0.05)
        _write_desired(tmp_path, {
            "mode": "play", "asset": "second.jpg",
            "timestamp": datetime.now(UTC).isoformat(),
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

def test_on_shell_event_ready_is_logged_only(tmp_path: Path, caplog) -> None:
    wp, _ = _make_player(tmp_path)
    with caplog.at_level("DEBUG", logger="agora_softplayer.windows_player"):
        wp.on_shell_event({"event": "ready"})
    assert any("shell event" in r.message for r in caplog.records)
    # `ready` does not produce a current.json update.
    assert _read_current(tmp_path) is None


def test_on_shell_event_ended_writes_ready_state(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "hello.jpg",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        wp.on_shell_event({"event": "ended", "asset": "/assets/images/hello.jpg"})
    finally:
        wp.stop()
    current = _read_current(tmp_path)
    assert current is not None
    assert current["pipeline_state"] == "READY"
    # asset stays so the dashboard still shows what was last on-screen.
    assert current["asset"] == "hello.jpg"


def test_on_shell_event_error_writes_error_state(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "hello.jpg",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        wp.on_shell_event({
            "event": "error", "asset": "/assets/images/hello.jpg",
            "msg": "image load failed",
        })
    finally:
        wp.stop()
    current = _read_current(tmp_path)
    assert current is not None
    assert current["pipeline_state"] == "ERROR"
    assert current["error"] == "image load failed"
    assert current["asset"] == "hello.jpg"


# -- slideshow dispatch --

def _write_slideshow_manifest(data_dir: Path, name: str, slides: list[dict]) -> Path:
    sdir = data_dir / "assets" / "slideshows"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{name}.json"
    path.write_text(
        json.dumps({"name": name, "checksum": "x", "slides": slides}),
        encoding="utf-8",
    )
    return path


def test_slideshow_dispatch_starts_sequencer(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    _write_slideshow_manifest(
        tmp_path, "show",
        [{"name": "hello.jpg", "asset_type": "image", "duration_ms": 5000}],
    )
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "show", "asset_type": "slideshow",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        assert _wait_for(lambda: _read_current(tmp_path) is not None)
    finally:
        wp.stop()
    current = _read_current(tmp_path)
    assert current["asset"] == "show"
    assert current["pipeline_state"] == "PLAYING"


def test_slideshow_missing_manifest_falls_back_to_splash(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    persist = tmp_path / "persist"
    persist.mkdir(parents=True, exist_ok=True)
    (persist / "splash").write_text("default.png", encoding="utf-8")
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "nope", "asset_type": "slideshow",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_splash.called)
    finally:
        wp.stop()
    chromium.show_image.assert_not_called()


def test_stop_during_slideshow_stops_sequencer(tmp_path: Path) -> None:
    _seed_assets(tmp_path)
    _write_slideshow_manifest(
        tmp_path, "show",
        [{"name": "hello.jpg", "asset_type": "image", "duration_ms": 5000}],
    )
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "show", "asset_type": "slideshow",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        assert wp._slideshow is not None and wp._slideshow.is_running()
        _write_desired(tmp_path, {
            "mode": "stop",
            "timestamp": datetime.now(UTC).isoformat(),
        })
        assert _wait_for(lambda: chromium.stop_playback.called)
        assert _wait_for(lambda: not wp._slideshow.is_running())
    finally:
        wp.stop()


def test_image_dispatch_stops_running_slideshow(tmp_path: Path) -> None:
    """Switching from slideshow to a single image tears down the sequencer."""
    _seed_assets(tmp_path)
    _write_slideshow_manifest(
        tmp_path, "show",
        [{"name": "hello.jpg", "asset_type": "image", "duration_ms": 5000}],
    )
    wp, chromium = _make_player(tmp_path)
    _write_desired(tmp_path, {
        "mode": "play", "asset": "show", "asset_type": "slideshow",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    wp.start()
    try:
        assert _wait_for(lambda: chromium.show_image.called)
        assert wp._slideshow.is_running()
        chromium.show_image.reset_mock()
        _write_desired(tmp_path, {
            "mode": "play", "asset": "hello.jpg", "asset_type": "image",
            "timestamp": datetime.now(UTC).isoformat(),
        })
        assert _wait_for(lambda: chromium.show_image.called)
        assert _wait_for(lambda: not wp._slideshow.is_running())
    finally:
        wp.stop()