"""Minimal FastAPI shell server for milestone 1.

This is a placeholder so we can prove end-to-end that the browser opens
a window and renders a page served by our process. Milestone 2 swaps
this for the real shell SPA from ``agora/player/shell/``.

Run in a background thread; the main thread blocks on the browser process.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


def _render_placeholder(data_dir: Path) -> str:
    # Inline string concatenation avoids %-format / str.format collisions
    # with the CSS in this page (`100%`, `0.04em`, etc).
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <title>agora-softplayer</title>\n"
        "  <style>\n"
        "    html, body { margin: 0; height: 100%; background: #111; color: #eee;\n"
        "                 font-family: system-ui, sans-serif; }\n"
        "    .center { height: 100%; display: grid; place-items: center; text-align: center; }\n"
        "    h1 { font-weight: 300; letter-spacing: 0.04em; }\n"
        "    code { background: #222; padding: 0.15em 0.4em; border-radius: 4px; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class=\"center\">\n"
        "    <div>\n"
        "      <h1>agora-softplayer</h1>\n"
        "      <p>milestone 1: window is up. CMS wiring lands next.</p>\n"
        f"      <p><code>data_dir={data_dir}</code></p>\n"
        "    </div>\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )


class ShellServer:
    """Run a tiny FastAPI app on a background thread."""

    def __init__(self, *, host: str, port: int, data_dir: Path) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir

        app = FastAPI()

        @app.get("/", response_class=HTMLResponse)
        async def root() -> str:
            return _render_placeholder(data_dir)

        @app.get("/healthz")
        async def healthz() -> dict:
            return {"status": "ok"}

        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

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
        logger.debug("Shell server thread started")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        logger.debug("Shell server stopped")
