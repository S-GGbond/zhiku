"""运行时配置与单例管理。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from .config import MASK, env_config, mask_config, normalize_patch
from .db import Database
from .providers import ConfigError, Embedder, LLMClient, ProviderError


class Runtime:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.data_dir = Path(data_dir)
        self.upload_dir = self.data_dir / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cfg: dict[str, Any] = env_config()
        stored = db.get_setting("config", {}) or {}
        self._cfg.update({k: v for k, v in stored.items() if k in self._cfg})
        dim = db.get_setting("embed_dim", 0) or 0
        if dim:
            self._cfg["embed_dim"] = int(dim)
        self._embedder: Embedder | None = None
        self._llm: LLMClient | None = None
        self._signature: tuple = ()

    # ---------- 配置 ----------
    @property
    def cfg(self) -> dict[str, Any]:
        return self._cfg

    def get(self, key: str, default: Any = None) -> Any:
        return self._cfg.get(key, default)

    def public_config(self) -> dict[str, Any]:
        data = mask_config(self._cfg)
        data["llm_ready"] = bool(self._cfg.get("llm_base_url") and self._cfg.get("llm_api_key")
                                  and self._cfg.get("llm_model"))
        data["embed_ready"] = bool(self._cfg.get("embed_base_url") and self._cfg.get("embed_api_key")
                                   and self._cfg.get("embed_model"))
        data["embed_dim"] = int(self._cfg.get("embed_dim") or 0)
        return data

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        clean = normalize_patch(patch)
        with self._lock:
            before = self._signature
            self._cfg.update(clean)
            persisted = {k: v for k, v in clean.items() if k not in {"embed_dim"}}
            stored = self.db.get_setting("config", {}) or {}
            stored.update(persisted)
            self.db.set_setting("config", stored)
            self._invalidate()
            if self._signature != before:
                self._embedder = None
                self._llm = None
        return self.public_config()

    def reload(self) -> None:
        """从数据库重新读取配置（导入备份后调用，避免内存里还是旧账号/旧模型）。"""
        with self._lock:
            self._cfg = env_config()
            stored = self.db.get_setting("config", {}) or {}
            self._cfg.update({k: v for k, v in stored.items() if k in self._cfg})
            dim = self.db.get_setting("embed_dim", 0) or 0
            if dim:
                self._cfg["embed_dim"] = int(dim)
            self._embedder = None
            self._llm = None
            self._invalidate()

    def _signature_of(self, kind: str) -> tuple:
        if kind == "llm":
            keys = ("llm_base_url", "llm_api_key", "llm_model", "llm_temperature", "llm_max_tokens")
        else:
            keys = ("embed_base_url", "embed_api_key", "embed_model", "embed_batch")
        return tuple(self._cfg.get(k) for k in keys)

    def _invalidate(self) -> None:
        self._signature = (self._signature_of("llm"), self._signature_of("embed"))

    # ---------- 客户端 ----------
    def embedder(self) -> Embedder:
        sig = self._signature_of("embed")
        with self._lock:
            if self._embedder is None or getattr(self, "_embed_sig", None) != sig:
                self._embedder = Embedder(
                    base_url=self._cfg["embed_base_url"],
                    api_key=self._cfg["embed_api_key"],
                    model=self._cfg["embed_model"],
                    batch=int(self._cfg["embed_batch"]),
                )
                self._embed_sig = sig
            return self._embedder

    def llm(self) -> LLMClient:
        sig = self._signature_of("llm")
        with self._lock:
            if self._llm is None or getattr(self, "_llm_sig", None) != sig:
                self._llm = LLMClient(
                    base_url=self._cfg["llm_base_url"],
                    api_key=self._cfg["llm_api_key"],
                    model=self._cfg["llm_model"],
                    temperature=float(self._cfg["llm_temperature"]),
                    max_tokens=int(self._cfg["llm_max_tokens"]),
                )
                self._llm_sig = sig
            return self._llm

    def embed_texts(self, texts: list[str], progress=None, allow_dim_change: bool = False) -> np.ndarray:
        if not self._cfg.get("embed_base_url") or not self._cfg.get("embed_api_key"):
            raise ConfigError("尚未配置向量模型接口，请在「设置」里填写 Base URL 与 API Key")
        vectors = self.embedder().encode(texts, progress=progress)
        dim = int(vectors.shape[1])
        known = int(self._cfg.get("embed_dim") or 0)
        if known and known != dim:
            if not allow_dim_change:
                raise ProviderError(
                    f"向量维度变化（原 {known}，现 {dim}）。请在「设置」页执行「重建全部索引」以适配新模型。"
                )
            self._cfg["embed_dim"] = dim
            self.db.set_setting("embed_dim", dim)
        if not known:
            self._cfg["embed_dim"] = dim
            self.db.set_setting("embed_dim", dim)
        return vectors

    # ---------- 连通性测试 ----------
    def test_embedding(self) -> dict:
        vectors = self.embed_texts(["连通性测试"])
        return {"ok": True, "dim": int(vectors.shape[1]),
                "message": f"向量接口可用，维度 {vectors.shape[1]}，模型 {self._cfg['embed_model']}"}

    def test_llm(self) -> dict:
        text = self.llm().complete([{"role": "user", "content": "回复两个字：可用"}], max_tokens=32)
        return {"ok": True, "message": f"对话接口可用（{self._cfg['llm_model']}）：{text[:40] or '无返回内容'}"}
