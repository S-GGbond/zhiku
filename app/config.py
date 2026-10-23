"""配置解析：默认值 -> .env -> 环境变量 -> WebUI 保存的覆盖值。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    # 服务
    "host": "127.0.0.1",
    "port": 8000,
    "api_token": "",
    # 对话模型
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "glm-4-flash",
    "llm_temperature": 0.2,
    "llm_max_tokens": 1500,
    # 向量模型
    "embed_base_url": "",
    "embed_api_key": "",
    "embed_model": "text-embedding-3-small",
    "embed_batch": 32,
    "embed_dim": 0,
    # 检索与切分
    "chunk_size": 800,
    "chunk_overlap": 120,
    "top_k_vector": 20,
    "top_k_keyword": 20,
    "final_k": 6,
    "max_context_chars": 6000,
    "query_rewrite": True,
    # 上传
    "max_upload_mb": 50,
}

SECRET_FIELDS = ("llm_api_key", "embed_api_key", "api_token")
MASK = "********"

_ENV_MAP = {
    "RAG_HOST": ("host", str),
    "RAG_PORT": ("port", int),
    "RAG_API_TOKEN": ("api_token", str),
    "LLM_BASE_URL": ("llm_base_url", str),
    "LLM_API_KEY": ("llm_api_key", str),
    "LLM_MODEL": ("llm_model", str),
    "LLM_TEMPERATURE": ("llm_temperature", float),
    "LLM_MAX_TOKENS": ("llm_max_tokens", int),
    "EMBED_BASE_URL": ("embed_base_url", str),
    "EMBED_API_KEY": ("embed_api_key", str),
    "EMBED_MODEL": ("embed_model", str),
    "EMBED_BATCH": ("embed_batch", int),
    "CHUNK_SIZE": ("chunk_size", int),
    "CHUNK_OVERLAP": ("chunk_overlap", int),
    "TOP_K_VECTOR": ("top_k_vector", int),
    "TOP_K_KEYWORD": ("top_k_keyword", int),
    "FINAL_K": ("final_k", int),
    "MAX_CONTEXT_CHARS": ("max_context_chars", int),
    "QUERY_REWRITE": ("query_rewrite", bool),
    "MAX_UPLOAD_MB": ("max_upload_mb", int),
}

# 复用机器上已有的多供应商密钥，避免重复填写
PRESETS: dict[str, dict[str, str]] = {
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "key": "GLM_API_KEY", "llm": "glm-4-flash"},
    "mx": {"base_url": "https://api.mxzzz.xyz/v1", "key": "MX_API_KEY", "llm": "gpt-6-sol"},
    "kkrich": {"base_url": "https://api.kkrich.ltd/v1", "key": "OPENAI_API_KEY", "llm": "deepseek-v4.1-flash"},
}


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载器；已存在的环境变量不覆盖。"""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _coerce(value: Any, caster: type) -> Any:
    if isinstance(value, str):
        if caster is bool:
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if not value.strip():
            return None
        try:
            return caster(value)
        except (TypeError, ValueError):
            return None
    if caster is int and isinstance(value, float):
        return int(value)
    return value


def env_config() -> dict[str, Any]:
    """从环境变量（含 .env、供应商预设）推导出初始配置。"""
    load_dotenv()
    cfg = dict(DEFAULTS)

    llm_preset = os.getenv("LLM_PRESET", "").strip().lower()
    embed_preset = os.getenv("EMBED_PRESET", "").strip().lower() or llm_preset
    for preset_name, target in ((llm_preset, "llm"), (embed_preset, "embed")):
        preset = PRESETS.get(preset_name)
        if not preset:
            continue
        base = os.getenv(f"{target.upper()}_BASE_URL") or preset["base_url"]
        key = os.getenv(f"{target.upper()}_API_KEY") or os.getenv(preset["key"], "")
        cfg[f"{target}_base_url"] = base
        cfg[f"{target}_api_key"] = key
        if target == "llm" and not os.getenv("LLM_MODEL"):
            cfg["llm_model"] = preset["llm"]

    for env_name, (field, caster) in _ENV_MAP.items():
        raw = os.getenv(env_name)
        if raw is None or not str(raw).strip():
            continue
        value = _coerce(raw, caster)
        if value is not None:
            cfg[field] = value
    return cfg


def mask_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    for field in SECRET_FIELDS:
        value = out.get(field) or ""
        out[field] = MASK if value else ""
        out[f"{field}_set"] = bool(value)
    return out


def normalize_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """过滤未知字段、忽略未修改的掩码密钥、转换类型。"""
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        if key in SECRET_FIELDS and value == MASK:
            continue
        if key == "embed_dim":
            continue
        caster = type(DEFAULTS[key]) if DEFAULTS[key] is not None else str
        if caster is bool:
            clean[key] = bool(value)
        elif caster is int:
            clean[key] = int(value)
        elif caster is float:
            clean[key] = float(value)
        else:
            clean[key] = "" if value is None else str(value).strip()
    for key in ("chunk_size",):
        if key in clean:
            clean[key] = max(100, min(4000, clean[key]))
    for key in ("chunk_overlap",):
        if key in clean:
            clean[key] = max(0, min(1000, clean[key]))
    for key in ("top_k_vector", "top_k_keyword"):
        if key in clean:
            clean[key] = max(1, min(100, clean[key]))
    if "final_k" in clean:
        clean["final_k"] = max(1, min(20, clean["final_k"]))
    if "embed_batch" in clean:
        clean["embed_batch"] = max(1, min(128, clean["embed_batch"]))
    if "llm_temperature" in clean:
        clean["llm_temperature"] = max(0.0, min(1.5, clean["llm_temperature"]))
    return clean
