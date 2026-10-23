"""MCP server：把本地知识库暴露给任意支持 MCP 的 Agent（stdio）。"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("RAG_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("RAG_API_TOKEN", "").strip()
USERNAME = os.getenv("RAG_USERNAME", "").strip()
PASSWORD = os.getenv("RAG_PASSWORD", "").strip()
_COOKIE: str | None = None

mcp = FastMCP("local-knowledge-base")

SERVICE_DOWN = ("错误：连接不上知识库服务（{url}）。请先启动服务：cd /home/chen/Project/zhiku && ./start.sh，"
                "或用环境变量 RAG_BASE_URL 指向实际地址。")


def safe(fn):
    """把连不上服务、接口报错等情况转成可读提示，而不是抛栈给 Agent。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except httpx.ConnectError:
            return SERVICE_DOWN.format(url=BASE_URL)
        except httpx.TimeoutException:
            return f"错误：请求知识库超时（{BASE_URL}），如果正在嵌入大量文档请稍后重试。"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return ("错误：知识库需要登录。请在 MCP 配置里设置 RAG_API_TOKEN（WebUI「设置」里生成的访问令牌），"
                        "或设置 RAG_USERNAME / RAG_PASSWORD，然后重启 MCP 服务。")
            text = exc.response.text[:300]
            return f"错误：知识库接口返回 {exc.response.status_code}：{text}"
        except Exception as exc:  # noqa: BLE001
            return f"错误：{exc}"
    return wrapper


def _login(client: httpx.Client) -> str | None:
    """没配访问令牌时，用用户名密码换一个登录 Cookie（进程内缓存）。"""
    global _COOKIE
    if _COOKIE:
        return _COOKIE
    if not (USERNAME and PASSWORD):
        return None
    resp = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    if resp.status_code >= 400:
        return None
    _COOKIE = resp.cookies.get("kb_session") or client.cookies.get("kb_session")
    return _COOKIE


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    client = httpx.Client(base_url=BASE_URL, headers=headers, timeout=180.0)
    if not TOKEN:
        cookie = _login(client)
        if cookie:
            client.cookies.set("kb_session", cookie)
    return client


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
@safe
def kb_status() -> str:
    """查看知识库状态：文档数、片段数、向量数，以及模型是否可用。"""
    with _client() as client:
        stats = client.get("/api/stats").json()
        cfg = client.get("/api/config").json()
    return _json({
        "documents": stats.get("documents"),
        "documents_ready": stats.get("documents_ready"),
        "chunks": stats.get("chunks"),
        "chunks_vectorized": stats.get("chunks_vectorized"),
        "embedding_model": cfg.get("embed_model"),
        "embedding_ready": cfg.get("embed_ready"),
        "chat_model": cfg.get("llm_model"),
        "chat_ready": cfg.get("llm_ready"),
        "busy": stats.get("busy"),
    })


@mcp.tool()
@safe
def search_knowledge_base(query: str, top_k: int = 6, doc_ids: list[int] | None = None) -> str:
    """在知识库中做语义+关键词混合检索，返回最相关的原文片段（不做总结）。

    Args:
        query: 检索问题或关键词。
        top_k: 返回片段数量，默认 6。
        doc_ids: 可选，只在指定文档 id 内检索。
    """
    with _client() as client:
        resp = client.post("/api/search", json={"query": query, "top_k": top_k, "doc_ids": doc_ids})
        resp.raise_for_status()
        data = resp.json()
    hits = [
        {
            "来源": f"《{h['doc_title']}》" + (f" 第 {h['page']} 页" if h.get("page") else ""),
            "标题": h.get("heading"),
            "相关度": h.get("score"),
            "内容": h["text"],
        }
        for h in data.get("hits", [])
    ]
    if not hits:
        return "知识库中没有检索到相关内容。"
    return _json(hits)


@mcp.tool()
@safe
def ask_knowledge_base(question: str, doc_ids: list[int] | None = None) -> str:
    """基于知识库内容回答问题，返回答案与引用来源。

    Args:
        question: 用户问题。
        doc_ids: 可选，只在指定文档 id 内检索。
    """
    with _client() as client:
        resp = client.post("/api/chat", json={
            "query": question, "doc_ids": doc_ids, "stream": False, "history": [],
        })
        resp.raise_for_status()
        data = resp.json()
    sources = [
        {"编号": s["index"], "来源": f"《{s['doc_title']}》" + (f" 第 {s['page']} 页" if s.get("page") else ""),
         "片段": s["preview"][:300]}
        for s in data.get("sources", [])
    ]
    return _json({"答案": data.get("answer"), "引用": sources})


