"""Local shell server.

Mounts the real agora player shell (``agora/player/shell/``) at ``/`` so
the chromium window loads the same SPA a Pi would. Static assets live
at ``/assets`` (rooted at the configured ``assets_dir``, which is the
data_dir on softplayer). ``/ws`` is the protocol channel the SPA opens
on load -- here it's an accept-only stub; pushing real ``show_image``
/ ``show_video`` commands lands in M3 once we have a Player layer
bridging DesiredState into shell commands.

Auxiliary endpoints:
  GET /healthz  -> {"status": "ok"}  (basic up/down probe)
  GET /about    -> JSON with version, device_id, cms_url, available_slots

Run in a background thread; the main thread blocks on the browser process.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agora_softplayer import __version__

logger = logging.getLogger(__name__)


# Path to the real shell SPA when the agora submodule is checked out.
# __file__ lives at <repo>/src/agora_softplayer/shell_server.py, so the
# submodule is three parents up + /agora/player/shell.
_REAL_SHELL_DIR = Path(__file__).resolve().parents[2] / "agora" / "player" / "shell"


# Tiny landing page used when the agora submodule is NOT checked out
# (e.g. someone pulled the softplayer source without --recurse-submodules
# and is running it for the first time). Lets M1-style smoke testing
# still work without the submodule.
def _render_placeholder(data_dir: Path) -> str:
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
    """FastAPI app hosting the agora shell SPA + auxiliary endpoints."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        data_dir: Path,
        cms_url: str | None = None,
        available_slots: int = 1,
        shell_dir: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.cms_url = cms_url
        self.available_slots = available_slots
        self.shell_dir = Path(shell_dir) if shell_dir else _REAL_SHELL_DIR
        self._app = self._build_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _device_id(self) -> str | None:
        path = self.data_dir / "device_id"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _build_app(self) -> FastAPI:
        # Route order matters: /ws BEFORE the catch-all StaticFiles mount
        # at /, otherwise Starlette dispatches the WS upgrade into the
        # static-file handler. /assets also goes before the / mount.
        app = FastAPI(title="agora-softplayer shell")
        have_real_shell = self.shell_dir.is_dir() and (self.shell_dir / "index.html").exists()
        assets_dir = self.data_dir / "assets"

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
                    "shell_source": "agora-real" if have_real_shell else "placeholder",
                    "data_dir": str(self.data_dir),
                }
            )

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            logger.info("shell WS connected")
            try:
                while True:
                    msg_text = await websocket.receive_text()
                    try:
                        payload = json.loads(msg_text)
                    except json.JSONDecodeError:
                        logger.debug("shell sent bad json: %s", msg_text)
                        continue
                    logger.debug("shell event: %s", payload)
            except WebSocketDisconnect:
                logger.info("shell WS disconnected")
            except Exception:
                logger.exception("shell ws unexpected error")

        if not have_real_shell:
            # Fallback: serve the M1 placeholder so the smoke story still
            # works when someone clones without the submodule.
            @app.get("/", response_class=HTMLResponse)
            async def root_placeholder() -> str:
                return _render_placeholder(self.data_dir)

            return app

        @app.get("/")
        async def root() -> FileResponse:
            return FileResponse(self.shell_dir / "index.html")

        if assets_dir.is_dir():
            app.mount(
                "/assets", StaticFiles(directory=str(assets_dir)),
                name="assets",
            )

        app.mount(
            "/", StaticFiles(directory=str(self.shell_dir), html=True),
            name="shell",
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
        logger.debug("Shell server thread started")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        logger.debug("Shell server stopped")

