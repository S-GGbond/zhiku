"""应用入口：装配服务、静态前端与启动逻辑。"""

from __future__ import annotations

import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import Ctx, router
from .auth import COOKIE, Auth
from .config import ROOT
from .db import Database
from .index import RetrievalIndex
from .ingest import Ingestor
from .runtime import Runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rag")

STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = Path(data_dir or DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(data_dir / "kb.sqlite3")
        runtime = Runtime(db, data_dir)
        index = RetrievalIndex(db, runtime)
        ingestor = Ingestor(db, runtime, index)
        app.state.ctx = Ctx(db, runtime, index, ingestor, Auth(db))
        threading.Thread(target=_warm, args=(index,), daemon=True).start()
        migrated = ingestor.migrate_files_to_db()
        if migrated["moved"]:
            log.info("原始文件已并入数据库：%s", migrated["moved"])
        orphans = ingestor.cleanup_orphans()
        if orphans["removed"]:
            log.info("清理无引用文件：%s", orphans["removed"])
        resumed = ingestor.resume_unfinished()
        if resumed["resumed"] or resumed["failed"]:
            log.info("恢复未完成的入库：%s，失败 %s", resumed["resumed"], resumed["failed"])
        log.info("知识库已就绪：%s", data_dir)
        yield
        ingestor.pool.shutdown(wait=False)

    app = FastAPI(title="本地知识库 RAG", version="0.1.0", lifespan=lifespan, docs_url="/api/docs")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """API 需要登录会话，或在设置里配置了访问令牌时用 Bearer Token（给 Agent/MCP 用）。"""
        ctx = getattr(request.app.state, "ctx", None)
        path = request.url.path
        if not ctx or not path.startswith("/api") or path in {"/api/docs", "/api/openapi.json"} \
                or path.startswith("/api/auth/"):
            return await call_next(request)
        if not ctx.auth.initialized():
            return await call_next(request)  # 首次使用，先让用户完成账号初始化
        configured = ctx.runtime.get("api_token") or ""
        header = request.headers.get("authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if configured and provided and hmac.compare_digest(provided, configured):
            return await call_next(request)
        if ctx.auth.check(request.cookies.get(COOKIE)):
            return await call_next(request)
        return JSONResponse({"detail": "请先登录，或在请求头带访问令牌"}, status_code=401)

    app.include_router(router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/login")
        def login_page():
            return FileResponse(STATIC_DIR / "login.html")

        @app.get("/")
        def index_page(request: Request):
            ctx = request.app.state.ctx
            if not ctx.auth.check(request.cookies.get(COOKIE)):
                return RedirectResponse("/login", status_code=307)
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _warm(index: RetrievalIndex) -> None:
    try:
        index.ensure_loaded()
        log.info("索引预热完成：%s 个片段", len(index.entries))
    except Exception as exc:  # noqa: BLE001
        log.warning("索引预热失败：%s", exc)


app = create_app()
