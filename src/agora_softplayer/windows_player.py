"""Windows-native player loop.

Bridges DesiredState (written by CMSClient to ``data_dir/state/desired.json``)
into shell SPA commands by calling ``ChromiumPlayer`` methods. Polls
because Windows has no inotify; debounces on ``(mtime, content hash)``
so identical desired states don't re-dispatch.

Scope this milestone (M3a-1): only PLAY mode with image assets.
``STOP`` -> ``stop_playback``; ``SPLASH`` and video assets are logged but
not yet rendered. Video and splash land in M3a-3, slideshows in M3b.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 0.25


def _resolve_asset(name: str, assets_dir: Path) -> Optional[Path]:
    """Locate an asset file under ``assets_dir``.

    Mirrors ``agora/player/service.py:_resolve_asset`` (same subdir
    order: videos, images, splash) so the softplayer asset-name lookup
    matches the Pi one byte for byte.
    """
    for subdir in ("videos", "images", "splash"):
        path = assets_dir / subdir / name
        if path.is_file():
            return path
    return None


class WindowsPlayer:
    """Poll ``desired.json`` and dispatch into a ``ChromiumPlayer``."""

    def __init__(
        self,
        *,
        data_dir: Path,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._desired_path = self._data_dir / "state" / "desired.json"
        self._assets_dir = self._data_dir / "assets"
        self._poll_interval = max(0.05, float(poll_interval_s))

        self._player: Any = None  # ChromiumPlayer, set via attach_player
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._last_sig: Optional[tuple] = None

    # ── Public lifecycle ──

    def attach_player(self, chromium_player: Any) -> None:
        """Bind the ChromiumPlayer that ``_dispatch`` will drive."""
        self._player = chromium_player

    def start(self) -> None:
        if self._thread:
            return
        if self._player is None:
            raise RuntimeError(
                "WindowsPlayer.start() called before attach_player(); "
                "no ChromiumPlayer bound to dispatch into."
            )
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="agora-softplayer-windows-player",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "WindowsPlayer started (poll=%.2fs, desired=%s)",
            self._poll_interval, self._desired_path,
        )

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop_evt.set()
        self._thread.join(timeout=5)
        self._thread = None

    # ── Event sink (shell SPA -> server) ──

    def on_shell_event(self, payload: dict) -> None:
        """Receive shell SPA -> server events.

        M3a-1 only logs them. M3a-2 wires these into ``current.json``.
        """
        logger.debug("shell event: %s", payload)

    # ── Internals ──

    def _poll_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("WindowsPlayer poll iteration failed")
            self._stop_evt.wait(self._poll_interval)

    def _poll_once(self) -> None:
        try:
            raw = self._desired_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.debug("desired.json unreadable: %s", e)
            return

        try:
            mtime = self._desired_path.stat().st_mtime_ns
        except OSError:
            mtime = 0

        sig = (mtime, hashlib.sha1(raw).hexdigest())
        if sig == self._last_sig:
            return
        self._last_sig = sig

        try:
            from shared.models import DesiredState
        except ImportError:
            logger.error(
                "shared.models import failed; shims must be installed first"
            )
            return

        try:
            desired = DesiredState.model_validate_json(raw)
        except Exception as e:
            logger.warning("desired.json parse failed: %s", e)
            return

        self._dispatch(desired)

    def _dispatch(self, desired: Any) -> None:
        from shared.models import PlaybackMode

        if desired.mode == PlaybackMode.STOP:
            logger.info("dispatch: STOP")
            self._player.stop_playback()
            return

        if desired.mode == PlaybackMode.SPLASH:
            logger.info("dispatch: SPLASH (deferred to M3a-3)")
            return

        if not desired.asset:
            logger.info("dispatch: PLAY with no asset; ignoring")
            return

        path = _resolve_asset(desired.asset, self._assets_dir)
        if path is None:
            logger.info(
                "dispatch: asset %r not yet on disk; waiting for fetch",
                desired.asset,
            )
            return

        subdir = path.parent.name
        if subdir == "images":
            logger.info("dispatch: show_image %s", path)
            self._player.show_image(path)
        elif subdir == "videos":
            logger.info(
                "dispatch: show_video (deferred to M3a-3): %s", path
            )
        else:
            logger.info(
                "dispatch: unsupported subdir %r for %s", subdir, path
            )
