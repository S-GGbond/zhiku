"""混合检索：向量召回 + BM25 关键词召回，RRF 融合。"""

from __future__ import annotations

import logging
import math
import threading
from collections import Counter, defaultdict
from typing import Any

import jieba
import numpy as np

jieba.setLogLevel(logging.WARNING)

STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "也", "都", "就", "而", "被", "把", "对",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "这", "那", "这个", "那个", "什么",
    "怎么", "如何", "为什么", "吗", "呢", "吧", "啊", "一个", "可以", "有", "没有", "请", "帮",
}


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in jieba.lcut(text.lower()):
        token = raw.strip()
        if not token or token in STOPWORDS:
            continue
        if all(not ch.isalnum() for ch in token):
            continue
        if len(token) == 1 and not token.isascii():
            if token not in {"云", "库", "表", "图", "码"}:
                continue
        tokens.append(token)
    return tokens


class Hit(dict):
    pass


class RetrievalIndex:
    def __init__(self, db, runtime):
        self.db = db
        self.runtime = runtime
        self._lock = threading.RLock()
        self._version = -1
        self.entries: list[dict] = []
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.vec_rows: list[int] = []
        self.doc_ids = np.zeros(0, dtype=np.int64)
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.dl = np.zeros(0, dtype=np.float32)
        self.avgdl = 1.0

    # ---------- 装载 ----------
    def _version_now(self) -> int:
        try:
            return int(self.db.get_meta("index_version", "0"))
        except ValueError:
            return 0

    def ensure_loaded(self) -> None:
        version = self._version_now()
        if version == self._version:
            return
        with self._lock:
            if version == self._version:
                return
            self._load()
            self._version = version

    def _load(self) -> None:
        rows = self.db.iter_chunks(with_embedding=True)
        entries: list[dict] = []
        vectors: list[np.ndarray] = []
        vec_rows: list[int] = []
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        dl = np.zeros(len(rows), dtype=np.float32)
        dim: int | None = None
        skipped = 0
        for i, row in enumerate(rows):
            text = row["text"]
            entries.append({
                "id": int(row["id"]),
                "doc_id": int(row["doc_id"]),
                "seq": int(row["seq"]),
                "text": text,
                "page": row["page"],
                "heading": row["heading"],
            })
            tokens = tokenize(text)
            dl[i] = max(1, len(tokens))
            for token, count in Counter(tokens).items():
                postings[token].append((i, count))
            blob = row["embedding"]
            if blob:
                vec = np.frombuffer(blob, dtype=np.float32)
                # 换模型重建到一半时新旧维度会共存，这里跳过不一致的，别让整个索引起不来
                if dim is None:
                    dim = vec.size
                if vec.size != dim:
                    skipped += 1
                    continue
                vectors.append(vec)
                vec_rows.append(i)
        if skipped:
            log.warning("跳过 %s 个维度不一致的向量（旧模型残留，重建全部向量可消除）", skipped)
        matrix = np.vstack(vectors).astype(np.float32) if vectors else np.zeros((0, 0), dtype=np.float32)
        if matrix.size:
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
        self.entries = entries
        self.postings = dict(postings)
        self.dl = dl
        self.avgdl = float(dl.mean()) if len(dl) else 1.0
        self.matrix = matrix
        self.vec_rows = vec_rows
        self.doc_ids = np.array([e["doc_id"] for e in entries], dtype=np.int64)

    def invalidate(self) -> None:
        self._version = -1

    # ---------- 检索 ----------
    def search(self, query: str, *, top_k_vector: int | None = None, top_k_keyword: int | None = None,
               final_k: int | None = None, doc_ids: list[int] | None = None) -> dict:
        self.ensure_loaded()
        cfg = self.runtime.cfg
        top_k_vector = top_k_vector or int(cfg["top_k_vector"])
        top_k_keyword = top_k_keyword or int(cfg["top_k_keyword"])
        final_k = final_k or int(cfg["final_k"])
        query = (query or "").strip()
        if not query or not self.entries:
            return {"hits": [], "meta": {"reason": "empty"}}

        mask = np.ones(len(self.entries), dtype=bool)
        if doc_ids:
            wanted = set(int(i) for i in doc_ids)
            mask = np.isin(self.doc_ids, list(wanted))
            if not mask.any():
                return {"hits": [], "meta": {"reason": "no_document_match"}}

        warnings: list[str] = []
        vec_scores = np.full(len(self.entries), -np.inf, dtype=np.float32)
        vec_used = False
        if self.matrix.size:
            try:
                qvec = self.runtime.embed_texts([query])[0]
                if qvec.shape[0] == self.matrix.shape[1]:
                    scores = self.matrix @ qvec
                    vec_scores[self.vec_rows] = scores
                    vec_used = True
                else:
                    warnings.append("向量维度与索引不一致，已退回关键词检索，请重建索引")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"向量检索不可用：{exc}")
        vec_scores[~mask] = -np.inf
        kw_scores = self._bm25(query)
        kw_scores[~mask] = -np.inf

        vec_rank = self._top_rank(vec_scores, top_k_vector)
        kw_rank = self._top_rank(kw_scores, top_k_keyword)

        rrf_k = 60
        fused: dict[int, float] = defaultdict(float)
        for rank, idx in enumerate(vec_rank, start=1):
            fused[idx] += 1.0 / (rrf_k + rank)
        for rank, idx in enumerate(kw_rank, start=1):
            fused[idx] += 1.0 / (rrf_k + rank)

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:final_k]
        titles = self.db.document_titles()
        hits = []
        for idx, score in ranked:
            entry = self.entries[idx]
            hits.append({
                "chunk_id": entry["id"],
                "doc_id": entry["doc_id"],
                "doc_title": titles.get(entry["doc_id"], "未知文档"),
                "seq": entry["seq"],
                "page": entry["page"],
                "heading": entry["heading"],
                "text": entry["text"],
                "score": round(float(score), 6),
                "vector_score": round(float(vec_scores[idx]), 4) if np.isfinite(vec_scores[idx]) else None,
                "keyword_score": round(float(kw_scores[idx]), 4) if np.isfinite(kw_scores[idx]) else None,
            })
        return {
            "hits": hits,
            "meta": {
                "vector_used": vec_used,
                "keyword_used": True,
                "candidates": len(self.entries),
                "warnings": warnings,
            },
        }

    def _top_rank(self, scores: np.ndarray, k: int) -> list[int]:
        if scores.size == 0 or k <= 0:
            return []
        finite = np.isfinite(scores)
        if not finite.any():
            return []
        positive = np.where(finite)[0]
        candidates = positive[scores[positive] > 0]
        if candidates.size == 0:
            return []
        k = min(k, candidates.size)
        if candidates.size <= k:
            order = candidates[np.argsort(-scores[candidates])]
        else:
            part = np.argpartition(-scores[candidates], k - 1)[:k]
            order = candidates[part]
            order = order[np.argsort(-scores[order])]
        return [int(i) for i in order]

    def _bm25(self, query: str, k1: float = 1.5, b: float = 0.75) -> np.ndarray:
        scores = np.zeros(len(self.entries), dtype=np.float32)
        tokens = tokenize(query)
        if not tokens or not self.entries:
            return scores
        total = len(self.entries)
        for token in set(tokens):
            postings = self.postings.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            for idx, tf in postings:
                norm = k1 * (1 - b + b * float(self.dl[idx]) / self.avgdl)
                scores[idx] += idf * (tf * (k1 + 1)) / (tf + norm)
        return scores

    def info(self) -> dict[str, Any]:
        self.ensure_loaded()
        return {
            "chunks_indexed": len(self.entries),
            "chunks_vectorized": len(self.vec_rows),
            "vocabulary": len(self.postings),
            "embed_dim": int(self.matrix.shape[1]) if self.matrix.size else 0,
            "version": self._version,
        }
