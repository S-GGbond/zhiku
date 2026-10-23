#!/usr/bin/env python3
"""命令行入口：serve / ingest / search / ask / status。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _services():
    from app.db import Database
    from app.index import RetrievalIndex
    from app.ingest import Ingestor
    from app.runtime import Runtime

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "kb.sqlite3")
    runtime = Runtime(db, data_dir)
    index = RetrievalIndex(db, runtime)
    ingestor = Ingestor(db, runtime, index)
    return runtime, index, ingestor


def cmd_serve(args) -> int:
    import uvicorn

    from app.config import env_config

    cfg = env_config()
    host = args.host or cfg["host"]
    port = args.port or int(cfg["port"])
    print(f"→ http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload, log_level="info")
    return 0


def cmd_ingest(args) -> int:
    from app.parsers import SUPPORTED_EXTS

    runtime, index, ingestor = _services()
    if not runtime.get("embed_base_url"):
        print("错误：未配置向量模型，请先在 WebUI 设置或 .env 中填写 EMBED_BASE_URL / EMBED_API_KEY")
        return 2
    paths: list[Path] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            paths.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in SUPPORTED_EXTS))
        elif path.exists():
            paths.append(path)
        else:
            print(f"跳过不存在的路径：{path}")
    if not paths:
        print("没有可入库的文件")
        return 1
    for path in paths:
        result = ingestor.add_file(path, path.name, None)
        status = "已存在，跳过" if result["duplicate"] else "已加入队列"
        print(f"· {path.name} -> {status}")
    ingestor.wait_idle()
    db = ingestor.db
    for doc in db.list_documents():
        line = f"  [{doc['status']}] {doc['title']} · {doc['num_chunks']} 片段"
        if doc["error"]:
            line += f" · {doc['error']}"
        print(line)
    return 0


def cmd_search(args) -> int:
    _, index, _ = _services()
    result = index.search(args.query, final_k=args.top_k)
    if not result["hits"]:
        print("没有命中结果")
        return 0
    for i, hit in enumerate(result["hits"], start=1):
        loc = f" 第 {hit['page']} 页" if hit.get("page") else ""
        print(f"[{i}] {hit['doc_title']}{loc} · score={hit['score']:.4f}")
        print("    " + hit["text"][:240].replace("\n", " ") + ("…" if len(hit["text"]) > 240 else ""))
    for warning in result["meta"].get("warnings", []):
        print("! " + warning)
    return 0


def cmd_ask(args) -> int:
    from app import rag

    runtime, index, _ = _services()
    data = rag.answer(runtime, index, args.query, top_k=args.top_k)
    print(data["answer"])
    if data["sources"]:
        print("\n来源：")
        for src in data["sources"]:
            loc = f" 第 {src['page']} 页" if src.get("page") else ""
            print(f"  [{src['index']}] {src['doc_title']}{loc}")
    return 0


def cmd_status(args) -> int:
    runtime, index, _ = _services()
    db = runtime.db
    stats = db.stats()
    info = index.info()
    print(json.dumps({
        "文档": stats["documents"],
        "就绪": stats["documents_ready"],
        "片段": stats["chunks"],
        "已向量化": stats["chunks_embedded"],
        "向量维度": info["embed_dim"],
        "向量模型": runtime.get("embed_model"),
        "对话模型": runtime.get("llm_model"),
        "接口": runtime.get("embed_base_url"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_reset_password(args) -> int:
    import getpass

    from app.auth import MIN_PASSWORD, Auth

    runtime, _, _ = _services()
    auth = Auth(runtime.db)
    username = args.username or auth.username() or "admin"
    password = getpass.getpass(f"为 {username} 设置新密码：")
    if len(password) < MIN_PASSWORD:
        print(f"密码至少 {MIN_PASSWORD} 位")
        return 1
    if password != getpass.getpass("再输入一次："):
        print("两次输入不一致")
        return 1
    auth.set_password(username, password)
    print("密码已重置，其他设备上的登录会话已失效")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地知识库 RAG")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_ingest = sub.add_parser("ingest", help="批量导入文件或目录")
    p_ingest.add_argument("paths", nargs="+")
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser("search", help="只检索不生成答案")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=6)
    p_search.set_defaults(func=cmd_search)

    p_ask = sub.add_parser("ask", help="提问并生成带引用的答案")
    p_ask.add_argument("query")
    p_ask.add_argument("--top-k", type=int, default=6)
    p_ask.set_defaults(func=cmd_ask)

    p_status = sub.add_parser("status", help="查看知识库状态")
    p_status.set_defaults(func=cmd_status)

    p_pwd = sub.add_parser("reset-password", help="本地重置登录密码（忘记密码时用）")
    p_pwd.add_argument("--username", default=None, help="改用户名，默认沿用原账号")
    p_pwd.set_defaults(func=cmd_reset_password)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