@mcp.tool()
@safe
def list_documents() -> str:
    """列出知识库中的全部文档及其状态。"""
    with _client() as client:
        resp = client.get("/api/documents")
        resp.raise_for_status()
        docs = resp.json().get("documents", [])
    return _json([
        {"id": d["id"], "标题": d["title"], "类型": d.get("mime"), "状态": d["status"],
         "片段数": d.get("num_chunks"), "来源": d.get("source")}
        for d in docs
    ])


@mcp.tool()
@safe
def add_document(file_path: str) -> str:
    """上传本地文件到知识库（PDF/Word/Excel/PPT/Markdown/文本/代码）。

    Args:
        file_path: 本机文件的绝对路径。
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        return f"文件不存在：{path}"
    with _client() as client, path.open("rb") as fh:
        resp = client.post("/api/documents", files=[("files", (path.name, fh, "application/octet-stream"))])
        resp.raise_for_status()
        data = resp.json()
    return _json(data.get("results", data))


@mcp.tool()
@safe
def sync_document(file_path: str, title: str | None = None, tags: list[str] | None = None) -> str:
    """把本机文件同步进知识库：同一路径已存在就覆盖更新，内容没变就跳过。

    什么时候用：你刚写完或改完一份文档、规范、报告之后，让知识库跟文件保持一致。
    写入前建议先 search_knowledge_base 看一眼库里是否已有同主题文档——有的话改原文件再同步，
    不要新起一份。临时草稿、中间产物不要写。

    Args:
        file_path: 本机文件的绝对路径。
        title: 可选标题，默认取文件名。
        tags: 可选标签，例如 ["规范", "写作"]。
    """
    with _client() as client:
        resp = client.post("/api/documents/sync", json={
            "path": str(Path(file_path).expanduser()), "title": title, "tags": tags or [],
        })
        resp.raise_for_status()
        data = resp.json()
    doc = data.get("document") or {}
    return _json({
        "动作": {"created": "新建", "updated": "已覆盖更新", "unchanged": "内容未变，已跳过",
                 "duplicate": "库里已有同样内容", "skipped": "已存在且未覆盖"}.get(data.get("action"), data.get("action")),
        "文档": {"id": doc.get("id"), "标题": doc.get("title"), "状态": doc.get("status"),
                 "片段数": doc.get("num_chunks")},
    })


@mcp.tool()
@safe
def save_note(title: str, text: str, tags: list[str] | None = None) -> str:
    """把一条值得复用的结论 / 规则 / 模板直接存进知识库，不用先建文件。

    只在内容以后会被反复用到时才写：项目规则、约定、决策、踩坑结论、写作模板、某个问题的最终答案。
    临时草稿、一次性对话、代码或原文里已经写清楚的东西不要写。
    标题相同会覆盖旧内容，所以标题要稳定（例如「前端代码规范」而不是「2026-10-23 的规范」）。
    需要溯源时，在正文末尾加一行「生成信息：模型 …；依据《…》」。

    Args:
        title: 笔记标题，同时作为覆盖更新的主键。
        text: 正文，支持 Markdown。
        tags: 可选标签，例如 ["规则", "部署"]。
    """
    with _client() as client:
        resp = client.post("/api/notes", json={"title": title, "text": text, "tags": tags or []})
        resp.raise_for_status()
        data = resp.json()
    doc = data.get("document") or {}
    return _json({
        "动作": {"created": "新建", "updated": "已覆盖更新", "unchanged": "内容未变，已跳过",
                 "duplicate": "库里已有同样内容"}.get(data.get("action"), data.get("action")),
        "文档": {"id": doc.get("id"), "标题": doc.get("title"), "状态": doc.get("status")},
    })


@mcp.tool()
@safe
def add_web_page(url: str, title: str | None = None) -> str:
    """抓取网页正文并加入知识库。

    Args:
        url: 网页链接。
        title: 可选标题。
    """
    with _client() as client:
        resp = client.post("/api/documents/url", json={"url": url, "title": title})
        resp.raise_for_status()
        return _json(resp.json())


if __name__ == "__main__":
    mcp.run()
