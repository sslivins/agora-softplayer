"""Tests for browser detection."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agora_softplayer.browser import find_browser


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
