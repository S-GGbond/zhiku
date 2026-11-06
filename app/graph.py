"""知识关系层：把文档里的实体与关系抽出来存成图，供图谱页展示与检索时沿边扩一跳。"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("rag.graph")

EXTRACT_PROMPT = """你是知识图谱抽取器。读下面这篇文档，抽出实体和它们之间的关系，只输出 JSON。

输出格式：
{"entities":[{"name":"实体名","kind":"概念|人物|组织|系统|指标|流程|文件"}],
 "relations":[{"src":"实体名A","rel":"关系","dst":"实体名B"}]}

要求：
1. 实体 5-12 个，优先专有名词：系统名、模块名、指标名、算法名、项目名、关键概念。不要抽「方法」「内容」「问题」「情况」这类空泛词。
2. 关系 3-10 条，src 与 dst 必须是 entities 里出现过的 name；rel 用 2-6 个字的动词或动词短语，如「依赖」「导致」「属于」「替代」「约束」「产出」「包含」。
3. 只写文档中明确支持的，推测出来的不要写。确实没有关系时 relations 返回 []。
4. 只输出 JSON 本身，不要解释，不要 markdown 代码块。"""

ENTITY_KINDS = {
    "概念", "人物", "组织", "系统", "指标", "流程", "文件", "产品", "技术",
    "模型", "工具", "事件", "规则", "数据", "接口", "模块",
}

# 太泛的词不进图，否则整张图会被「系统」「数据」这类节点糊死
STOP_ENTITIES = {
    "方法", "内容", "问题", "情况", "部分", "相关信息", "数据", "文档",
    "系统", "功能", "结果", "方案", "过程", "方面", "时间", "方式", "用户", "我们",
    "本文", "文章", "如下", "以上", "说明", "总结", "概况",
}

_PUNCT = re.compile(r"^[\s\"'“”‘’《》〈〉()（）\[\]【】{}<>·、,，.。;；:：!！?？\-—_/\\|]+")
_REL_CLEAN = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize(name: str) -> str:
    """归一化：去掉引号标点、压缩空白、英文转小写，用于判重。"""
    text = _PUNCT.sub("", str(name or "").strip())
    text = re.sub(r"\s+", "", text)
    return text.lower()


def _clean_name(raw) -> str:
    name = _PUNCT.sub("", str(raw or "").strip()).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:30]


def _valid(name: str) -> bool:
    if len(name) < 2 or len(name) > 30:
        return False
    if name in STOP_ENTITIES or name.lower() in STOP_ENTITIES:
        return False
    if all(not ch.isalnum() for ch in name):
        return False
    return True


def _loads(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract(llm, title: str, text: str, max_chars: int = 6000) -> dict:
    """调一次对话模型，返回 {entities, relations}；解析失败返回空。"""
    body = (text or "")[:max_chars]
    prompt = f"文档标题：{title}\n\n文档内容：\n{body}"
    raw = llm.complete(
        [{"role": "system", "content": EXTRACT_PROMPT},
         {"role": "user", "content": prompt}],
        max_tokens=900,
        temperature=0.0,
        timeout=60,  # 抽取只是加分项，别让它长时间占住线程
    )
    data = _loads(raw)
    if not data:
        log.warning("图谱抽取返回无法解析：%s", (raw or "")[:120])
        return {"entities": [], "relations": []}

    entities: dict[str, dict] = {}
    for item in data.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_name(item.get("name"))
        norm = normalize(name)
        if not norm or norm in entities or not _valid(name):
            continue
        kind = str(item.get("kind") or "").strip()
        entities[norm] = {"name": name, "norm": norm, "kind": kind if kind in ENTITY_KINDS else "概念"}

    relations: list[dict] = []
    seen_rel: set[tuple[str, str, str]] = set()
    for item in data.get("relations") or []:
        if not isinstance(item, dict):
            continue
        src, dst = normalize(item.get("src")), normalize(item.get("dst"))
        rel = _REL_CLEAN.sub("", str(item.get("rel") or "")).strip()[:12]
        if not rel or src not in entities or dst not in entities or src == dst:
            continue
        key = (src, rel, dst)
        if key in seen_rel:
            continue
        seen_rel.add(key)
        relations.append({"src": src, "dst": dst, "rel": rel})

    return {"entities": list(entities.values()), "relations": relations}


def store(db, doc_id: int, data: dict, text: str = "") -> dict:
    """把抽取结果写进图：先清掉这篇文档的旧边，再按实体名去重挂上。"""
    db.clear_graph(doc_id)
    entities = data.get("entities") or []
    ids: dict[str, int] = {}
    for ent in entities:
        weight = 1.0 + min(text.count(ent["name"]), 9)
        entity_id = db.upsert_entity(ent["name"], ent["norm"], ent.get("kind", ""))
        if not entity_id:
            continue
        ids[ent["norm"]] = entity_id
        db.link_doc_entity(doc_id, entity_id, weight)
    linked = 0
    for rel in data.get("relations") or []:
        src, dst = ids.get(rel["src"]), ids.get(rel["dst"])
        if src and dst:
            db.add_relation(src, dst, rel["rel"], doc_id)
            linked += 1
    return {"entities": len(ids), "relations": linked}


def build(runtime, db, doc_id: int, text: str | None = None) -> dict:
    """为一篇文档建图；任何失败都只记日志，不影响文档本身已入库的状态。"""
    if not runtime.get("graph_enabled", True):
        return {"status": "disabled"}
    cfg = runtime.cfg
    if not (cfg.get("llm_base_url") and cfg.get("llm_api_key") and cfg.get("llm_model")):
        return {"status": "skipped", "reason": "未配置对话模型"}

    doc = db.get_document(doc_id)
    if not doc:
        return {"status": "missing"}
    if text is None:
        rows = db.chunks_of(doc_id)
        text = "\n".join(r["text"] for r in rows)
    if not (text or "").strip():
        return {"status": "empty"}

    cap = int(runtime.get("graph_max_chars", 6000))
    try:
        data = extract(runtime.llm(), doc.get("title") or "", text, max_chars=cap)
    except Exception as exc:  # noqa: BLE001 上游模型问题不该拖垮入库
        log.warning("图谱抽取失败 doc=%s: %s", doc_id, exc)
        return {"status": "error", "message": str(exc)[:200]}
    if not data["entities"]:
        db.clear_graph(doc_id)
        return {"status": "no_entity"}
    saved = store(db, doc_id, data, text[:cap])
    log.info("图谱已更新 doc=%s：实体 %s，关系 %s", doc_id, saved["entities"], saved["relations"])
    return {"status": "ok", **saved}


# ---------- 查询 ----------
def view(db, doc_id: int | None = None, limit_docs: int = 40, limit_entities: int = 400) -> dict:
    """返回前端画图用的 nodes / edges。doc_id 给了就只看这篇文档及其一层邻居。"""
    db.gc_entities()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    max_nodes = limit_entities + limit_docs

    if doc_id:
        doc = db.get_document(doc_id)
        if not doc:
            return {"nodes": [], "edges": [], "stats": db.graph_stats(), "focus": None}
        docs = [doc]
    else:
        docs = [d for d in db.list_documents() if d.get("status") == "ready"][:limit_docs]

    entity_ids: list[int] = []
    for doc in docs:
        ents = db.entities_of_doc(int(doc["id"]))
        if not ents and doc_id is None:
            continue
        doc_node = f"doc:{doc['id']}"
        nodes[doc_node] = {"id": doc_node, "label": doc["title"], "type": "文档",
                           "ref": int(doc["id"]), "size": max(len(ents), 1), "kind": "doc"}
        for ent in ents:
            ent_node = f"ent:{ent['id']}"
            if ent_node not in nodes and len(nodes) < max_nodes:
                nodes[ent_node] = {"id": ent_node, "label": ent["name"],
                                   "type": ent.get("kind") or "概念",
                                   "ref": int(ent["id"]), "size": float(ent.get("weight") or 1),
                                   "kind": "entity"}
            if ent_node in nodes:
                edges.append({"source": doc_node, "target": ent_node, "rel": "提及",
                              "kind": "mention"})
            if int(ent["id"]) not in entity_ids:
                entity_ids.append(int(ent["id"]))

    for rel in db.relations_of_entities(entity_ids, limit=600):
        src, dst = f"ent:{rel['src_id']}", f"ent:{rel['dst_id']}"
        for node_id, name in ((src, rel["src_name"]), (dst, rel["dst_name"])):
            if node_id not in nodes and len(nodes) < max_nodes:
                nodes[node_id] = {"id": node_id, "label": name, "type": "概念",
                                  "ref": int(node_id.split(":")[1]), "size": 1.0, "kind": "entity"}
        if src in nodes and dst in nodes:
            edges.append({"source": src, "target": dst, "rel": rel["rel"], "kind": "relation"})

    # 同一实体出现在多篇文档里时，文档之间也连一条虚线，表示「隐性共现」
    ent_docs: dict[int, list[str]] = {}
    for edge in edges:
        if edge["kind"] == "mention":
            ent_docs.setdefault(int(edge["target"].split(":")[1]), []).append(edge["source"])
    for ref, doc_nodes in ent_docs.items():
        if len(doc_nodes) < 2:
            continue
        label = nodes.get(f"ent:{ref}", {}).get("label", "")
        for i in range(len(doc_nodes)):
            for j in range(i + 1, len(doc_nodes)):
                edges.append({"source": doc_nodes[i], "target": doc_nodes[j],
                              "rel": f"共现：{label}", "kind": "cooccur"})

    stats = db.graph_stats()
    stats["shown_nodes"] = len(nodes)
    stats["shown_edges"] = len(edges)
    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats,
            "focus": doc_id}


def related(db, doc_id: int, limit: int = 8) -> list[dict]:
    """找一篇文档的邻居：共现实体 + 实体关系，两类合并去重。"""
    out: dict[int, dict] = {}
    for row in db.shared_entity_docs(doc_id, limit=limit):
        out[int(row["doc_id"])] = {
            "doc_id": int(row["doc_id"]), "title": row["title"],
            "shared": [n for n in str(row.get("names") or "").split("、") if n][:6],
            "relations": [],
        }

    ents = db.entities_of_doc(doc_id)
    ids = [int(e["id"]) for e in ents]
    name_by_id = {int(e["id"]): e["name"] for e in ents}
    for rel in db.relations_of_entities(ids, limit=80):
        src, dst = int(rel["src_id"]), int(rel["dst_id"])
        other = dst if src in name_by_id else src if dst in name_by_id else None
        if other is None:
            continue
        for doc in db.docs_of_entity(other, exclude_doc=doc_id, limit=3):
            item = out.setdefault(int(doc["id"]), {
                "doc_id": int(doc["id"]), "title": doc["title"], "shared": [], "relations": [],
            })
            item["relations"].append({
                "rel": rel["rel"],
                "via": name_by_id.get(src) or rel["src_name"],
                "to": name_by_id.get(dst) or rel["dst_name"],
            })
    items = sorted(out.values(), key=lambda x: (len(x["shared"]), len(x["relations"])), reverse=True)
    return items[:limit]


def expand_hits(index, db, hits: list[dict], question: str,
                max_docs: int = 4, per_doc: int = 1) -> list[dict]:
    """检索后沿图扩一跳：把关联文档里最相关的片段补进上下文。"""
    if not hits:
        return []
    seen_docs = {int(h["doc_id"]) for h in hits}
    neighbors: list[int] = []
    for hit in hits[:3]:
        for rel in related(db, int(hit["doc_id"]), limit=4):
            nid = int(rel["doc_id"])
            if nid in seen_docs or nid in neighbors:
                continue
            neighbors.append(nid)
            if len(neighbors) >= max_docs:
                break
        if len(neighbors) >= max_docs:
            break
    if not neighbors:
        return []

    seen_chunks = {int(h["chunk_id"]) for h in hits}
    try:
        # 一次查完所有邻居文档；查询向量由 index 缓存，不会重复调用向量接口
        found = index.search(question, final_k=max(per_doc * len(neighbors), per_doc),
                             doc_ids=neighbors)["hits"]
    except Exception as exc:  # noqa: BLE001
        log.warning("图谱扩展检索失败：%s", exc)
        return []
    extra: list[dict] = []
    for hit in found:
        if int(hit["chunk_id"]) in seen_chunks:
            continue
        seen_chunks.add(int(hit["chunk_id"]))
        hit["via"] = "graph"
        extra.append(hit)
        if len(extra) >= max_docs:
            break
    return extra
