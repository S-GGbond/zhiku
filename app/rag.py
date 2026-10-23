"""检索增强问答：查询改写、上下文拼装、流式回答。"""

from __future__ import annotations

from typing import Iterator

SYSTEM_PROMPT = """你是严谨的知识库问答助手。请遵守：
1. 只能依据【资料】中的内容作答，不得使用资料外的知识或自行推测。
2. 每处结论后用 [编号] 标注来源，例如「入职时间是 2024-03-01 [1]」。
3. 资料不足以回答时，直接说明「知识库中没有找到相关依据」，并指出缺少什么信息。
4. 回答使用简体中文，结构清晰；能一句话说清就不要长篇罗列。"""

REWRITE_PROMPT = """根据对话历史，把用户的最新问题改写成一句可以独立检索的完整问题。
要求：只输出改写后的问题本身，不要解释、不要加引号；保留关键实体与限定条件。"""


def _sources(hits: list[dict]) -> list[dict]:
    return [
        {
            "index": i + 1,
            "chunk_id": h["chunk_id"],
            "doc_id": h["doc_id"],
            "doc_title": h["doc_title"],
            "page": h["page"],
            "heading": (h["heading"] or "").split(">")[-1].strip() or None,
            "score": h["score"],
            "preview": h["text"][:600],
        }
        for i, h in enumerate(hits)
    ]


def build_context(hits: list[dict], max_chars: int = 6000) -> str:
    parts: list[str] = []
    used = 0
    for i, hit in enumerate(hits, start=1):
        meta = [f"《{hit['doc_title']}》"]
        if hit.get("page"):
            meta.append(f"第 {hit['page']} 页")
        if hit.get("heading"):
            meta.append(str(hit["heading"])[:60])
        block = f"[{i}] " + " · ".join(meta) + "\n" + hit["text"].strip()
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def rewrite_query(llm, question: str, history: list[dict]) -> str:
    if not history:
        return question
    recent = history[-6:]
    lines = []
    for msg in recent:
        role = "用户" if msg.get("role") == "user" else "助手"
        lines.append(f"{role}：{str(msg.get('content', ''))[:300]}")
    prompt = "对话历史：\n" + "\n".join(lines) + f"\n\n最新问题：{question}\n\n改写后的问题："
    try:
        text = llm.complete(
            [{"role": "system", "content": REWRITE_PROMPT}, {"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.0,
        )
        text = text.strip().strip('"').strip("「」").split("\n")[0]
        return text if 1 < len(text) <= 200 else question
    except Exception:  # noqa: BLE001 改写失败不影响主流程
        return question


def build_messages(question: str, hits: list[dict], history: list[dict], max_chars: int) -> list[dict]:
    context = build_context(hits, max_chars=max_chars)
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history[-6:]:
        role = msg.get("role")
        content = str(msg.get("content", ""))[:2000]
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({
        "role": "user",
        "content": f"【资料】\n{context}\n\n【问题】\n{question}\n\n请依据资料回答，并标注引用编号。",
    })
    return messages


def prepare(runtime, index, question: str, history: list[dict] | None = None,
            doc_ids: list[int] | None = None, top_k: int | None = None) -> dict:
    """完成检索与提示词构造，供流式与非流式复用。"""
    history = history or []
    cfg = runtime.cfg
    search_query = question
    rewritten = False
    if cfg.get("query_rewrite") and history:
        search_query = rewrite_query(runtime.llm(), question, history)
        rewritten = search_query.strip() != question.strip()
    result = index.search(
        search_query,
        final_k=top_k or int(cfg["final_k"]),
        doc_ids=doc_ids,
    )
    hits = result["hits"]
    messages = build_messages(question, hits, history, int(cfg["max_context_chars"]))
    return {
        "hits": hits,
        "sources": _sources(hits),
        "messages": messages,
        "search_query": search_query,
        "rewritten": rewritten,
        "meta": result["meta"],
    }


NO_HIT_ANSWER = "知识库中没有找到相关依据。可以换一种说法再问，或先上传包含该主题的文档。"


def retrieve(runtime, index, question: str, doc_ids: list[int] | None = None,
             top_k: int | None = None) -> dict:
    """不调用对话模型，只返回检索到的原文片段。"""
    result = index.search(question, final_k=top_k, doc_ids=doc_ids)
    return {
        "hits": result["hits"],
        "sources": _sources(result["hits"]),
        "retrieval": result["meta"],
    }


def retrieve_stream(runtime, index, question: str, doc_ids: list[int] | None = None,
                    top_k: int | None = None) -> Iterator[dict]:
    try:
        data = retrieve(runtime, index, question, doc_ids, top_k)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
        return
    yield {
        "type": "meta",
        "mode": "retrieve",
        "sources": data["sources"],
        "hits": data["hits"],
        "retrieval": data["retrieval"],
    }
    yield {"type": "done"}


def answer_stream(runtime, index, question: str, history: list[dict] | None = None,
                  doc_ids: list[int] | None = None, top_k: int | None = None) -> Iterator[dict]:
    try:
        prepared = prepare(runtime, index, question, history, doc_ids, top_k)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
        return

    yield {
        "type": "meta",
        "sources": prepared["sources"],
        "search_query": prepared["search_query"],
        "rewritten": prepared["rewritten"],
        "retrieval": prepared["meta"],
    }
    if not prepared["hits"]:
        yield {"type": "delta", "text": NO_HIT_ANSWER}
        yield {"type": "done"}
        return
    try:
        for piece in runtime.llm().stream(prepared["messages"]):
            yield {"type": "delta", "text": piece}
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
        return
    yield {"type": "done"}


def answer(runtime, index, question: str, history: list[dict] | None = None,
           doc_ids: list[int] | None = None, top_k: int | None = None) -> dict:
    prepared = prepare(runtime, index, question, history, doc_ids, top_k)
    if not prepared["hits"]:
        return {
            "answer": NO_HIT_ANSWER,
            "sources": [],
            "search_query": prepared["search_query"],
            "retrieval": prepared["meta"],
        }
    text = runtime.llm().complete(prepared["messages"])
    return {
        "answer": text,
        "sources": prepared["sources"],
        "search_query": prepared["search_query"],
        "rewritten": prepared["rewritten"],
        "retrieval": prepared["meta"],
    }
