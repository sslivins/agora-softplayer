"""Windows-native player loop.

Bridges DesiredState (written by CMSClient to ``data_dir/state/desired.json``)
into shell SPA commands by calling ``ChromiumPlayer`` methods. Polls
because Windows has no inotify; debounces on ``(mtime, content hash)``
so identical desired states don't re-dispatch.

Scope this milestone (M3a-3): full M3a feature set. PLAY image, PLAY
video, SPLASH and STOP all dispatch into ChromiumPlayer and reflect
in ``data_dir/state/current.json`` so CMSClient's heartbeat publishes
them up to the CMS dashboard.

Pipeline-state convention (matches Pi values consumed by
agora-cms/cms/templates/devices.html):
  * "PLAYING" -- after a successful show_image / show_video dispatch
  * "NULL"    -- splash on-screen (mode=SPLASH; CMS badge = "Splash")
  * "READY"   -- shell finished a video / asset was stopped
  * "ERROR"   -- shell reported error or dispatch failed
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agora_softplayer._slideshow import SlideshowSequencer

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 0.25


def _resolve_asset(name: str, assets_dir: Path) -> Path | None:
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


def _read_splash_config(persist_dir: Path) -> str | None:
    """Return the user-configured splash asset name, or None.

    CMSClient writes ``<persist_dir>/splash`` as a plain text file
    containing the asset filename (see
    ``cms_client.service.CMSClient._persist_splash``). Matches
    ``agora/player/service.py:_find_splash`` step 1.
    """
    path = persist_dir / "splash"
    try:
        name = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return name or None


class WindowsPlayer:
    """Poll ``desired.json`` and dispatch into a ``ChromiumPlayer``.

    Also owns the ``current.json`` writer: every transition through
    this class produces a CurrentState that CMSClient picks up on its
    next heartbeat.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._desired_path = self._data_dir / "state" / "desired.json"
        self._current_path = self._data_dir / "state" / "current.json"
        self._assets_dir = self._data_dir / "assets"
        self._persist_dir = self._data_dir / "persist"
        self._poll_interval = max(0.05, float(poll_interval_s))

        self._player: Any = None  # ChromiumPlayer, set via attach_player
        self._slideshow: SlideshowSequencer | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._last_sig: tuple | None = None
        self._current_asset: str | None = None
        # Stable-state debounce for single-asset dispatch. CMS
        # republishes carry a fresh ``desired.timestamp``, so the
        # file-hash debounce in ``_poll_once`` doesn't catch them and
        # without this we'd re-fire show_image / show_video on every
        # WPS sync. Tuple shape: ``(mode, asset_type, asset, loop,
        # loop_count)``. Slideshow has its own ``manifest_unchanged``
        # path; this key is bypassed for slideshow dispatches.
        self._last_dispatch_key: tuple | None = None

    # -- Public lifecycle ----------------------------------------------------

    def attach_player(self, chromium_player: Any) -> None:
        """Bind the ChromiumPlayer that ``_dispatch`` will drive."""
        self._player = chromium_player
        self._slideshow = SlideshowSequencer(
            player=chromium_player,
            assets_dir=self._assets_dir,
            on_done=self._on_slideshow_done,
        )

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

    # -- Event sink (shell SPA -> server) ------------------------------------

    def on_shell_event(self, payload: dict) -> None:
        """Receive shell SPA -> server events and reflect them in current.json."""
        logger.debug("shell event: %s", payload)
        event = payload.get("event")
        if event == "ended":
            asset_url = payload.get("asset")
            # Give the slideshow sequencer first crack at the event --
            # if it matched a play_to_end claim, the sequencer has
            # already advanced + updated current.json, so DON'T also
            # write READY here (that would clobber the new PLAYING).
            if self._slideshow is not None and self._slideshow.on_shell_ended(
                asset_url,
            ):
                return
            # Asset finished playback. Keep mode/asset on CurrentState
            # so the CMS still shows what was last on-screen, but mark
            # the pipeline as READY (not PLAYING) so the badge flips.
            self._write_current(
                asset=self._current_asset,
                pipeline_state="READY",
            )
        elif event == "error":
            self._write_current(
                asset=self._current_asset,
                pipeline_state="ERROR",
                error=str(payload.get("msg") or "shell reported error"),
            )
        # "ready" is a chatty connect-time event from the SPA; nothing
        # to reflect in CurrentState.

    # -- Internals -----------------------------------------------------------

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
            if self._last_dispatch_key == ("stop", None, None, False, None):
                logger.debug("dispatch: STOP already in effect -- skipping")
                return
            logger.info("dispatch: STOP")
            self._stop_slideshow_if_running()
            self._player.stop_playback()
            self._current_asset = None
            self._write_current(asset=None, pipeline_state="READY")
            self._last_dispatch_key = ("stop", None, None, False, None)
            return

        if desired.mode == PlaybackMode.SPLASH:
            # Splash dispatch reads persist/splash on every call. The
            # contents could change without ``desired`` changing, so we
            # do NOT debounce SPLASH on the dispatch key -- but we DO
            # tear down a running slideshow first.
            self._stop_slideshow_if_running()
            self._dispatch_splash()
            self._last_dispatch_key = (
                "splash", None, self._current_asset, False, None,
            )
            return

        if not desired.asset:
            logger.info("dispatch: PLAY with no asset; ignoring")
            return

        # Slideshow takes precedence over single-asset dispatch -- the
        # ``asset`` field carries the slideshow name, not an asset
        # filename, so the regular videos/images lookup would otherwise
        # 404. Matches the Pi's ``_desired_kind`` classification
        # (agora/player/service.py:2594).
        if (getattr(desired, "asset_type", None) or "").lower() == "slideshow":
            self._dispatch_slideshow(desired)
            # Slideshow path manages its own idempotency via the
            # sequencer; clear the single-asset key so a later switch
            # back to image/video doesn't false-skip.
            self._last_dispatch_key = (
                "play", "slideshow", desired.asset, False,
                getattr(desired, "loop_count", None),
            )
            return

        # Single-asset path: tear down any in-flight slideshow first.
        self._stop_slideshow_if_running()

        path = _resolve_asset(desired.asset, self._assets_dir)
        if path is None:
            logger.info(
                "dispatch: asset %r not yet on disk; waiting for fetch",
                desired.asset,
            )
            return

        subdir = path.parent.name
        asset_type = "image" if subdir == "images" else (
            "video" if subdir == "videos" else subdir
        )
        # Stable-state idempotency: a re-publish with identical content
        # but a fresh ``timestamp`` would otherwise re-fire show_image
        # / show_video and cause visible flicker.
        new_key = (
            "play", asset_type, desired.asset,
            bool(desired.loop), desired.loop_count,
        )
        if new_key == self._last_dispatch_key:
            logger.debug(
                "dispatch: %s %s already playing -- skipping re-dispatch",
                asset_type, desired.asset,
            )
            return

        if subdir == "images":
            logger.info("dispatch: show_image %s", path)
            self._player.show_image(path)
            self._current_asset = desired.asset
            self._write_current(asset=desired.asset, pipeline_state="PLAYING")
            self._last_dispatch_key = new_key
        elif subdir == "videos":
            logger.info(
                "dispatch: show_video %s (loop=%s, loop_count=%s)",
                path, desired.loop, desired.loop_count,
            )
            self._player.show_video(
                path,
                loop=bool(desired.loop),
                muted=False,
                loop_count=desired.loop_count,
            )
            self._current_asset = desired.asset
            self._write_current(asset=desired.asset, pipeline_state="PLAYING")
            self._last_dispatch_key = new_key
        else:
            logger.info(
                "dispatch: unsupported subdir %r for %s", subdir, path
            )

    def _dispatch_slideshow(self, desired: Any) -> None:
        """Hand the slideshow ``desired`` state to the sequencer.

        On manifest-missing / parse-failure the sequencer returns False;
        we record the error in ``current.json`` and fall back to
        splash so the screen doesn't stay frozen on whatever was
        previously shown.
        """
        if self._slideshow is None:  # pragma: no cover -- guarded by attach_player
            logger.error("dispatch: slideshow requested but sequencer not attached")
            return
        name = desired.asset
        loop_count = getattr(desired, "loop_count", None)
        anchor = getattr(desired, "schedule_anchor_at", None)
        # Stable-state idempotency: a CMS re-publish gets a fresh
        # ``desired.timestamp``, so my file-hash debounce in
        # ``_poll_once`` misses it. Without this short-circuit, every
        # WPS sync would restart the slideshow at slide 1. Mirrors the
        # Pi's ``_slideshow_manifest_unchanged`` check
        # (service.py:2594-2700). ``matches_anchor`` is the
        # schedule-derived analogue: a schedule.start_time edit shifts
        # the anchor, which must trigger a restart so the cycle
        # re-aligns to the new wall-clock origin.
        if (
            self._slideshow.is_running()
            and self._slideshow.current_name() == name
            and self._slideshow.matches_loop_count(loop_count)
            and self._slideshow.matches_anchor(anchor)
            and self._slideshow.manifest_unchanged()
        ):
            logger.debug(
                "dispatch: slideshow %s already running with matching "
                "manifest + loop_count + anchor -- skipping restart",
                name,
            )
            return
        logger.info(
            "dispatch: slideshow %s (loop_count=%s anchor=%s)",
            name, loop_count, anchor.isoformat() if anchor else "<none>",
        )
        ok = self._slideshow.start(name, loop_count, anchor=anchor)
        if not ok:
            logger.info(
                "dispatch: slideshow %r manifest unavailable; falling back to splash",
                name,
            )
            self._current_asset = None
            self._write_current(
                asset=None,
                pipeline_state="ERROR",
                error=f"Slideshow not found: {name}",
            )
            self._dispatch_splash()
            return
        self._current_asset = name
        self._write_current(asset=name, pipeline_state="PLAYING")

    def _stop_slideshow_if_running(self) -> None:
        if self._slideshow is not None and self._slideshow.is_running():
            self._slideshow.stop()

    def _on_slideshow_done(self) -> None:
        """Callback fired by the sequencer when a slideshow ends naturally
        or aborts. Transitions to splash so the screen never sits on
        the last slide."""
        logger.info("slideshow done; falling back to splash")
        self._dispatch_splash()

    def _dispatch_splash(self) -> None:
        """Resolve and show the configured splash asset.

        Reads ``<data_dir>/persist/splash`` (a plain text file CMSClient
        writes via ``_persist_splash``) and looks the name up under
        ``assets/``. If neither is available, the dispatch is a no-op
        -- matches Pi behaviour (splash with nothing configured leaves
        the screen as-is until something is set).
        """
        name = _read_splash_config(self._persist_dir)
        if not name:
            logger.info("dispatch: SPLASH but no splash configured; waiting")
            return
        path = _resolve_asset(name, self._assets_dir)
        if path is None:
            logger.info(
                "dispatch: SPLASH asset %r not yet on disk; waiting", name
            )
            return
        logger.info("dispatch: show_splash %s", path)
        self._player.show_splash(path)
        self._current_asset = name
        # mode=splash + pipeline_state=NULL -> CMS dashboard shows the
        # "Splash" badge (per cms/templates/_macros.html lines 802-813).
        self._write_current(asset=name, pipeline_state="NULL", mode="splash")

    def _write_current(
        self,
        *,
        asset: str | None,
        pipeline_state: str,
        error: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Write CurrentState to data_dir/state/current.json (atomic).

        ``mode`` defaults to PLAY whenever an asset is in play (matches
        Pi). After STOP it becomes SPLASH (Pi convention -- there is no
        STOP equivalent on the dashboard). Splash dispatch passes
        ``mode="splash"`` explicitly.
        """
        try:
            from shared.models import CurrentState, PlaybackMode
            from shared.state import write_state
        except ImportError:
            logger.error("shims not installed; cannot write current.json")
            return
        if mode is None:
            mode_enum = PlaybackMode.PLAY if asset else PlaybackMode.SPLASH
        else:
            mode_enum = PlaybackMode(mode)
        state = CurrentState(
            mode=mode_enum,
            asset=asset,
            pipeline_state=pipeline_state,
            started_at=datetime.now(UTC) if pipeline_state == "PLAYING" else None,
            error=error,
        )
        try:
            write_state(self._current_path, state)
        except OSError as e:
            logger.warning("failed to write current.json: %s", e)
        else:
            logger.debug(
                "current.json updated: pipeline_state=%s asset=%s error=%s",
                pipeline_state, asset, error,
            )
