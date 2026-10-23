"""整库备份与迁移：导出 zip，导入覆盖恢复（含文档、向量、账号与配置）。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

FORMAT = "my-rag-backup"
VERSION = 1


def export_zip(db, data_dir: Path, runtime) -> Path:
    """把数据库、原始文件、笔记打包成一个 zip。数据库用 SQLite backup API 取一致快照。"""
    data_dir = Path(data_dir)
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"kb-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"

    stage = Path(tempfile.mkdtemp(prefix="kb-export-"))
    snapshot = stage / "kb.sqlite3"
    try:
        src = sqlite3.connect(db.path)
        dst = sqlite3.connect(snapshot)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()

        stats = db.stats()
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "data_dir": str(data_dir.resolve()),
            "documents": stats["documents"],
            "chunks": stats["chunks"],
            "embed_dim": int(runtime.get("embed_dim") or 0),
            "embed_model": runtime.get("embed_model"),
            "llm_model": runtime.get("llm_model"),
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, "kb.sqlite3")
            for folder in ("uploads", "notes"):
                base = data_dir / folder
                if not base.exists():
                    continue
                for path in base.rglob("*"):
                    if path.is_file():
                        zf.write(path, f"{folder}/{path.relative_to(base).as_posix()}")
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return target


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    for name in zf.namelist():
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts:
            raise ValueError(f"备份包内含非法路径：{name}")
    zf.extractall(dest)


def import_zip(zip_path: Path, db, data_dir: Path, runtime, index) -> dict:
    """导入备份：清空现有文档与配置后整体恢复，源路径按新数据目录重写。"""
    data_dir = Path(data_dir)
    with zipfile.ZipFile(zip_path) as zf:
        if "manifest.json" not in zf.namelist():
            raise ValueError("不是知识库备份包（缺少 manifest.json）")
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("备份格式不匹配，可能不是本系统导出的")
        if int(manifest.get("version", 0)) > VERSION:
            raise ValueError("备份来自更新的版本，请先升级本服务")
        stage = Path(tempfile.mkdtemp(prefix="kb-import-"))
        try:
            _safe_extract(zf, stage)
            staged_db = stage / "kb.sqlite3"
            if not staged_db.is_file():
                raise ValueError("备份包缺少 kb.sqlite3")

            backup_dir = data_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            rolled = backup_dir / f"before-import-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
            src = sqlite3.connect(db.path)
            dst = sqlite3.connect(rolled)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()

            counts = db.replace_all_from(staged_db)

            old_root = str(manifest.get("data_dir") or "").rstrip("/")
            if old_root:
                db.conn().execute(
                    "UPDATE documents SET source = ? || substr(source, ?) WHERE source LIKE ?",
                    (str(data_dir.resolve()), len(old_root) + 1, old_root + "/%"),
                )
                db.conn().commit()

            files = 0
            for folder in ("uploads", "notes"):
                src_dir = stage / folder
                if not src_dir.is_dir():
                    continue
                dst_dir = data_dir / folder
                dst_dir.mkdir(parents=True, exist_ok=True)
                for path in src_dir.rglob("*"):
                    if path.is_file():
                        target = dst_dir / path.relative_to(src_dir)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
                        files += 1
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    index.invalidate()
    warnings = []
    old_dim = int(manifest.get("embed_dim") or 0)
    new_dim = int(runtime.get("embed_dim") or 0)
    if old_dim and new_dim and old_dim != new_dim:
        warnings.append(f"向量维度不一致（备份 {old_dim}，当前 {new_dim}），建议在文档页「重建全部向量」")
    return {
        "documents": counts["documents"],
        "chunks": counts["chunks"],
        "files": files,
        "rollback_db": str(rolled),
        "warnings": warnings,
        "need_relogin": True,
    }
