from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import paths
from app.db import SessionLocal, init_database
from app.ingestion.pipeline import recover_interrupted_imports


def create_app() -> FastAPI:
    paths.ensure()
    init_database()
    with SessionLocal() as session:
        recover_interrupted_imports(session)
    application = FastAPI(
        title="AI Agent 工业交换机自动配置",
        version="0.1.0",
        description="本地单用户的手册注入、型号库、拓扑与逐台配置工作台。",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)
    return application


app = create_app()
