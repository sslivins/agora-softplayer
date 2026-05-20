"""Auxiliary FastAPI for ``/healthz`` and ``/about``.

ChromiumPlayer (vendored from ``agora/player/chromium_backend.py``) owns
``127.0.0.1:8780`` with its WS + SPA + ``/assets`` mount. To avoid
patching softplayer-specific endpoints onto the vendored class, the
softplayer's auxiliary endpoints live on a sibling FastAPI on a
different port.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agora_softplayer import __version__

logger = logging.getLogger(__name__)


class AuxServer:
    """Small FastAPI exposing ``/healthz`` and ``/about``."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        data_dir: Path,
        cms_url: Optional[str] = None,
        available_slots: int = 1,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = Path(data_dir)
        self.cms_url = cms_url
        self.available_slots = available_slots
        self._app = self._build_app()
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def _device_id(self) -> Optional[str]:
        path = self.data_dir / "device_id"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="agora-softplayer aux")

        @app.get("/healthz")
        async def healthz() -> dict:
            return {"status": "ok"}

        @app.get("/about")
        async def about() -> JSONResponse:
            return JSONResponse(
                {
                    "version": __version__,
                    "device_id": self._device_id(),
                    "cms_url": self.cms_url,
                    "available_slots": self.available_slots,
                    "data_dir": str(self.data_dir),
                }
            )

        return app

    def start(self) -> None:
        if self._thread:
            return
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        logger.debug("AuxServer listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
