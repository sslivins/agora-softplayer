"""Background CMSClient runner.

Hosts the agora ``cms_client.service.CMSClient`` on its own daemon thread
with its own asyncio loop, so it doesn't compete with uvicorn (which has
its own thread+loop in ``shell_server``) or block the click main thread
that owns the browser subprocess.

Caller is expected to have already done:
    agora_softplayer.shims.configure(data_dir=..., available_slots=...)
    agora_softplayer.shims.apply_shims()
so that ``cms_client.service`` is importable and probes are Windows-friendly.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class CMSRunner:
    """Run a single CMSClient against the configured CMS in a daemon thread."""

    def __init__(
        self,
        *,
        cms_url: str,
        data_dir: Path,
        fleet_id: str,
        fleet_secret_hex: str,
        cms_transport: str = "direct",
        device_api_key: str = "",
    ) -> None:
        self.cms_url = cms_url
        self.data_dir = data_dir
        self.fleet_id = fleet_id
        self.fleet_secret_hex = fleet_secret_hex
        self.cms_transport = cms_transport
        self.device_api_key = device_api_key

        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._run_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(
            target=self._run, name="agora-softplayer-cms", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            logger.error("CMSRunner failed to bring up its asyncio loop within 5s")

    def stop(self) -> None:
        if not self._thread:
            return
        if self._loop and self._client and not self._loop.is_closed():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                logger.exception("CMSRunner shutdown raised")
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                # Loop closed between the is_closed() check and the call --
                # benign race, the thread is already on its way out.
                pass
        self._thread.join(timeout=5)
        self._thread = None

    async def _shutdown(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop()
        except Exception:
            logger.exception("CMSClient.stop() raised")
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    def _build_settings(self):
        # Defer import: requires shims to be installed first (caller's job).
        from api.config import Settings

        # The softplayer is bootstrap-v2-only by design; there is no legacy
        # register-then-mint-a-token fallback path on Windows. Settings.bootstrap_v2
        # together with fleet_id + fleet_secret_hex drives ensure_wps_credentials()
        # in cms_client.bootstrap_boot.
        return Settings(
            agora_base=self.data_dir,
            cms_url=self.cms_url,
            cms_transport=self.cms_transport,
            device_api_key=self.device_api_key,
            bootstrap_v2=True,
            fleet_id=self.fleet_id,
            fleet_secret_hex=self.fleet_secret_hex,
        )

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            from cms_client.service import CMSClient

            settings = self._build_settings()
            settings.ensure_dirs()
            self._client = CMSClient(settings)
            logger.info(
                "CMSRunner: connecting to %s (transport=%s, agora_base=%s)",
                settings.cms_url, settings.cms_transport, settings.agora_base,
            )
            self._run_task = loop.create_task(self._client.run())
            self._ready.set()
            loop.run_until_complete(self._run_task)
        except asyncio.CancelledError:
            logger.info("CMSRunner: cancelled")
        except Exception:
            logger.exception("CMSRunner crashed")
            self._ready.set()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._stopped.set()
            logger.info("CMSRunner: thread exited")
