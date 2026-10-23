"""入库流水线：解析 -> 分块 -> 向量化 -> 落库（后台线程执行）。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .chunker import chunk_blocks
from .parsers import ParseError, fetch_url, parse_file

log = logging.getLogger("rag.ingest")


class Ingestor:
    def __init__(self, db, runtime, index):
        self.db = db
        self.runtime = runtime
        self.index = index
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest")
        self._active: set[int] = set()
        self._lock = threading.Lock()

    # ---------- 对外接口 ----------
    def add_file(self, path: Path, original_name: str, mime: str | None) -> dict:
        """从本机路径导入：内容进库，路径只作为来源标识。"""
        path = Path(path)
        result = self._upsert(
            source=str(path), title=original_name.rsplit(".", 1)[0], data=path.read_bytes(),
            name=original_name, mime=mime,
        )
        return {"duplicate": result["action"] == "duplicate", "document": result["document"]}

    def add_bytes(self, data: bytes, original_name: str, mime: str | None = None) -> dict:
        """浏览器上传：直接把字节写进库，不再落 data/uploads。"""
        sha = hashlib.sha256(data).hexdigest()
        return self._upsert(
            source=f"db://{sha[:12]}/{original_name}",
            title=original_name.rsplit(".", 1)[0],
            data=data, name=original_name, mime=mime,
        )

    def sync_file(self, path: Path, title: str | None = None, tags: str = "",
                  replace: bool = True) -> dict:
        """按来源路径入库：已存在就覆盖更新，内容没变就跳过，别处已有的同一内容算重复。

        Agent 改完文档再同步时走这里，避免同一份文档在库里堆出多个版本。
        """
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        existing = self.db.find_by_source(str(path))
        if existing:
            doc_id = int(existing["id"])
            old_tags = existing["tags"] or ""
            new_title = title or existing["title"]
            if existing["sha256"] == sha and existing["status"] in {"pending", "parsing", "embedding", "ready"}:
                if (tags and tags != old_tags) or new_title != existing["title"]:
                    self.db.update_document(doc_id, tags=tags or old_tags, title=new_title)
                return {"action": "unchanged", "document": self.db.get_document(doc_id)}
            if not replace:
                return {"action": "skipped", "document": dict(existing)}
            other = self.db.find_by_sha(sha)
            if other and int(other["id"]) != doc_id:
                self.db.delete_document(int(other["id"]))
            self.db.update_document(
                doc_id, sha256=sha, size=len(data), status="pending", error=None, progress=0.0,
                tags=tags or old_tags, title=new_title,
            )
            self.db.put_blob(doc_id, data, path.name, existing["mime"])
            self._submit(doc_id, lambda: self._run_blob(doc_id, path.name))
            return {"action": "updated", "document": self.db.get_document(doc_id)}
        duplicate = self.db.find_by_sha(sha)
        if duplicate:
            return {"action": "duplicate", "document": dict(duplicate)}
        doc_id = self.db.create_document(
            title=title or path.stem,
            source=str(path),
            kind="file",
            mime=None,
            size=len(data),
            sha256=sha,
            filename=path.name,
            tags=tags,
        )
        self.db.put_blob(doc_id, data, path.name, None)
        self._submit(doc_id, lambda: self._run_blob(doc_id, path.name))
        return {"action": "created", "document": self.db.get_document(doc_id)}

    def add_note(self, title: str, text: str, tags: str = "") -> dict:
        """把一段结论/规则/模板直接存成笔记（正文进库，不再落 notes/*.md）。"""
        title = (title or "").strip()
        body = (text or "").strip()
        if not title or not body:
            raise ParseError("标题和内容都不能为空")
        slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", title).strip("-")[:60] or "note"
        payload = f"# {title}\n\n{body}\n".encode("utf-8")
        return self._upsert(
            source=f"note://{slug}", title=title, data=payload,
            name=f"{slug}.md", mime="text/markdown", tags=tags, kind="note",
        )

    # ---------- 统一写入 ----------
    def _upsert(self, *, source: str, title: str, data: bytes, name: str,
                mime: str | None = None, tags: str = "", replace: bool = True,
                kind: str = "file") -> dict:
        """同一条来源覆盖更新；内容没变跳过；别处已有同样内容算重复。"""
        sha = hashlib.sha256(data).hexdigest()
        existing = self.db.find_by_source(source)
        if existing:
            doc_id = int(existing["id"])
            old_tags = existing["tags"] or ""
            if existing["sha256"] == sha and existing["status"] in {"pending", "parsing", "embedding", "ready"}:
                if (tags and tags != old_tags) or title != existing["title"]:
                    self.db.update_document(doc_id, tags=tags or old_tags, title=title)
                return {"action": "unchanged", "document": self.db.get_document(doc_id)}
            if not replace:
                return {"action": "skipped", "document": dict(existing)}
            other = self.db.find_by_sha(sha)
            if other and int(other["id"]) != doc_id:
                self.db.delete_document(int(other["id"]))
            self.db.update_document(
                doc_id, sha256=sha, size=len(data), status="pending", error=None, progress=0.0,
                tags=tags or old_tags, title=title, filename=name,
                mime=mime or existing["mime"],
            )
            self.db.put_blob(doc_id, data, name, mime)
            self._submit(doc_id, lambda: self._run_blob(doc_id, name))
            return {"action": "updated", "document": self.db.get_document(doc_id)}
        duplicate = self.db.find_by_sha(sha)
        if duplicate:
            return {"action": "duplicate", "document": dict(duplicate)}
        doc_id = self.db.create_document(
            title=title, source=source, kind=kind, mime=mime,
            size=len(data), sha256=sha, filename=name, tags=tags,
        )
        self.db.put_blob(doc_id, data, name, mime)
        self._submit(doc_id, lambda: self._run_blob(doc_id, name))
        return {"action": "created", "document": self.db.get_document(doc_id)}

    def _run_blob(self, doc_id: int, name: str) -> None:
        """从库里的原始内容解析（落一个临时文件，解析库不需改造）。"""
        blob = self.db.get_blob(doc_id)
        if not blob:
            doc = self.db.get_document(doc_id) or {}
            legacy = Path(doc.get("source") or "")
            if legacy.is_file():
                self._run_file(doc_id, legacy)
                return
            raise ParseError("原始内容已丢失，请重新上传")
        data, stored_name, _ = blob
        suffix = Path(name or stored_name or "file").suffix
        fd, tmp = tempfile.mkstemp(prefix="kb-parse-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            self.db.update_document(doc_id, status="parsing", error=None, progress=0.02)
            self._store(doc_id, parse_file(Path(tmp)))
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ---------- 存量数据维护 ----------
    def migrate_files_to_db(self) -> dict:
        """把历史版本留在 data/uploads、data/notes 的文件搬进库，搬完删掉磁盘副本。"""
        moved, skipped = [], []
        for doc in self.db.docs_without_blob():
            source = doc.get("source") or ""
            if not source or doc.get("kind") == "url":
                continue
            path = Path(source)
            if not path.is_file():
                skipped.append(int(doc["id"]))
                continue
            try:
                self.db.put_blob(int(doc["id"]), path.read_bytes(), path.name, doc.get("mime"))
            except OSError as exc:
                log.warning("迁移原始文件失败 doc=%s: %s", doc["id"], exc)
                skipped.append(int(doc["id"]))
                continue
            moved.append(int(doc["id"]))
            if self._in_data_dir(path):
                path.unlink(missing_ok=True)
        return {"moved": moved, "skipped": skipped}

    def _in_data_dir(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.runtime.data_dir.resolve())
        except (OSError, ValueError):
            return False

    def cleanup_orphans(self) -> dict:
        """删掉 data/uploads、data/notes 里已经没有文档引用的文件。"""
        referenced = {
            str(Path(doc["source"]).resolve())
            for doc in self.db.list_documents()
            if doc.get("source") and not str(doc["source"]).startswith(("db://", "note://", "http"))
        }
        removed = []
        for folder in ("uploads", "notes"):
            base = self.runtime.data_dir / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if path.is_file() and str(path.resolve()) not in referenced:
                    path.unlink(missing_ok=True)
                    removed.append(path.name)
        return {"removed": removed}

    def add_url(self, url: str, title: str | None = None) -> dict:
        doc_id = self.db.create_document(
            title=title or url,
            source=url,
            kind="url",
            mime="text/html",
            size=0,
            sha256=None,
        )
        self._submit(doc_id, lambda: self._run_url(doc_id, url))
        return {"duplicate": False, "document": self.db.get_document(doc_id)}

    def reindex_document(self, doc_id: int) -> dict:
        doc = self.db.get_document(doc_id)
        if not doc:
            raise KeyError(doc_id)
        source = doc["source"]
        if self.db.has_blob(doc_id):
            name = doc.get("filename") or Path(source).name or "file"
            self._submit(doc_id, lambda: self._run_blob(doc_id, name))
        elif doc["kind"] == "url":
            self._submit(doc_id, lambda: self._run_url(doc_id, source))
        elif Path(source).exists():
            self._submit(doc_id, lambda: self._run_file(doc_id, Path(source)))
        else:
            raise FileNotFoundError("原始内容已不存在，请重新上传")
        return self.db.get_document(doc_id)

    def reembed_all(self) -> dict:
        docs = self.db.list_documents()
        targets = [d["id"] for d in docs if d["num_chunks"]]
        for doc_id in targets:
            self._submit(doc_id, lambda d=doc_id: self._run_reembed(d))
        return {"queued": len(targets)}

    # ---------- 内部执行 ----------
    def _submit(self, doc_id: int, fn) -> None:
        with self._lock:
            self._active.add(doc_id)
        self.pool.submit(self._guard, doc_id, fn)

    def _guard(self, doc_id: int, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest failed doc=%s: %s", doc_id, exc)
            message = str(exc) if isinstance(exc, (ParseError, FileNotFoundError)) else f"{exc}"
            self.db.update_document(doc_id, status="error", error=message[:500], progress=0)
            log.debug(traceback.format_exc())
        finally:
            with self._lock:
                self._active.discard(doc_id)
            self.index.invalidate()

    def _run_file(self, doc_id: int, path: Path) -> None:
        self.db.update_document(doc_id, status="parsing", error=None, progress=0.02)
        parsed = parse_file(path)
        self._store(doc_id, parsed)

    def _run_url(self, doc_id: int, url: str) -> None:
        self.db.update_document(doc_id, status="parsing", error=None, progress=0.02)
        parsed = fetch_url(url)
        doc = self.db.get_document(doc_id) or {}
        if doc.get("title") in (None, "", url):
            self.db.update_document(doc_id, title=parsed.title)
        self._store(doc_id, parsed)

    def _store(self, doc_id: int, parsed) -> None:
        size = int(self.runtime.get("chunk_size", 800))
        overlap = int(self.runtime.get("chunk_overlap", 120))
        chunks = chunk_blocks(parsed.blocks, size=size, overlap=overlap)
        if not chunks:
            raise ParseError("没有可入库的文本内容")
        self.db.update_document(doc_id, status="embedding", num_chunks=len(chunks), progress=0.2)
        last = {"pct": 0.0}

        def on_progress(done: int, total: int) -> None:
            pct = 0.2 + 0.75 * (done / max(total, 1))
            if pct - last["pct"] >= 0.05 or done == total:
                last["pct"] = pct
                self.db.update_document(doc_id, progress=round(pct, 3))

        vectors = self.runtime.embed_texts([c["text"] for c in chunks], progress=on_progress)
        blobs = [vec.tobytes() for vec in vectors]
        self.db.replace_chunks(doc_id, chunks, blobs)
        current = self.db.get_document(doc_id) or {}
        self.db.update_document(
            doc_id,
            status="ready",
            progress=1.0,
            error=None,
            num_chunks=len(chunks),
            title=current.get("title") or parsed.title,
        )
        self.index.invalidate()

    def _run_reembed(self, doc_id: int) -> None:
        rows = self.db.chunks_of(doc_id)
        if not rows:
            return
        self.db.update_document(doc_id, status="embedding", progress=0.05, error=None)
        last = {"pct": 0.0}

        def on_progress(done: int, total: int) -> None:
            pct = 0.05 + 0.9 * (done / max(total, 1))
            if pct - last["pct"] >= 0.05 or done == total:
                last["pct"] = pct
                self.db.update_document(doc_id, progress=round(pct, 3))

        vectors = self.runtime.embed_texts([r["text"] for r in rows], progress=on_progress)
        self.db.update_embeddings(doc_id, [v.tobytes() for v in vectors])
        self.db.update_document(doc_id, status="ready", progress=1.0, error=None)
        self.index.invalidate()

    def resume_unfinished(self) -> dict:
        """服务重启会把没跑完的入库线程带走，这里把挂着的文档重新排队，避免永远卡在「向量化」。"""
        stuck = [d for d in self.db.list_documents() if d["status"] in {"pending", "parsing", "embedding"}]
        resumed, failed = [], []
        for doc in stuck:
            source = doc.get("source") or ""
            if doc.get("kind") == "file":
                if not source or not Path(source).is_file():
                    self.db.update_document(int(doc["id"]), status="error", error="原始文件已不存在，请重新上传",
                                            progress=0)
                    failed.append(int(doc["id"]))
                    continue
                self._submit(int(doc["id"]), lambda d=int(doc["id"]), p=Path(source): self._run_file(d, p))
            else:
                self._submit(int(doc["id"]), lambda d=int(doc["id"]), u=source: self._run_url(d, u))
            resumed.append(int(doc["id"]))
        return {"resumed": resumed, "failed": failed}

    def busy(self) -> bool:
        with self._lock:
            return bool(self._active)

    def wait_idle(self, timeout: float = 900.0) -> bool:
        import time as _time

        deadline = _time.time() + timeout
        while self.busy() and _time.time() < deadline:
            _time.sleep(0.25)
        return not self.busy()
