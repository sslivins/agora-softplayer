"""Slideshow state machine for the Windows softplayer.

Ports ``agora/player/service.py:_start_slideshow`` / ``_play_next_slide``
into a standalone class owned by :class:`WindowsPlayer`. Replaces the
Pi player's GLib mainloop integration with ``threading.Timer`` so the
sequencer can live inside the softplayer's threaded poll loop without
pulling in pygobject on Windows.

PR-1 scope (M3b-1): image slides only. Video slides are logged and
counted toward the per-cycle miss budget so an all-video manifest
still aborts cleanly rather than spinning. PR-2 (M3b-2) wires up
video dispatch + ``play_to_end`` via the shell ``ended`` event.

Concurrency:

* All state mutation goes through ``self._lock``.
* ``threading.Timer`` callbacks acquire the same lock at the top of
  :meth:`_advance` and bail immediately when their ``epoch`` argument
  no longer matches ``self._state.epoch`` -- this is the Pi's stale-
  timer defense and is what makes :meth:`stop` safe to call mid-flight.
* :meth:`_on_done` is invoked outside the lock so user callbacks
  cannot deadlock the sequencer.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Slide default dwell when the manifest doesn't specify one. Matches the
# Pi (``service.py:491,512``).
DEFAULT_SLIDE_DURATION_MS = 10_000

# Per-slide transition fallback when the manifest schema doesn't carry
# one. Matches the Pi (``service.py:466-467``).
DEFAULT_TRANSITION = "cut"
DEFAULT_TRANSITION_MS = 600

# Watchdog timing for video slides played to natural EOF. The shell
# ``ended`` event is the happy path; the watchdog only fires if the
# event never arrives (codec hang, shell crash). Mirrors the Pi
# (``service.py:_PLAY_TO_END_WATCHDOG_HARD_CAP_MS = 300_000`` with a
# 60s floor when the manifest hints a duration).
PLAY_TO_END_WATCHDOG_FLOOR_MS = 60_000
PLAY_TO_END_WATCHDOG_CAP_MS = 300_000

# Indirection point for tests: monkeypatch this with a fake Timer that
# captures ``(interval, fn, args)`` and exposes a synchronous ``fire()``.
Timer = threading.Timer


def _anchored_timer_ms(
    duration_ms: int, anchored_remaining_ms: int | None,
) -> int:
    """Pick the next slide-advance timer interval.

    When the slideshow has no wall-clock anchor we honour the manifest's
    per-slide ``duration_ms`` (legacy behaviour). When anchored, we arm
    the timer at ``min(remaining_ms, RESYNC_CAP_MS)`` so a drifted
    player re-evaluates the anchor at most every ``RESYNC_CAP_MS`` --
    mirrors the Pi (``service.py:_play_anchored_slide``). Anchored
    intervals are also floored at 1ms so a "we're already past the
    boundary" tick doesn't spin at 0.
    """
    if anchored_remaining_ms is None:
        return duration_ms
    # Lazy import: shims path is set up by apply_shims().
    from player.slideshow_engine import RESYNC_CAP_MS

    return max(1, min(int(anchored_remaining_ms), int(RESYNC_CAP_MS)))


@dataclass
class _SlideshowState:
    name: str
    slides: list[dict]
    digest: str
    loop_count: int | None
    index: int = 0
    loops_completed: int = 0
    misses_this_cycle: int = 0
    epoch: int = 0
    timer: threading.Timer | None = field(default=None, repr=False)
    # Set whenever a video slide is dispatched with play_to_end (loop=False
    # + asset_url available). The shell ``ended`` event with matching
    # ``asset_url`` advances; the watchdog timer is the fallback.
    pending_play_to_end: dict | None = field(default=None, repr=False)
    watchdog: threading.Timer | None = field(default=None, repr=False)
    # Wall-clock anchor (agora#226 Phase 2 / Phase 4 port). When set,
    # ``_advance`` jumps ``index`` to ``locate_slide_at(now - anchor)``
    # instead of incrementing -- so a softplayer restart mid-cycle
    # resumes at the right slide rather than starting from slide 0.
    schema_version: str = "1.0"
    anchor: datetime | None = None
    cycle_duration_ms: int = 0
    clock_skew_active: bool = False
    # Index of the slide most recently dispatched to the player.  Used
    # by the anchored path to suppress disruptive re-dispatch on resync
    # ticks when the wall clock hasn't crossed a slide boundary yet
    # (re-firing ``show_video`` mid-playback would visibly reload the
    # video). ``None`` until the first slide ships.
    last_dispatched_index: int | None = None


def _resolve_asset(name: str, assets_dir: Path) -> Path | None:
    """Look ``name`` up under ``assets_dir/{videos,images,splash}``.

    Matches :func:`agora_softplayer.windows_player._resolve_asset` so
    slide-name resolution is byte-identical to single-asset dispatch.
    """
    for subdir in ("videos", "images", "splash"):
        candidate = assets_dir / subdir / name
        if candidate.is_file():
            return candidate
    return None


class SlideshowSequencer:
    """Walks the slides of a manifest, dispatching to a ChromiumPlayer.

    Single instance per :class:`WindowsPlayer`. Reusable: ``start`` can
    be called repeatedly with different slideshow names; each call
    bumps the internal ``epoch`` so stale timer callbacks from the
    previous slideshow are inert.
    """

    def __init__(
        self,
        *,
        player: Any,
        assets_dir: Path,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._player = player
        self._assets_dir = Path(assets_dir)
        self._slideshows_dir = self._assets_dir / "slideshows"
        self._on_done = on_done
        self._lock = threading.Lock()
        self._state: _SlideshowState | None = None
        # Bumped on every ``start`` and ``stop`` so any in-flight timer
        # callback can tell its slideshow is no longer current.
        self._epoch = 0

    # -- Public API --------------------------------------------------

    def start(self, name: str, loop_count: int | None) -> bool:
        """Begin sequencing slides from ``<assets>/slideshows/<name>.json``.

        Returns ``True`` on success (state initialised, first slide
        dispatched or pending). Returns ``False`` when the manifest is
        missing or malformed -- caller is responsible for falling back
        to splash + writing a ``current.json`` error.
        """
        manifest = self._read_manifest(name)
        if manifest is None:
            return False
        data, digest = manifest
        slides = data["slides"]
        # Wall-clock anchor (agora#226 Phase 2 port). Only meaningful
        # when the manifest is schema 1.1+ AND carries a parseable
        # ``started_at``. Anything else leaves ``anchor`` as None and
        # ``_advance`` falls back to the legacy relative-timer path.
        # Lazy import so the shims path is set up first (see
        # ``_read_manifest`` for the same pattern).
        from player.slideshow_engine import (
            parse_iso8601_utc,
            parse_schema_version,
        )
        schema_version = str(data.get("manifest_schema_version") or "1.0")
        anchor_dt = parse_iso8601_utc(data.get("started_at") or "")
        if anchor_dt is not None and parse_schema_version(schema_version) < (1, 1):
            anchor_dt = None
        cycle_duration_ms = sum(
            max(int(s.get("duration_ms") or 0), 0) for s in slides
        )
        with self._lock:
            self._cancel_timer_locked()
            self._epoch += 1
            self._state = _SlideshowState(
                name=name,
                slides=slides,
                digest=digest,
                loop_count=loop_count,
                epoch=self._epoch,
                schema_version=schema_version,
                anchor=anchor_dt,
                cycle_duration_ms=cycle_duration_ms,
            )
            epoch_started = self._epoch
        logger.info(
            "Slideshow start: name=%s slides=%d loop_count=%s epoch=%d "
            "digest=%s anchor=%s",
            name, len(slides), loop_count, epoch_started, digest[:8],
            anchor_dt.isoformat() if anchor_dt else "<none>",
        )
        # Kick the first slide. ``_advance`` re-acquires the lock; we
        # deliberately released it above so any user callback fired
        # inside _advance (e.g. on_done for an empty-after-filtering
        # slideshow) cannot deadlock the sequencer.
        self._advance(epoch_started)
        return True

    def stop(self) -> None:
        """Tear down state. Pending timers become no-ops via epoch bump."""
        with self._lock:
            if self._state is None:
                return
            self._cancel_timer_locked()
            self._epoch += 1
            self._state = None
        logger.info("Slideshow stop")

    def is_running(self) -> bool:
        with self._lock:
            return self._state is not None

    def current_name(self) -> str | None:
        with self._lock:
            return self._state.name if self._state else None

    def matches_loop_count(self, loop_count: int | None) -> bool:
        """Return True iff the running slideshow was started with the
        same ``loop_count`` value. Used by dispatch idempotency so a
        CMS re-publish that changed nothing but the loop count still
        triggers a restart."""
        with self._lock:
            if self._state is None:
                return False
            return self._state.loop_count == loop_count

    def manifest_digest(self) -> str | None:
        """SHA-256 hex of the manifest bytes captured at :meth:`start`.

        Exposed for PR-3 (stable-state idempotency) so dispatch can
        decide whether the running slideshow is still in sync with the
        on-disk manifest.
        """
        with self._lock:
            return self._state.digest if self._state else None

    def manifest_unchanged(self) -> bool:
        """Return True iff the running slideshow's manifest on disk
        still matches the digest captured at :meth:`start`.

        Used by :class:`WindowsPlayer._dispatch` to debounce a CMS
        re-publish of the same slideshow (different ``desired.timestamp``
        but identical content). On any I/O / parse error returns
        ``False`` so the caller falls back to a normal restart.
        """
        with self._lock:
            if self._state is None:
                return False
            running_name = self._state.name
            running_digest = self._state.digest
        manifest = self._read_manifest(running_name)
        if manifest is None:
            return False
        _data, digest = manifest
        return digest == running_digest

    def on_shell_ended(self, asset_url: str | None) -> bool:
        """Handle a shell ``ended`` event.

        Returns ``True`` iff the event matched an armed play_to_end
        claim (in which case the slideshow has advanced and the
        WindowsPlayer should NOT also overwrite ``current.json`` with a
        READY pipeline_state). Mirrors the Pi's
        ``_dispatch_chromium_ended_to_slideshow`` (service.py:2488).
        """
        if not asset_url:
            return False
        advance_epoch: int | None = None
        with self._lock:
            if self._state is None:
                return False
            pending = self._state.pending_play_to_end
            if not pending:
                return False
            if pending.get("asset_url") != asset_url:
                logger.debug(
                    "Slideshow ended ignored: asset mismatch (event=%s armed=%s)",
                    asset_url, pending.get("asset_url"),
                )
                return False
            logger.info(
                "Slideshow %s: ended for slide %d (%s) -- advancing",
                self._state.name,
                pending["slide_index"],
                pending["slide_name"],
            )
            # Cancel watchdog + clear pending claim so _advance starts
            # cleanly.
            if self._state.watchdog is not None:
                try:
                    self._state.watchdog.cancel()
                except Exception:  # pragma: no cover
                    logger.exception("Watchdog cancel raised")
                self._state.watchdog = None
            self._state.pending_play_to_end = None
            advance_epoch = self._state.epoch
        if advance_epoch is not None:
            self._advance(advance_epoch)
        return True

    def _on_play_to_end_watchdog(self, epoch: int) -> None:
        """Watchdog callback when a video slide's ``ended`` never arrives.

        Mirrors the Pi's ``_on_play_to_end_chromium_watchdog``
        (service.py): logs, drops the pending claim, and advances.
        """
        advance = False
        with self._lock:
            if self._state is None or epoch != self._state.epoch:
                return
            self._state.watchdog = None
            pending = self._state.pending_play_to_end
            if not pending:
                return
            logger.warning(
                "Slideshow %s: play_to_end watchdog fired for slide %d (%s) "
                "-- advancing",
                self._state.name,
                pending["slide_index"],
                pending["slide_name"],
            )
            self._state.pending_play_to_end = None
            advance = True
            advance_epoch = self._state.epoch
        if advance:
            self._advance(advance_epoch)

    # -- Internals ---------------------------------------------------

    def _read_manifest(self, name: str) -> tuple[dict, str] | None:
        """Read + validate a slideshow manifest.

        Delegates to the shared
        :func:`player.slideshow_engine.read_slideshow_manifest` helper
        in the vendored ``agora`` submodule so the file-IO + parse +
        sha256-digest path stays byte-identical to the Pi player.
        Adds softplayer-specific error logging (the shared helper is
        silent so callers can surface failures however they want).

        Returns the full parsed manifest ``data`` dict (so callers can
        read ``slides``, ``manifest_schema_version``, ``started_at``,
        ...) plus the content digest.

        Imported lazily because ``agora_softplayer.shims.apply_shims``
        is what puts the agora submodule on ``sys.path``, and that
        happens after this module is first imported.
        """
        # Lazy import: sys.path is set up either by ``shims.apply_shims()``
        # in production or by ``tests/conftest.py`` under pytest.
        from player.slideshow_engine import read_slideshow_manifest

        path = self._slideshows_dir / f"{name}.json"
        result = read_slideshow_manifest(self._assets_dir, name)
        if result is None:
            # Match the previous log surface: ``_read_manifest`` used
            # to emit three distinct messages (missing / unreadable /
            # malformed / no-slides). The shared helper collapses them
            # all into a single ``None`` return, so we log one generic
            # error referring to the same path.
            logger.error(
                "Slideshow manifest %s missing, malformed, or has no slides",
                path,
            )
            return None
        data, digest = result
        return data, digest

    def _cancel_timer_locked(self) -> None:
        """Cancel any pending slide-advance + watchdog timers."""
        if self._state is None:
            return
        for attr in ("timer", "watchdog"):
            t = getattr(self._state, attr)
            if t is not None:
                try:
                    t.cancel()
                except Exception:  # pragma: no cover -- defensive
                    logger.exception("Timer cancel raised")
                setattr(self._state, attr, None)
        self._state.pending_play_to_end = None

    def _advance(self, epoch: int) -> None:
        """Advance to the next playable slide.

        Called both as the kickoff after :meth:`start` and as the
        :class:`threading.Timer` callback at slide expiry. Drives the
        whole state machine: cycle wraparound, ``loop_count``
        termination, miss-counting + all-missing abort, dispatch.

        Returns nothing; uses ``self._on_done`` to signal completion.
        Any user callback is fired *after* the lock is released so the
        callback can re-enter (e.g. start a new slideshow) without
        deadlocking.
        """
        fire_on_done = False
        with self._lock:
            if self._state is None or epoch != self._state.epoch:
                return
            # Timer has fired -- it owns no further callbacks.
            self._state.timer = None

            # Wall-clock anchored resolution (Phase 2 / Phase 4 port).
            # Jump ``index`` to the slide that should be on screen at
            # ``now`` instead of walking forward one tick at a time --
            # so a softplayer restart mid-cycle resumes at the right
            # slide. Falls through to the legacy index-increment path
            # for pre-1.1 manifests, clock-skew, or degenerate cycles.
            # The override is used to arm the next timer at
            # ``min(remaining_ms, RESYNC_CAP_MS)`` so a drifted player
            # re-syncs without needing a separate watchdog tick.
            anchored_remaining_ms: int | None = None
            s = self._state
            if s.anchor is not None and s.cycle_duration_ms > 0:
                from player.slideshow_engine import (
                    AnchorStatus,
                    resolve_anchored_target,
                )
                resolution = resolve_anchored_target(
                    slides=s.slides,
                    cycle_duration_ms=s.cycle_duration_ms,
                    anchor=s.anchor,
                )
                if (
                    resolution.status is AnchorStatus.OK
                    and resolution.target is not None
                ):
                    target_idx, remaining_ms = resolution.target
                    if s.clock_skew_active:
                        logger.info(
                            "Slideshow %s: clock-skew guard CLEARED -- "
                            "switching to anchored playback",
                            s.name,
                        )
                        s.clock_skew_active = False
                    # Suppress disruptive re-dispatch on resync ticks:
                    # if the wall clock still says we should be on the
                    # already-displayed slide, just re-arm the timer
                    # and bail. Re-firing ``show_video`` mid-playback
                    # would reload the <video> element from scratch.
                    # ``pending_play_to_end`` is set while we're
                    # waiting on the shell's natural EOF for a
                    # play_to_end video -- in that case there's no
                    # ``timer`` to re-arm; we just leave it running.
                    if (
                        s.last_dispatched_index == target_idx
                        and s.pending_play_to_end is None
                    ):
                        timer_ms = _anchored_timer_ms(0, remaining_ms)
                        t = Timer(
                            timer_ms / 1000.0,
                            self._advance,
                            args=(s.epoch,),
                        )
                        t.daemon = True
                        s.timer = t
                        t.start()
                        return
                    s.index = target_idx
                    anchored_remaining_ms = remaining_ms
                elif (
                    resolution.status is AnchorStatus.CLOCK_SKEW_BEHIND
                    and not s.clock_skew_active
                ):
                    logger.info(
                        "Slideshow %s: clock-skew guard ACTIVE "
                        "(skew_s=%.0f) -- using legacy timer chain",
                        s.name, resolution.skew_s,
                    )
                    s.clock_skew_active = True

            # Walk forward through (possibly missing/unsupported)
            # slides until we either dispatch one or decide the
            # slideshow is done.
            while True:
                s = self._state
                if s.index >= len(s.slides):
                    s.loops_completed += 1
                    target = s.loop_count
                    if target is not None and s.loops_completed >= target:
                        logger.info(
                            "Slideshow %s: completed %d/%d loops -> done",
                            s.name, s.loops_completed, target,
                        )
                        self._clear_locked()
                        fire_on_done = True
                        break
                    s.index = 0
                    s.misses_this_cycle = 0

                slide = s.slides[s.index]
                s.index += 1
                slide_name = slide.get("name") or ""
                path = _resolve_asset(slide_name, self._assets_dir)
                asset_type = (slide.get("asset_type") or "").lower()

                if path is None:
                    logger.error(
                        "Slideshow %s: slide %d (%s) missing on disk -- skipping",
                        s.name, s.index - 1, slide_name,
                    )
                    s.misses_this_cycle += 1
                    if s.misses_this_cycle >= len(s.slides):
                        logger.error(
                            "Slideshow %s: all %d slides unplayable -- abort",
                            s.name, len(s.slides),
                        )
                        self._clear_locked()
                        fire_on_done = True
                        break
                    continue

                if asset_type == "video":
                    s.misses_this_cycle = 0
                    transition = slide.get("transition") or DEFAULT_TRANSITION
                    transition_ms = int(
                        slide.get("transition_ms") or DEFAULT_TRANSITION_MS
                    )
                    duration_ms = int(slide.get("duration_ms") or 0)
                    play_to_end = bool(slide.get("play_to_end"))
                    # Wall-clock anchored seek: if we know where we
                    # should be inside this slide's slot, tell the
                    # shell so it can seek the <video> on
                    # loadedmetadata. Only meaningful when anchored
                    # AND the manifest gave the slide a duration.
                    start_offset_ms = 0
                    if (
                        anchored_remaining_ms is not None
                        and duration_ms > 0
                    ):
                        start_offset_ms = max(
                            0, duration_ms - anchored_remaining_ms,
                        )
                    # Resolve the asset URL the shell will echo back in
                    # ``ended``. ``asset_url`` may not exist on test
                    # doubles -- treat that as the "no URL" fallback.
                    asset_url = None
                    try:
                        asset_url = self._player.asset_url(path)
                    except Exception:
                        logger.debug(
                            "Slideshow %s: asset_url(%s) raised; using "
                            "timer-driven fallback", s.name, path,
                            exc_info=True,
                        )

                    if play_to_end and asset_url:
                        # Happy path: shell signals natural EOF.
                        logger.info(
                            "Slideshow %s: slide %d/%d video=%s play_to_end "
                            "asset_url=%s start_offset_ms=%d",
                            s.name, s.index, len(s.slides),
                            slide_name, asset_url, start_offset_ms,
                        )
                        try:
                            self._player.show_video(
                                path, loop=False, muted=False,
                                transition=transition,
                                duration_ms=transition_ms,
                                start_offset_ms=start_offset_ms,
                            )
                        except TypeError:
                            # Older ChromiumPlayer without
                            # start_offset_ms support: fall back to
                            # play-from-zero. The next anchored tick
                            # will still re-target if needed.
                            self._player.show_video(
                                path, loop=False, muted=False,
                                transition=transition,
                                duration_ms=transition_ms,
                            )
                        except Exception:
                            logger.exception(
                                "Slideshow %s: show_video dispatch raised",
                                s.name,
                            )
                        s.last_dispatched_index = s.index - 1
                        # Watchdog: 2× hinted duration with a 60s floor,
                        # capped at the hard cap. Matches Pi
                        # ``_play_slide_to_end_chromium`` (service.py:651).
                        if duration_ms > 0:
                            watchdog_ms = max(
                                duration_ms * 2,
                                PLAY_TO_END_WATCHDOG_FLOOR_MS,
                            )
                        else:
                            watchdog_ms = PLAY_TO_END_WATCHDOG_CAP_MS
                        watchdog_ms = min(
                            watchdog_ms, PLAY_TO_END_WATCHDOG_CAP_MS,
                        )
                        s.pending_play_to_end = {
                            "slide_index": s.index - 1,
                            "slide_name": slide_name,
                            "asset_url": asset_url,
                            "epoch": s.epoch,
                        }
                        wd = Timer(
                            watchdog_ms / 1000.0,
                            self._on_play_to_end_watchdog,
                            args=(s.epoch,),
                        )
                        wd.daemon = True
                        s.watchdog = wd
                        wd.start()
                        break

                    # Fallback: either ``play_to_end`` is unset OR we
                    # couldn't compute an asset_url. Loop the video in
                    # place and advance on the duration timer, exactly
                    # like the image branch. The Pi takes the same
                    # path (service.py:629).
                    if duration_ms <= 0:
                        duration_ms = DEFAULT_SLIDE_DURATION_MS
                    if play_to_end and not asset_url:
                        logger.warning(
                            "Slideshow %s: video slide %s requested "
                            "play_to_end but asset_url unavailable -- "
                            "falling back to timer-driven advance",
                            s.name, slide_name,
                        )
                    else:
                        logger.info(
                            "Slideshow %s: slide %d/%d video=%s "
                            "duration=%dms (loop)",
                            s.name, s.index, len(s.slides),
                            slide_name, duration_ms,
                        )
                    try:
                        self._player.show_video(
                            path, loop=True, muted=False,
                            transition=transition,
                            duration_ms=transition_ms,
                            start_offset_ms=start_offset_ms,
                        )
                    except TypeError:
                        self._player.show_video(
                            path, loop=True, muted=False,
                            transition=transition,
                            duration_ms=transition_ms,
                        )
                    except Exception:
                        logger.exception(
                            "Slideshow %s: show_video dispatch raised",
                            s.name,
                        )
                    s.last_dispatched_index = s.index - 1
                    timer_ms = _anchored_timer_ms(
                        duration_ms, anchored_remaining_ms,
                    )
                    t = Timer(
                        timer_ms / 1000.0,
                        self._advance,
                        args=(s.epoch,),
                    )
                    t.daemon = True
                    s.timer = t
                    t.start()
                    break

                # Found an image slide -- dispatch it.
                s.misses_this_cycle = 0
                transition = slide.get("transition") or DEFAULT_TRANSITION
                transition_ms = int(slide.get("transition_ms") or DEFAULT_TRANSITION_MS)
                duration_ms = int(slide.get("duration_ms") or 0)
                if duration_ms <= 0:
                    duration_ms = DEFAULT_SLIDE_DURATION_MS

                logger.info(
                    "Slideshow %s: slide %d/%d image=%s duration=%dms",
                    s.name, s.index, len(s.slides), slide_name, duration_ms,
                )
                try:
                    self._player.show_image(
                        path,
                        transition=transition,
                        duration_ms=transition_ms,
                    )
                except Exception:
                    logger.exception(
                        "Slideshow %s: show_image dispatch raised", s.name,
                    )
                s.last_dispatched_index = s.index - 1
                # Schedule slide expiry. We pass ``epoch`` so the
                # callback can detect a slideshow restart and bail.
                timer_ms = _anchored_timer_ms(
                    duration_ms, anchored_remaining_ms,
                )
                t = Timer(
                    timer_ms / 1000.0,
                    self._advance,
                    args=(s.epoch,),
                )
                t.daemon = True
                s.timer = t
                t.start()
                break

        if fire_on_done and self._on_done is not None:
            try:
                self._on_done()
            except Exception:
                logger.exception("Slideshow on_done callback raised")

    def _clear_locked(self) -> None:
        """Drop state + bump epoch. Caller holds lock."""
        self._cancel_timer_locked()
        self._epoch += 1
        self._state = None
