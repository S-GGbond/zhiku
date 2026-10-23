"""HTTP API。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import backup as kbbackup
from . import rag
from .auth import COOKIE, SESSION_TTL, AuthError
from .parsers import SUPPORTED_EXTS
from .providers import ConfigError, ProviderError

router = APIRouter(prefix="/api")


# ---------- 依赖 ----------
class Ctx:
    def __init__(self, db, runtime, index, ingestor, auth=None):
        self.db = db
        self.runtime = runtime
        self.index = index
        self.ingestor = ingestor
        self.auth = auth


def ctx_of(request: Request) -> Ctx:
    return request.app.state.ctx


# ---------- 数据模型 ----------
class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    top_k: int | None = None
    doc_ids: list[int] | None = None


class ChatIn(BaseModel):
    query: str = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)
    doc_ids: list[int] | None = None
    top_k: int | None = None
    stream: bool = True
    mode: str = "answer"  # answer: 调对话模型生成答案；retrieve: 只返回检索片段


class UrlIn(BaseModel):
    url: str
    title: str | None = None


class SyncIn(BaseModel):
    path: str = Field(min_length=1)
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    replace: bool = True


class NoteIn(BaseModel):
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class ConfigIn(BaseModel):
    patch: dict[str, Any]


class TestIn(BaseModel):
    target: str = "embed"


class ReindexIn(BaseModel):
    scope: str = "embeddings"


class AccountIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


# ---------- 账号 ----------
def _set_session(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/")


@router.get("/auth/status")
def auth_status(request: Request):
    ctx = ctx_of(request)
    user = ctx.auth.check(request.cookies.get(COOKIE))
    return {"initialized": ctx.auth.initialized(), "authenticated": bool(user), "username": user or ""}


@router.post("/auth/setup")
def auth_setup(request: Request, body: AccountIn, response: Response):
    ctx = ctx_of(request)
    try:
        ctx.auth.create_account(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_session(response, ctx.auth.issue())
    return {"ok": True, "username": ctx.auth.username()}


@router.post("/auth/login")
def auth_login(request: Request, body: AccountIn, response: Response):
    ctx = ctx_of(request)
    if not ctx.auth.initialized():
        raise HTTPException(status_code=400, detail="还没有创建账号，请先完成初始化")
    if not ctx.auth.verify(body.username, body.password):
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    _set_session(response, ctx.auth.issue())
    return {"ok": True, "username": body.username}


@router.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password")
def auth_change_password(request: Request, body: PasswordIn, response: Response):
    ctx = ctx_of(request)
    if not ctx.auth.check(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        ctx.auth.change_password(body.old_password, body.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_session(response, ctx.auth.issue())
    return {"ok": True}


# ---------- 基础 ----------
@router.get("/stats")
def stats(request: Request):
    ctx = ctx_of(request)
    data = ctx.db.stats()
    data.update(ctx.index.info())
    data["busy"] = ctx.ingestor.busy()
    return data


@router.get("/config")
def get_config(request: Request):
    return ctx_of(request).runtime.public_config()


@router.put("/config")
def put_config(request: Request, body: ConfigIn):
    ctx = ctx_of(request)
    try:
        return ctx.runtime.save_config(body.patch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/config/test")
def test_config(request: Request, body: TestIn):
    ctx = ctx_of(request)
    try:
        return ctx.runtime.test_llm() if body.target == "llm" else ctx.runtime.test_embedding()
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------- 文档 ----------
@router.get("/documents")
def list_documents(request: Request):
    ctx = ctx_of(request)
    docs = ctx.db.list_documents()
    for doc in docs:
        doc.pop("sha256", None)
        if not doc.get("filename") and doc["kind"] == "file":
            doc["filename"] = Path(doc["source"]).name
    return {"documents": docs}


@router.post("/documents")
async def upload_documents(request: Request, files: list[UploadFile]):
    ctx = ctx_of(request)
    if not ctx.runtime.get("embed_base_url"):
        raise HTTPException(status_code=400, detail="请先在「设置」中配置向量模型接口")
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    max_bytes = int(ctx.runtime.get("max_upload_mb", 50)) * 1024 * 1024
    results = []
    for upload in files:
        name = os.path.basename(upload.filename or "unnamed")
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            results.append({"name": name, "ok": False, "error": f"不支持的文件类型 {ext or '（无扩展名）'}"})
            continue
        data = await upload.read()
        if len(data) > max_bytes:
            results.append({"name": name, "ok": False, "error": f"文件超过 {max_bytes // 1024 // 1024}MB 限制"})
            continue
        try:
            result = ctx.ingestor.add_bytes(data, name, upload.content_type)
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "ok": False, "error": str(exc)})
            continue
        doc = result["document"]
        results.append({
            "name": name,
            "ok": True,
            "duplicate": result["duplicate"],
            "document_id": doc["id"],
            "status": doc["status"],
        })
    return {"results": results}


@router.post("/documents/url")
def add_url(request: Request, body: UrlIn):
    ctx = ctx_of(request)
    if not ctx.runtime.get("embed_base_url"):
        raise HTTPException(status_code=400, detail="请先在「设置」中配置向量模型接口")
    try:
        return ctx.ingestor.add_url(body.url.strip(), body.title)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/sync")
def sync_document(request: Request, body: SyncIn):
    """按本机路径入库：Agent 写完/改完文件直接同步，同一路径覆盖旧版本。"""
    ctx = ctx_of(request)
    if not ctx.runtime.get("embed_base_url"):
        raise HTTPException(status_code=400, detail="请先在「设置」中配置向量模型接口")
    path = Path(body.path).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{path}")
    max_bytes = int(ctx.runtime.get("max_upload_mb", 50)) * 1024 * 1024
    if path.stat().st_size > max_bytes:
        raise HTTPException(status_code=400, detail=f"文件超过 {max_bytes // 1024 // 1024}MB 限制")
    try:
        return ctx.ingestor.sync_file(path, title=body.title, tags=",".join(body.tags),
                                      replace=body.replace)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/notes")
def add_note(request: Request, body: NoteIn):
    """把一段结论/规则直接存成笔记，标题相同即覆盖更新。"""
    ctx = ctx_of(request)
    if not ctx.runtime.get("embed_base_url"):
        raise HTTPException(status_code=400, detail="请先在「设置」中配置向量模型接口")
    try:
        return ctx.ingestor.add_note(body.title, body.text, tags=",".join(body.tags))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/documents/{doc_id}")
def get_document(request: Request, doc_id: int):
    ctx = ctx_of(request)
    doc = ctx.db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    doc.pop("sha256", None)
    return doc


@router.get("/documents/{doc_id}/chunks")
def get_chunks(request: Request, doc_id: int, limit: int = 50, offset: int = 0):
    ctx = ctx_of(request)
    if not ctx.db.get_document(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    chunks = ctx.db.chunks_of(doc_id)
    return {"total": len(chunks), "chunks": chunks[offset:offset + limit]}


@router.delete("/documents/{doc_id}")
def delete_document(request: Request, doc_id: int):
    ctx = ctx_of(request)
    doc = ctx.db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    ctx.db.delete_document(doc_id)
    if doc["kind"] == "file" and not str(doc["source"]).startswith(("db://", "note://")):
        path = Path(doc["source"])
        try:
            if path.resolve().is_relative_to(ctx.runtime.data_dir.resolve()):
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    ctx.index.invalidate()
    return {"ok": True}


@router.post("/documents/{doc_id}/reindex")
def reindex_document(request: Request, doc_id: int):
    ctx = ctx_of(request)
    try:
        return ctx.ingestor.reindex_document(doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reindex")
def reindex_all(request: Request, body: ReindexIn):
    ctx = ctx_of(request)
    if body.scope == "embeddings":
        return ctx.ingestor.reembed_all()
    raise HTTPException(status_code=400, detail="暂不支持该重建范围")


# ---------- 备份与迁移 ----------
@router.get("/backup/export")
def backup_export(request: Request):
    """导出整库备份（数据库 + 原始文件 + 笔记 + 账号与配置）。"""
    ctx = ctx_of(request)
    try:
        path = kbbackup.export_zip(ctx.db, ctx.runtime.data_dir, ctx.runtime)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.post("/backup/import")
async def backup_import(request: Request, file: UploadFile):
    """导入备份并整体覆盖当前知识库；导入后需要重新登录。"""
    ctx = ctx_of(request)
    if ctx.ingestor.busy():
        raise HTTPException(status_code=409, detail="正在入库文档，请等入库完成后再导入")
    workdir = Path(tempfile.mkdtemp(prefix="kb-upload-"))
    target = workdir / "backup.zip"
    try:
        target.write_bytes(await file.read())
        result = kbbackup.import_zip(target, ctx.db, ctx.runtime.data_dir, ctx.runtime, ctx.index)
        ctx.runtime.reload()
        result["orphans_removed"] = ctx.ingestor.cleanup_orphans()["removed"]
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"导入失败：{exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- 检索与问答 ----------
@router.post("/search")
def search(request: Request, body: SearchIn):
    ctx = ctx_of(request)
    try:
        return ctx.index.search(body.query, final_k=body.top_k, doc_ids=body.doc_ids)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/chat")
def chat(request: Request, body: ChatIn):
    ctx = ctx_of(request)
    cfg = ctx.runtime.cfg
    llm_ready = bool(cfg.get("llm_base_url") and cfg.get("llm_api_key") and cfg.get("llm_model"))
    use_retrieve = body.mode == "retrieve" or not llm_ready

    if use_retrieve and not body.stream:
        try:
            return rag.retrieve(ctx.runtime, ctx.index, body.query, body.doc_ids, body.top_k)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not body.stream:
        try:
            return rag.answer(ctx.runtime, ctx.index, body.query, body.history, body.doc_ids, body.top_k)
        except (ProviderError, ConfigError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def event_stream():
        try:
            if use_retrieve:
                events = rag.retrieve_stream(ctx.runtime, ctx.index, body.query, body.doc_ids, body.top_k)
            else:
                events = rag.answer_stream(ctx.runtime, ctx.index, body.query, body.history,
                                           body.doc_ids, body.top_k)
            for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            payload = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
