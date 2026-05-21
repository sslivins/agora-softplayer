"""Tests for browser detection."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agora_softplayer.browser import find_browser, launch_browser


def test_find_browser_returns_edge_when_present(tmp_path: Path) -> None:
    fake_edge = tmp_path / "msedge.exe"
    fake_edge.write_bytes(b"")
    with patch("agora_softplayer.browser.EDGE_CANDIDATES", [str(fake_edge)]):
        assert find_browser() == fake_edge


def test_find_browser_falls_back_to_chrome(tmp_path: Path) -> None:
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    with patch("agora_softplayer.browser.EDGE_CANDIDATES", []), \
         patch("agora_softplayer.browser.CHROME_CANDIDATES", [str(fake_chrome)]):
        assert find_browser() == fake_chrome


def test_find_browser_returns_none_when_nothing_found() -> None:
    with patch("agora_softplayer.browser.EDGE_CANDIDATES", []), \
         patch("agora_softplayer.browser.CHROME_CANDIDATES", []), \
         patch("agora_softplayer.browser.shutil.which", return_value=None):
        assert find_browser() is None


def test_launch_browser_passes_autoplay_policy(tmp_path: Path) -> None:
    """Regression: shell needs Chromium's autoplay gate disabled, otherwise
    unmuted <video> elements only render the first frame."""
    fake_browser = tmp_path / "msedge.exe"
    fake_browser.write_bytes(b"")
    user_data_dir = tmp_path / "profile"
    with patch("agora_softplayer.browser.subprocess.Popen") as popen:
        launch_browser(fake_browser, url="http://localhost:9000/", user_data_dir=user_data_dir)
    argv = popen.call_args.args[0]
    assert "--autoplay-policy=no-user-gesture-required" in argv
