"""分块：按标题/段落/句子边界滑动切分，保留页码与标题上下文。"""

from __future__ import annotations

import re

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
MIN_CHUNK = 12


def split_sentences(text: str) -> list[str]:
    parts = [p for p in _SENT_SPLIT.split(text) if p and p.strip()]
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > 1200:  # 超长段落（如无标点的表格/代码）强制硬切
            out.extend(part[i:i + 600] for i in range(0, len(part), 600))
        else:
            out.append(part)
    return out


def chunk_text(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    sentences = split_sentences(text)
    if not sentences:
        return []
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for sentence in sentences:
        if length + len(sentence) > size and current:
            chunks.append("".join(current).strip())
            tail: list[str] = []
            tail_len = 0
            for prev in reversed(current):
                if tail_len + len(prev) > overlap:
                    break
                tail.insert(0, prev)
                tail_len += len(prev)
            current = tail
            length = tail_len
        current.append(sentence)
        length += len(sentence)
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if len(c.strip()) >= MIN_CHUNK]


def _with_heading(body: str, heading: str | None) -> str:
    if not heading:
        return body
    parts = [p.strip() for p in heading.split(">") if p.strip()]
    if not parts:
        return body
    leaf = parts[-1]
    if body.startswith(leaf):
        prefix = " > ".join(parts[:-1])
        return f"{prefix}\n{body}" if prefix else body
    return f"{heading}\n{body}"


def chunk_blocks(blocks, size: int = 800, overlap: int = 120) -> list[dict]:
    """把解析块切成检索单元：先补标题上下文，再把过短的标题块并入下一块。"""
    out: list[dict] = []
    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        heading = block.heading
        pieces = chunk_text(text, size=size, overlap=overlap) if len(text) > size else [text]
        for piece in pieces:
            if len(piece.strip()) < MIN_CHUNK:
                continue
            out.append({"text": _with_heading(piece, heading), "page": block.page, "heading": heading})

    merged: list[dict] = []
    index = 0
    while index < len(out):
        current = out[index]
        following = out[index + 1] if index + 1 < len(out) else None
        too_short = len(current["text"]) < 40
        if too_short and following and current["text"] in following["text"]:
            index += 1
            continue
        if too_short and following and len(current["text"]) + len(following["text"]) <= size * 1.6:
            following["text"] = current["text"] + "\n" + following["text"]
            if not following.get("heading"):
                following["heading"] = current.get("heading")
            index += 1
            continue
        merged.append(current)
        index += 1
    return merged
