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

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Slide default dwell when the manifest doesn't specify one. Matches the
# Pi (``service.py:491,512``).
DEFAULT_SLIDE_DURATION_MS = 10_000

# Per-slide transition fallback when the manifest schema doesn't carry
# one. Matches the Pi (``service.py:466-467``).
DEFAULT_TRANSITION = "cut"
DEFAULT_TRANSITION_MS = 600

# Indirection point for tests: monkeypatch this with a fake Timer that
# captures ``(interval, fn, args)`` and exposes a synchronous ``fire()``.
Timer = threading.Timer


@dataclass
class _SlideshowState:
    name: str
    slides: list[dict]
    digest: str
    loop_count: Optional[int]
    index: int = 0
    loops_completed: int = 0
    misses_this_cycle: int = 0
    epoch: int = 0
    timer: Optional[threading.Timer] = field(default=None, repr=False)


def _resolve_asset(name: str, assets_dir: Path) -> Optional[Path]:
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
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        self._player = player
        self._assets_dir = Path(assets_dir)
        self._slideshows_dir = self._assets_dir / "slideshows"
        self._on_done = on_done
        self._lock = threading.Lock()
        self._state: Optional[_SlideshowState] = None
        # Bumped on every ``start`` and ``stop`` so any in-flight timer
        # callback can tell its slideshow is no longer current.
        self._epoch = 0

    # -- Public API --------------------------------------------------

    def start(self, name: str, loop_count: Optional[int]) -> bool:
        """Begin sequencing slides from ``<assets>/slideshows/<name>.json``.

        Returns ``True`` on success (state initialised, first slide
        dispatched or pending). Returns ``False`` when the manifest is
        missing or malformed -- caller is responsible for falling back
        to splash + writing a ``current.json`` error.
        """
        manifest = self._read_manifest(name)
        if manifest is None:
            return False
        slides, digest = manifest
        with self._lock:
            self._cancel_timer_locked()
            self._epoch += 1
            self._state = _SlideshowState(
                name=name,
                slides=slides,
                digest=digest,
                loop_count=loop_count,
                epoch=self._epoch,
            )
            epoch_started = self._epoch
        logger.info(
            "Slideshow start: name=%s slides=%d loop_count=%s epoch=%d digest=%s",
            name, len(slides), loop_count, epoch_started, digest[:8],
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

    def current_name(self) -> Optional[str]:
        with self._lock:
            return self._state.name if self._state else None

    def manifest_digest(self) -> Optional[str]:
        """SHA-256 hex of the manifest bytes captured at :meth:`start`.

        Exposed for PR-3 (stable-state idempotency) so dispatch can
        decide whether the running slideshow is still in sync with the
        on-disk manifest.
        """
        with self._lock:
            return self._state.digest if self._state else None

    # -- Internals ---------------------------------------------------

    def _read_manifest(self, name: str) -> Optional[tuple[list[dict], str]]:
        """Read + validate a slideshow manifest.

        Mirrors ``service.py:_read_slideshow_manifest``. Returns
        ``(slides_list, digest_hex)`` or ``None`` when the manifest is
        missing, malformed, or has an empty slides list.
        """
        path = self._slideshows_dir / f"{name}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            logger.error("Slideshow manifest missing: %s", path)
            return None
        except OSError as e:
            logger.error("Slideshow manifest unreadable: %s (%s)", path, e)
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error("Slideshow manifest malformed: %s (%s)", path, e)
            return None
        if not isinstance(data, dict):
            logger.error("Slideshow manifest not a JSON object: %s", path)
            return None
        slides = data.get("slides")
        if not isinstance(slides, list) or not slides:
            logger.error("Slideshow manifest has no slides: %s", path)
            return None
        digest = hashlib.sha256(raw).hexdigest()
        return slides, digest

    def _cancel_timer_locked(self) -> None:
        """Cancel any pending slide-advance timer. Caller holds lock."""
        if self._state and self._state.timer is not None:
            try:
                self._state.timer.cancel()
            except Exception:  # pragma: no cover -- defensive
                logger.exception("Timer cancel raised")
            self._state.timer = None

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
                    # PR-1 deferral: video slides handled in M3b-2.
                    # Count as a miss so an all-video slideshow aborts
                    # cleanly rather than spinning silently.
                    logger.warning(
                        "Slideshow %s: video slide %s deferred (M3b-2) -- skipping",
                        s.name, slide_name,
                    )
                    s.misses_this_cycle += 1
                    if s.misses_this_cycle >= len(s.slides):
                        logger.error(
                            "Slideshow %s: every slide in cycle unplayable -- abort",
                            s.name,
                        )
                        self._clear_locked()
                        fire_on_done = True
                        break
                    continue

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
                # Schedule slide expiry. We pass ``epoch`` so the
                # callback can detect a slideshow restart and bail.
                t = Timer(
                    duration_ms / 1000.0,
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
