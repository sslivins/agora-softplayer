"""Browser detection + launch on Windows.

Auto-detects Microsoft Edge (preinstalled on all modern Windows) first,
then Google Chrome. Both are Chromium-based and accept the kiosk-ish
flags we care about (``--app=URL``, ``--user-data-dir=``,
``--no-first-run``).

The caller is responsible for waiting on / terminating the returned
``subprocess.Popen``.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> Path | None:
    """Locate Edge or Chrome on the host. Returns ``None`` if neither found."""
    for raw in EDGE_CANDIDATES + CHROME_CANDIDATES:
        path = Path(os.path.expandvars(raw))
        if path.is_file():
            logger.debug("Detected browser: %s", path)
            return path

    for exe in ("msedge.exe", "chrome.exe"):
        which = shutil.which(exe)
        if which:
            logger.debug("Detected browser via PATH: %s", which)
            return Path(which)

    return None


def launch_browser(
    browser_path: Path,
    *,
    url: str,
    user_data_dir: Path,
) -> subprocess.Popen:
    """Spawn the browser as a windowed app pointed at ``url``."""
    user_data_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        str(browser_path),
        f"--app={url}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI",
        "--disable-popup-blocking",
        "--disable-component-update",
    ]
    logger.debug("Launching: %s", " ".join(argv))
    return subprocess.Popen(argv, close_fds=False)
