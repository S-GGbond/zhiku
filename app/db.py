"""SQLite 存储：文档元数据、分块文本与向量（float32 BLOB）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'file',
    mime        TEXT,
    filename    TEXT,
    size        INTEGER DEFAULT 0,
    sha256      TEXT,
    tags        TEXT DEFAULT '',
    num_chunks  INTEGER DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    progress    REAL DEFAULT 0,
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS doc_blobs (
    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    data   BLOB NOT NULL,
    name   TEXT,
    mime   TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    page        INTEGER,
    heading     TEXT,
    char_len    INTEGER DEFAULT 0,
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


DOC_COLS = ("id", "title", "source", "kind", "mime", "filename", "size", "sha256",
            "tags", "num_chunks", "status", "progress", "error", "created_at", "updated_at")
CHUNK_COLS = ("id", "doc_id", "seq", "text", "page", "heading", "char_len", "embedding")


def now() -> float:
    return time.time()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """老库补列：documents.tags（Agent 写入时打的标签）。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        if "tags" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN tags TEXT DEFAULT ''")

    # ---------- 连接 ----------
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn().execute(sql, tuple(args)).fetchall())

    def query_one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(args)).fetchone()

    def execute(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._write_lock:
            conn = self.conn()
            cur = conn.execute(sql, tuple(args))
            conn.commit()
            return int(cur.lastrowid or 0)

    # ---------- meta / settings ----------
    def get_meta(self, key: str, default: str = "0") -> str:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def bump_version(self) -> None:
        self.set_meta("index_version", str(int(self.get_meta("index_version", "0")) + 1))

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---------- documents ----------
    def find_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM documents WHERE sha256 = ?", (sha256,))

    def find_by_source(self, source: str) -> sqlite3.Row | None:
        """同一条来源（文件绝对路径 / note 文件）视为同一份文档，用于覆盖更新。"""
        return self.query_one("SELECT * FROM documents WHERE source = ?", (source,))

    def create_document(self, *, title: str, source: str, kind: str = "file", mime: str | None = None,
                        size: int = 0, sha256: str | None = None, filename: str | None = None,
                        tags: str = "") -> int:
        ts = now()
        return self.execute(
            "INSERT INTO documents(title, source, kind, mime, filename, size, sha256, tags, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (title, source, kind, mime, filename, size, sha256, tags, "pending", ts, ts),
        )

    def update_document(self, doc_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*fields.values(), doc_id))

    def delete_document(self, doc_id: int) -> None:
        self.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self.bump_version()

    def list_documents(self) -> list[dict]:
        rows = self.query("SELECT * FROM documents ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def get_document(self, doc_id: int) -> dict | None:
        row = self.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        return dict(row) if row else None

    # ---------- 原始文件 ----------
    def put_blob(self, doc_id: int, data: bytes, name: str | None = None,
                 mime: str | None = None) -> None:
        self.execute(
            "INSERT INTO doc_blobs(doc_id, data, name, mime) VALUES(?,?,?,?)"
            " ON CONFLICT(doc_id) DO UPDATE SET data = excluded.data, name = excluded.name,"
            " mime = excluded.mime",
            (doc_id, sqlite3.Binary(data), name, mime),
        )

    def get_blob(self, doc_id: int) -> tuple[bytes, str, str] | None:
        row = self.query_one("SELECT data, name, mime FROM doc_blobs WHERE doc_id = ?", (doc_id,))
        if not row:
            return None
        return bytes(row["data"]), (row["name"] or ""), (row["mime"] or "")

    def has_blob(self, doc_id: int) -> bool:
        return self.query_one("SELECT 1 AS ok FROM doc_blobs WHERE doc_id = ?", (doc_id,)) is not None

    def docs_without_blob(self) -> list[dict]:
        rows = self.query(
            "SELECT d.* FROM documents d LEFT JOIN doc_blobs b ON b.doc_id = d.id"
            " WHERE b.doc_id IS NULL"
        )
        return [dict(r) for r in rows]

    # ---------- chunks ----------
    def replace_chunks(self, doc_id: int, chunks: list[dict], embeddings: list[bytes] | None) -> None:
        with self._write_lock:
            conn = self.conn()
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            rows = [
                (
                    doc_id,
                    i,
                    c["text"],
                    c.get("page"),
                    c.get("heading"),
                    len(c["text"]),
                    embeddings[i] if embeddings else None,
                )
                for i, c in enumerate(chunks)
            ]
            conn.executemany(
                "INSERT INTO chunks(doc_id, seq, text, page, heading, char_len, embedding)"
                " VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        self.bump_version()

    def iter_chunks(self, doc_ids: list[int] | None = None, with_embedding: bool = True) -> list[sqlite3.Row]:
        cols = "c.id, c.doc_id, c.seq, c.text, c.page, c.heading" + (", c.embedding" if with_embedding else "")
        if doc_ids:
            placeholders = ",".join("?" * len(doc_ids))
            sql = f"SELECT {cols} FROM chunks c WHERE c.doc_id IN ({placeholders}) ORDER BY c.doc_id, c.seq"
            return self.query(sql, doc_ids)
        return self.query(f"SELECT {cols} FROM chunks c ORDER BY c.doc_id, c.seq")

    def update_embeddings(self, doc_id: int, embeddings: list[bytes]) -> None:
        rows = self.query("SELECT id FROM chunks WHERE doc_id = ? ORDER BY seq", (doc_id,))
        if len(rows) != len(embeddings):
            raise ValueError("分块数量与向量数量不一致")
        with self._write_lock:
            conn = self.conn()
            conn.executemany(
                "UPDATE chunks SET embedding = ? WHERE id = ?",
                [(blob, int(row["id"])) for row, blob in zip(rows, embeddings)],
            )
            conn.commit()
        self.bump_version()

    def chunks_of(self, doc_id: int) -> list[dict]:
        rows = self.query(
            "SELECT id, seq, text, page, heading, char_len, length(embedding) AS embed_bytes"
            " FROM chunks WHERE doc_id = ? ORDER BY seq",
            (doc_id,),
        )
        return [dict(r) for r in rows]

    def replace_all_from(self, sqlite_path: str | Path) -> dict:
        """用另一个库的 documents/chunks/settings 整体替换本库（导入备份用）。"""
        conn = self.conn()
        conn.execute("ATTACH DATABASE ? AS incoming", (str(sqlite_path),))
        try:
            have_docs = {r["name"] for r in conn.execute("PRAGMA incoming.table_info(documents)")}
            have_chunks = {r["name"] for r in conn.execute("PRAGMA incoming.table_info(chunks)")}
            doc_cols = [c for c in DOC_COLS if c in have_docs]
            chunk_cols = [c for c in CHUNK_COLS if c in have_chunks]
            have_blobs = {r["name"] for r in conn.execute("PRAGMA incoming.table_info(doc_blobs)")}
            with self._write_lock:
                conn.execute("DELETE FROM doc_blobs")
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM documents")
                conn.execute(f"INSERT INTO documents({','.join(doc_cols)})"
                             f" SELECT {','.join(doc_cols)} FROM incoming.documents")
                conn.execute(f"INSERT INTO chunks({','.join(chunk_cols)})"
                             f" SELECT {','.join(chunk_cols)} FROM incoming.chunks")
                if have_blobs:
                    conn.execute("INSERT INTO doc_blobs(doc_id, data, name, mime)"
                                 " SELECT doc_id, data, name, mime FROM incoming.doc_blobs")
                conn.execute("DELETE FROM settings")
                conn.execute("INSERT INTO settings(key, value) SELECT key, value FROM incoming.settings")
                conn.commit()
            docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        finally:
            conn.execute("DETACH DATABASE incoming")
        self.bump_version()
        return {"documents": int(docs), "chunks": int(chunks)}

    def document_titles(self) -> dict[int, str]:
        return {int(r["id"]): r["title"] for r in self.query("SELECT id, title FROM documents")}

    # ---------- stats ----------
    def stats(self) -> dict:
        doc = self.query_one("SELECT COUNT(*) AS n FROM documents")
        ok = self.query_one("SELECT COUNT(*) AS n FROM documents WHERE status = 'ready'")
        chunk = self.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(char_len), 0) AS chars FROM chunks")
        embedded = self.query_one("SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL")
        return {
            "documents": int(doc["n"]) if doc else 0,
            "documents_ready": int(ok["n"]) if ok else 0,
            "chunks": int(chunk["n"]) if chunk else 0,
            "chunks_embedded": int(embedded["n"]) if embedded else 0,
            "total_chars": int(chunk["chars"]) if chunk else 0,
        }
