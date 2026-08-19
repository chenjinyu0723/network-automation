from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import paths
from app.db import SessionLocal, init_database
from app.ingestion.pipeline import recover_interrupted_imports


def _web_dist_path() -> Path | None:
    """Locate the Vite bundle in development and in a PyInstaller build."""

    configured = os.getenv("NETWORK_AUTOMATION_WEB_DIST")
    candidates = [Path(configured)] if configured else []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "web_dist")  # type: ignore[attr-defined]
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    return next((path for path in candidates if (path / "index.html").is_file()), None)


def create_app() -> FastAPI:
    paths.ensure()
    init_database()
    with SessionLocal() as session:
        recover_interrupted_imports(session)
    application = FastAPI(
        title="AI Agent 工业交换机自动配置",
        version="0.1.0",
        description="本地单用户的手册注入、拓扑与逐台配置工作台。",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)
    web_dist = _web_dist_path()
    if web_dist:
        application.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

        @application.get("/{client_path:path}", include_in_schema=False)
        def desktop_spa(client_path: str):  # type: ignore[no-untyped-def]
            if client_path.startswith("api/"):
                return FileResponse(web_dist / "index.html", status_code=404)
            candidate = web_dist / client_path
            return FileResponse(candidate if candidate.is_file() else web_dist / "index.html")
    return application


app = create_app()
