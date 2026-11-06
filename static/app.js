/* 本地知识库前端：原生 JS，无构建步骤。 */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    view: "chat",
    config: null,
    docs: [],
    messages: [],
    scope: new Set(),
    sending: false,
    pollTimer: null,
    mode: localStorage.getItem("rag_mode") || "answer",
  };

  const STATUS_TEXT = {
    pending: "排队中", parsing: "解析中", embedding: "向量化", ready: "就绪", error: "失败",
  };

  // ---------- 基础工具 ----------
  async function api(path, options = {}) {
    const opts = { headers: {}, ...options };
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const token = localStorage.getItem("rag_token");
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    const res = await fetch(path, opts);
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      location.replace("/login");
      throw new Error("登录已失效，请重新登录");
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        detail = data.detail || data.message || JSON.stringify(data);
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    const type = res.headers.get("content-type") || "";
    return type.includes("application/json") ? res.json() : res.text();
  }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function md(text) {
    let h = esc(text);
    h = h.replace(/```([\s\S]*?)```/g, (_, code) => `<pre class="code">${code.trim()}</pre>`);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    h = h.replace(/^#{1,6}\s+(.+)$/gm, "<b>$1</b>");
    h = h.replace(/^\s*[-*]\s+(.+)$/gm, "• $1");
    h = h.replace(/\[(\d{1,2})\]/g, '<span class="cite">[$1]</span>');
    return h;
  }

  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = message;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; }, 3200);
    setTimeout(() => el.remove(), 3800);
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtSize(bytes) {
    if (!bytes) return "-";
    if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
    if (bytes > 1024) return (bytes / 1024).toFixed(0) + " KB";
    return bytes + " B";
  }

  // ---------- 视图切换 ----------
  function setView(view) {
    state.view = view;
    $$(".view").forEach((el) => el.classList.toggle("active", el.id === "view-" + view));
    $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
    if (view === "docs") loadDocuments();
    if (view === "graph") loadGraph();
    if (view === "settings") loadConfig();
  }

  $$(".nav-item").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));

  function setMode(mode, persist = true) {
    state.mode = mode;
    if (persist) localStorage.setItem("rag_mode", mode);
    $$("#mode-switch button").forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
    $("#chat-input").placeholder = mode === "retrieve"
      ? "输入问题，直接返回命中的原文片段"
      : "输入问题，Enter 发送，Shift+Enter 换行";
  }
  $$("#mode-switch button").forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.mode)));
  setMode(state.mode);
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    if (e.key === "1") setView("chat");
    if (e.key === "2") setView("docs");
    if (e.key === "3") setView("graph");
    if (e.key === "4") setView("settings");
  });

  // ---------- 概览 ----------
  async function loadStats() {
    try {
      const data = await api("/api/stats");
      $("#stat-docs").textContent = data.documents_ready ?? data.documents;
      $("#stat-chunks").textContent = data.chunks;
      $("#stat-vectors").textContent = data.chunks_vectorized;
    } catch (_) { /* 忽略 */ }
  }

  async function loadConfig() {
    try {
      state.config = await api("/api/config");
      fillConfigForm(state.config);
      renderStatus();
      $("#cfg-banner").classList.toggle("hidden", !!state.config.embed_ready);
      $("#llm-banner").classList.toggle("hidden", !(state.config.embed_ready && !state.config.llm_ready));
      if (state.config.embed_ready && !state.config.llm_ready && state.mode === "answer") {
        setMode("retrieve", false);
      }
    } catch (err) {
      toast("读取配置失败：" + err.message, "err");
    }
  }

  function renderStatus() {
    const cfg = state.config;
    const box = $("#side-status");
    if (!cfg) { box.innerHTML = ""; return; }
    const row = (label, ok, text) =>
      `<div class="status-row"><span class="dot ${ok ? "ok" : "err"}"></span><span>${label}：${esc(text)}</span></div>`;
    box.innerHTML =
      row("向量", cfg.embed_ready && cfg.embed_dim > 0, cfg.embed_ready ? (cfg.embed_model + (cfg.embed_dim ? ` (${cfg.embed_dim}d)` : "")) : "未配置") +
      row("对话", cfg.llm_ready, cfg.llm_ready ? cfg.llm_model : "未配置");
  }

  // ---------- 对话 ----------
  const messagesEl = $("#messages");

  function addMessage(role, content, sources = []) {
    const empty = $("#chat-empty");
    if (empty) empty.remove();
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    wrap.innerHTML = `<div class="role">${role === "user" ? "你" : "知识库"}</div>`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = role === "user" ? esc(content) : md(content);
    wrap.appendChild(bubble);
    if (sources.length) wrap.appendChild(renderSources(sources));
    messagesEl.appendChild(wrap);
    scrollBottom(true);
    return { wrap, bubble };
  }

  function renderSources(sources, answerText = "") {
    const cited = new Set();
    (answerText.match(/\[(\d{1,2})\]/g) || []).forEach((m) => cited.add(Number(m.slice(1, -1))));
    const row = document.createElement("div");
    row.className = "sources";
    sources.forEach((src) => {
      const btn = document.createElement("button");
      btn.className = "source-chip" + (cited.size && !cited.has(src.index) ? " muted" : "");
      const meta = [src.doc_title, src.page ? `第 ${src.page} 页` : "", src.heading || ""]
        .filter(Boolean).join(" · ");
      btn.innerHTML = `<b>[${src.index}]</b><span>${esc(meta)}</span>`;
      btn.addEventListener("click", () => showSource(src));
      row.appendChild(btn);
    });
    return row;
  }

  function showSource(src) {
    $("#modal-title").textContent = `[${src.index}] ${src.doc_title}`;
    const metaParts = [];
    if (src.page) metaParts.push(`第 ${src.page} 页`);
    if (src.heading) metaParts.push(src.heading);
    metaParts.push("相关度 " + Number(src.score).toFixed(3));
    $("#modal-body").innerHTML = `<div class="meta">${esc(metaParts.join(" · "))}</div>` + esc(src.preview);
    $("#modal").classList.remove("hidden");
  }

  function scrollBottom(force = false) {
    const gap = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
    if (force || gap < 160) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderHits(hits) {
    const box = document.createElement("div");
    box.className = "results";
    if (!hits || !hits.length) {
      box.innerHTML = '<div class="result-empty">没有检索到相关片段。换个说法，或先上传相关文档。</div>';
      return box;
    }
    hits.forEach((hit, i) => {
      const card = document.createElement("div");
      card.className = "result-card";
      const leaf = (hit.heading || "").split(">").pop().trim();
      const meta = [i + 1, hit.doc_title, hit.page ? `第 ${hit.page} 页` : "", leaf].filter(Boolean).join(" · ");
      card.innerHTML = `<div class="result-meta">${esc(meta)}</div><div class="result-text">${esc(hit.text)}</div>`;
      if (hit.text.length > 260) {
        const more = document.createElement("button");
        more.className = "link small";
        more.textContent = "展开全文";
        more.addEventListener("click", () => {
          const expanded = card.classList.toggle("expanded");
          more.textContent = expanded ? "收起" : "展开全文";
        });
        card.appendChild(more);
      }
      box.appendChild(card);
    });
    return box;
  }

  async function sendChat(text, modeOverride) {
    if (!text.trim() || state.sending) return;
    if (!state.config?.embed_ready) {
      toast("请先在设置里配置向量模型", "err");
      setView("settings");
      return;
    }
    const mode = modeOverride || ((state.mode === "retrieve" || !state.config?.llm_ready) ? "retrieve" : "answer");
    if (state.mode === "answer" && !state.config?.llm_ready && !modeOverride) {
      toast("未配置对话模型，本次直接返回检索片段", "");
    }
    state.sending = true;
    $("#chat-send").disabled = true;
    $("#chat-input").value = "";
    autoGrow($("#chat-input"));
    addMessage("user", text);

    let wrap, bubble;
    if (mode === "retrieve") {
      const empty = $("#chat-empty");
      if (empty) empty.remove();
      wrap = document.createElement("div");
      wrap.className = "msg assistant";
      wrap.innerHTML = '<div class="role">知识库</div>';
      bubble = document.createElement("div");
      bubble.className = "bubble typing";
      bubble.textContent = "检索中…";
      wrap.appendChild(bubble);
      messagesEl.appendChild(wrap);
      scrollBottom(true);
    } else {
      ({ wrap, bubble } = addMessage("assistant", ""));
      bubble.classList.add("typing");
    }

    let full = "";
    let sources = [];
    let hits = [];
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: text,
          history: mode === "retrieve" ? [] : state.messages.slice(-6),
          doc_ids: state.scope.size ? Array.from(state.scope) : null,
          stream: true,
          mode,
        }),
      });
      if (!res.ok || !res.body) {
        const detail = await res.text();
        throw new Error(detail.slice(0, 300) || res.statusText);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let raf = 0;
      const paint = () => {
        bubble.innerHTML = md(full) || "…";
        scrollBottom();
      };
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          let event;
          try { event = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
          if (event.type === "meta") {
            sources = event.sources || [];
            hits = event.hits || [];
            if (event.rewritten) bubble.dataset.rewritten = event.search_query;
          } else if (event.type === "delta") {
            full += event.text;
            if (!raf) raf = requestAnimationFrame(() => { raf = 0; paint(); });
          } else if (event.type === "error") {
            throw new Error(event.message);
          }
        }
      }
      if (raf) cancelAnimationFrame(raf);
      bubble.classList.remove("typing");
      if (mode === "retrieve") {
        bubble.remove();
        wrap.appendChild(renderHits(hits));
        state.messages.push({ role: "user", content: text });
      } else {
        bubble.innerHTML = md(full || "（没有返回内容）");
        if (sources.length) wrap.appendChild(renderSources(sources, full));
        state.messages.push({ role: "user", content: text });
        state.messages.push({ role: "assistant", content: full });
      }
      scrollBottom();
    } catch (err) {
      bubble.classList.remove("typing");
      bubble.innerHTML = `<span class="err-text">失败：${esc(err.message)}</span>`;
      if (mode === "answer") {
        const retry = document.createElement("button");
        retry.className = "ghost small";
        retry.textContent = "改用仅检索查看原文";
        retry.addEventListener("click", () => {
          wrap.remove();
          sendChat(text, "retrieve");
        });
        wrap.appendChild(retry);
      }
    } finally {
      state.sending = false;
      $("#chat-send").disabled = false;
      $("#chat-input").focus();
    }
  }

  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 180) + "px";
  }

  $("#chat-input").addEventListener("input", (e) => autoGrow(e.target));
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat(e.target.value);
    }
  });
  $("#chat-send").addEventListener("click", () => sendChat($("#chat-input").value));
  $$(".samples button").forEach((btn) => btn.addEventListener("click", () => sendChat(btn.textContent)));
  $("#chat-new").addEventListener("click", () => {
    state.messages = [];
    messagesEl.innerHTML = `<div class="empty" id="chat-empty">
      <h3>向你的资料提问</h3>
      <p>已入库文档会先被检索，再由模型基于原文回答。</p>
      <div class="samples"><button>这份文档的主要结论是什么？</button><button>总结一下有哪些关键流程？</button></div>
    </div>`;
    $$(".samples button").forEach((btn) => btn.addEventListener("click", () => sendChat(btn.textContent)));
  });

  // 检索范围
  function renderScope() {
    const list = $("#scope-list");
    const ready = state.docs.filter((d) => d.status === "ready");
    list.innerHTML = ready.length
      ? ready.map((d) => `<label><input type="checkbox" value="${d.id}" ${state.scope.has(d.id) ? "checked" : ""}><span title="${esc(d.title)}">${esc(d.title)}</span></label>`).join("")
      : '<p class="card-hint" style="padding:4px 6px;margin:0">暂无可用文档</p>';
    list.querySelectorAll("input").forEach((input) => {
      input.addEventListener("change", () => {
        const id = Number(input.value);
        input.checked ? state.scope.add(id) : state.scope.delete(id);
        updateScopeLabel();
      });
    });
    updateScopeLabel();
  }

  function updateScopeLabel() {
    const label = $("#scope-label");
    if (!state.scope.size) label.textContent = "范围：全部文档";
    else if (state.scope.size === 1) {
      const doc = state.docs.find((d) => d.id === [...state.scope][0]);
      label.textContent = "范围：" + (doc ? doc.title : "1 个文档");
    } else label.textContent = `范围：已选 ${state.scope.size} 个`;
  }

  $("#scope-all").addEventListener("click", () => {
    state.docs.filter((d) => d.status === "ready").forEach((d) => state.scope.add(d.id));
    renderScope();
  });
  $("#scope-none").addEventListener("click", () => { state.scope.clear(); renderScope(); });
  document.addEventListener("click", (e) => {
    const scope = document.querySelector(".scope");
    if (scope && scope.open && !scope.contains(e.target)) scope.open = false;
  });

  // ---------- 文档 ----------
  async function loadDocuments() {
    try {
      const data = await api("/api/documents");
      state.docs = data.documents;
      renderDocuments();
      renderScope();
      schedulePolling();
    } catch (err) {
      toast("读取文档失败：" + err.message, "err");
    }
  }

  function docType(doc) {
    const name = doc.filename || doc.source || "";
    const ext = name.includes(".") ? name.split(".").pop().toUpperCase() : "URL";
    return ext.slice(0, 6);
  }

  function statusChip(doc) {
    const label = STATUS_TEXT[doc.status] || doc.status;
    const cls = doc.status === "ready" ? "ready" : doc.status === "error" ? "error" : "working";
    const bar = doc.status === "embedding"
      ? `<span class="bar"><i style="width:${Math.round((doc.progress || 0) * 100)}%"></i></span>`
      : "";
    const title = doc.error ? ` title="${esc(doc.error)}"` : "";
    return `<span class="chip ${cls}"${title}>${label}</span>${bar}`;
  }

  function renderDocuments() {
    const tbody = $("#doc-rows");
    $("#docs-empty").classList.toggle("hidden", state.docs.length > 0);
    tbody.innerHTML = state.docs.map((doc) => {
      const name = doc.filename || doc.title;
      return `<tr data-id="${doc.id}">
        <td class="title"><span class="name" title="${esc(name)}">${esc(doc.title || name)}</span>
          <span class="path">${esc(doc.kind === "url" ? doc.source : fmtSize(doc.size))}</span></td>
        <td>${docType(doc)}</td>
        <td>${statusChip(doc)}</td>
        <td>${doc.num_chunks || 0}</td>
        <td>${fmtTime(doc.created_at)}</td>
        <td class="actions">
          <button class="ghost" data-act="chunks">片段</button>
          <button class="ghost" data-act="reindex">重新解析</button>
          <button class="ghost" data-act="delete">删除</button>
        </td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.closest("tr").dataset.id);
        const act = btn.dataset.act;
        if (act === "chunks") showChunks(id);
        if (act === "reindex") reindexDoc(id);
        if (act === "delete") deleteDoc(id);
      });
    });
  }

  async function showChunks(docId) {
    try {
      const data = await api(`/api/documents/${docId}/chunks?limit=10`);
      const rows = data.chunks.map((c) =>
        `<div style="margin-bottom:14px">
           <div class="meta">#${c.seq + 1}${c.page ? " · 第 " + c.page + " 页" : ""}${c.heading ? " · " + esc(c.heading) : ""} · ${c.char_len} 字</div>
           <div>${esc(c.text)}</div>
         </div>`).join("");
      $("#modal-title").textContent = `文档片段（共 ${data.total} 个，显示前 ${data.chunks.length} 个）`;
      $("#modal-body").innerHTML = rows || "没有片段";
      $("#modal").classList.remove("hidden");
    } catch (err) {
      toast("读取片段失败：" + err.message, "err");
    }
  }

  async function reindexDoc(docId) {
    try {
      await api(`/api/documents/${docId}/reindex`, { method: "POST" });
      toast("已加入重新解析队列", "ok");
      loadDocuments();
    } catch (err) {
      toast("操作失败：" + err.message, "err");
    }
  }

  async function deleteDoc(docId) {
    const doc = state.docs.find((d) => d.id === docId);
    if (!confirm(`确定删除《${doc ? doc.title : docId}》及其全部片段？`)) return;
    try {
      await api(`/api/documents/${docId}`, { method: "DELETE" });
      toast("已删除", "ok");
      loadDocuments();
      loadStats();
    } catch (err) {
      toast("删除失败：" + err.message, "err");
    }
  }

  // 上传
  const dropzone = $("#dropzone");
  $("#pick-file").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => uploadFiles(e.target.files));
  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files?.length) uploadFiles(e.dataTransfer.files);
  });

  async function uploadFiles(fileList) {
    if (!fileList || !fileList.length) return;
    const form = new FormData();
    Array.from(fileList).forEach((f) => form.append("files", f));
    toast(`正在上传 ${fileList.length} 个文件…`);
    try {
      const data = await api("/api/documents", { method: "POST", body: form });
      const failed = data.results.filter((r) => !r.ok);
      const dup = data.results.filter((r) => r.duplicate);
      if (failed.length) toast(failed.map((r) => r.name + "：" + r.error).join("；"), "err");
      if (dup.length) toast(`${dup.length} 个文件内容重复，已跳过`, "");
      if (data.results.some((r) => r.ok && !r.duplicate)) toast("已加入解析队列", "ok");
      loadDocuments();
      loadStats();
    } catch (err) {
      toast("上传失败：" + err.message, "err");
    } finally {
      $("#file-input").value = "";
    }
  }

  $("#url-add").addEventListener("click", async () => {
    const url = $("#url-input").value.trim();
    if (!url) return;
    try {
      await api("/api/documents/url", { method: "POST", body: { url } });
      $("#url-input").value = "";
      toast("已开始抓取网页", "ok");
      loadDocuments();
    } catch (err) {
      toast("抓取失败：" + err.message, "err");
    }
  });

  $("#docs-refresh").addEventListener("click", () => { loadDocuments(); loadStats(); });
  $("#reembed-all").addEventListener("click", async () => {
    if (!confirm("将用当前向量模型重新计算所有片段向量，耗时取决于文档量，继续？")) return;
    try {
      const data = await api("/api/reindex", { method: "POST", body: { scope: "embeddings" } });
      toast(`已排入 ${data.queued} 个文档的向量重建`, "ok");
      loadDocuments();
    } catch (err) {
      toast("操作失败：" + err.message, "err");
    }
  });

  function schedulePolling() {
    const busy = state.docs.some((d) => !["ready", "error"].includes(d.status));
    if (busy && !state.pollTimer) {
      state.pollTimer = setInterval(async () => {
        await loadDocuments();
        await loadStats();
        if (!state.docs.some((d) => !["ready", "error"].includes(d.status))) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          loadStats();
        }
      }, 2500);
    } else if (!busy && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }


  // ---------- 关系图谱 ----------
  const graphState = {
    nodes: [], links: [], scale: 1, ox: 0, oy: 0,
    selected: null, hover: null, alpha: 1, raf: null, drag: null, pan: null, fitted: false,
    ctxNode: null,
  };
  const GRAPH_COLORS = {
    "文档": "#2f6bff", "概念": "#8b5cf6", "系统": "#0ea5e9", "指标": "#14875a",
    "流程": "#b45309", "人物": "#db2777", "组织": "#0891b2", "技术": "#7c3aed",
  };

  function graphColor(type) {
    return GRAPH_COLORS[type] || "#8b5cf6";
  }

  function renderGraphStats(stats) {
    const items = [
      ["实体", stats.entities || 0],
      ["关系", stats.relations || 0],
      ["已建图文档", stats.linked_docs || 0],
      ["当前节点", stats.shown_nodes || 0],
    ];
    $("#graph-stats").innerHTML = items.map((it) =>
      '<span class="gstat"><b>' + it[1] + "</b>" + it[0] + "</span>").join("");
  }

  async function loadGraph() {
    // 不再按文档筛选：一次看全库，实体本来就跨文档共用同一个节点
    try {
      const data = await api("/api/graph");
      renderGraphStats(data.stats || {});
      $("#graph-empty").classList.toggle("hidden", (data.nodes || []).length > 0);
      setGraphData(data.nodes || [], data.edges || []);
    } catch (err) {
      toast("图谱加载失败：" + err.message, "err");
    }
  }

  function setGraphData(nodes, edges) {
    const count = Math.max(nodes.length, 1);
    const radius = Math.max(130, Math.min(340, 42 * Math.sqrt(count)));
    nodes.forEach((n, i) => {
      const angle = (i / count) * Math.PI * 2;
      const ring = n.kind === "doc" ? radius * 0.55 : radius;
      n.x = Math.cos(angle) * ring + (Math.random() - 0.5) * 24;
      n.y = Math.sin(angle) * ring + (Math.random() - 0.5) * 24;
      n.vx = 0; n.vy = 0;
      n.radius = n.kind === "doc"
        ? 9 + Math.min(5, Math.sqrt(n.size || 1) * 1.6)
        : 5 + Math.min(7, Math.sqrt(n.size || 1));
      n.color = n.kind === "doc" ? GRAPH_COLORS["文档"] : graphColor(n.type);
    });
    const byId = {};
    nodes.forEach((n) => { byId[n.id] = n; });
    const links = [];
    edges.forEach((e) => {
      const s = byId[e.source], t = byId[e.target];
      if (s && t) links.push({ s: s, t: t, rel: e.rel, kind: e.kind });
    });
    graphState.nodes = nodes;
    graphState.links = links;
    graphState.selected = null;
    graphState.hover = null;
    graphState.scale = 1;
    graphState.ox = 0;
    graphState.oy = 0;
    graphState.alpha = 1;
    graphState.fitted = false;
    $("#graph-panel").innerHTML = '<p class="muted">点击节点查看详情</p>';
    startGraphLoop();
  }

  function startGraphLoop() {
    if (graphState.raf) cancelAnimationFrame(graphState.raf);
    const step = () => {
      if (state.view !== "graph") { graphState.raf = null; return; }
      if (graphState.alpha > 0.02 && !graphState.pan) {
        simulateGraph();
      } else if (!graphState.fitted) {
        fitGraph();
        graphState.fitted = true;
      }
      drawGraph();
      graphState.raf = requestAnimationFrame(step);
    };
    graphState.raf = requestAnimationFrame(step);
  }

  function simulateGraph() {
    const nodes = graphState.nodes, links = graphState.links, alpha = graphState.alpha;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
        const d = Math.sqrt(d2);
        const weight = (a.kind === "doc" || b.kind === "doc") ? 2.2 : 1;
        const force = 2600 * alpha * weight / d2;
        const fx = (dx / d) * force, fy = (dy / d) * force;
        a.vx -= fx; a.vy -= fy;
        b.vx += fx; b.vy += fy;
      }
    }
    links.forEach((l) => {
      const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
      const d = Math.max(1, Math.hypot(dx, dy));
      const ideal = l.kind === "mention" ? 84 : 118;
      const force = (d - ideal) * 0.03 * alpha;
      const fx = (dx / d) * force, fy = (dy / d) * force;
      l.s.vx += fx; l.s.vy += fy;
      l.t.vx -= fx; l.t.vy -= fy;
    });
    nodes.forEach((n) => {
      n.vx -= n.x * 0.0025 * alpha;
      n.vy -= n.y * 0.0025 * alpha;
      if (n === graphState.drag) { n.vx = 0; n.vy = 0; return; }
      n.vx *= 0.86; n.vy *= 0.86;
      n.x += n.vx; n.y += n.vy;
    });
    graphState.alpha *= 0.975;
  }

  function fitGraph() {
    const canvas = $("#graph-canvas");
    if (!canvas || !graphState.nodes.length) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    graphState.nodes.forEach((n) => {
      minX = Math.min(minX, n.x - n.radius);
      minY = Math.min(minY, n.y - n.radius);
      maxX = Math.max(maxX, n.x + n.radius);
      maxY = Math.max(maxY, n.y + n.radius);
    });
    const spanX = Math.max(maxX - minX, 1);
    const spanY = Math.max(maxY - minY, 1);
    const pad = 90;
    const scale = Math.min(1.7, (rect.width - pad * 2) / spanX, (rect.height - pad * 2) / spanY);
    graphState.scale = Math.max(0.35, scale);
    graphState.ox = -((minX + maxX) / 2) * graphState.scale - 70;
    graphState.oy = -((minY + maxY) / 2) * graphState.scale;
  }

  function graphSurface() {
    const canvas = $("#graph-canvas");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, Math.floor(rect.width * dpr));
    const height = Math.max(1, Math.floor(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: rect.width, h: rect.height };
  }

  function toScreen(node, w, h) {
    return {
      x: node.x * graphState.scale + w / 2 + graphState.ox,
      y: node.y * graphState.scale + h / 2 + graphState.oy,
    };
  }

  function drawGraph() {
    const surface = graphSurface();
    const ctx = surface.ctx, w = surface.w, h = surface.h;
    const css = getComputedStyle(document.documentElement);
    const line = (css.getPropertyValue("--border") || "#e3e6ea").trim();
    const text = (css.getPropertyValue("--text") || "#16191d").trim();
    const muted = (css.getPropertyValue("--muted") || "#6b7280").trim();
    ctx.clearRect(0, 0, w, h);
    if (!graphState.nodes.length) return;

    const pos = {};
    graphState.nodes.forEach((n) => { pos[n.id] = toScreen(n, w, h); });

    ctx.lineWidth = 1;
    graphState.links.forEach((l) => {
      const a = pos[l.s.id], b = pos[l.t.id];
      if (!a || !b) return;
      const active = graphState.selected &&
        (l.s.id === graphState.selected.id || l.t.id === graphState.selected.id);
      ctx.beginPath();
      ctx.setLineDash(l.kind === "cooccur" ? [4, 4] : []);
      ctx.strokeStyle = active ? l.s.color || line : line;
      ctx.globalAlpha = active ? 0.9 : 0.55;
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    graphState.nodes.forEach((n) => {
      const p = pos[n.id];
      const isSel = graphState.selected && graphState.selected.id === n.id;
      const isHover = graphState.hover && graphState.hover.id === n.id;
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.radius + (isSel ? 3 : isHover ? 2 : 0), 0, Math.PI * 2);
      ctx.fillStyle = n.color;
      ctx.globalAlpha = isSel || isHover ? 1 : 0.88;
      ctx.fill();
      if (isSel) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = text;
        ctx.globalAlpha = 0.9;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    });

    // 标签单独一遍：文档优先，放不下的直接跳过，避免互相压字
    const showLabels = graphState.scale > 1.15 || graphState.nodes.length <= 30;
    const priority = (n) => (n.kind === "doc" ? 2 : 0)
      + (graphState.selected && graphState.selected.id === n.id ? 3 : 0)
      + (graphState.hover && graphState.hover.id === n.id ? 2 : 0);
    const placed = [];
    graphState.nodes.slice()
      .filter((n) => n.kind === "doc" || showLabels
        || (graphState.selected && graphState.selected.id === n.id)
        || (graphState.hover && graphState.hover.id === n.id))
      .sort((a, b) => priority(b) - priority(a))
      .forEach((n) => {
        const p = pos[n.id];
        if (!p) return;
        const label = n.label.length > 16 ? n.label.slice(0, 16) + "…" : n.label;
        ctx.font = (n.kind === "doc" ? "600 12px" : "11px") + " system-ui, sans-serif";
        const width = ctx.measureText(label).width;
        const hits = (box) => placed.some((r) => box.x < r.x + r.w && box.x + box.w > r.x
          && box.y < r.y + r.h && box.y + box.h > r.y);
        let box = { x: p.x - width / 2 - 3, y: p.y + n.radius + 4, w: width + 6, h: 16 };
        let top = p.y + n.radius + 6;
        if (hits(box)) {
          // 下方被占了就翻到节点上方再试一次
          box = { x: box.x, y: p.y - n.radius - 20, w: box.w, h: box.h };
          top = p.y - n.radius - 18;
          if (hits(box)) return;
        }
        placed.push(box);
        ctx.fillStyle = n.kind === "doc" ? text : muted;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(label, p.x, top);
      });
  }

  function watchRebuild(total) {
    // 重建是后台逐篇跑的，这里只轮统计数字，跑完再重排一次图，避免每轮都抖
    return new Promise((resolve) => {
      let last = -1, idle = 0, rounds = 0;
      const timer = setInterval(async () => {
        if (state.view !== "graph") { clearInterval(timer); resolve(); return; }
        rounds += 1;
        try {
          const data = await api("/api/graph");
          renderGraphStats(data.stats || {});
          const done = (data.stats || {}).linked_docs || 0;
          if (done > last) { last = done; idle = 0; } else { idle += 1; }
          if ((total && done >= total) || idle >= 4 || rounds > 150) {
            clearInterval(timer);
            await loadGraph();
            resolve();
          }
        } catch (_) {
          clearInterval(timer);
          resolve();
        }
      }, 6000);
    });
  }

  function graphPoint(evt) {
    const canvas = $("#graph-canvas");
    const rect = canvas.getBoundingClientRect();
    return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
  }

  function graphPick(point) {
    const canvas = $("#graph-canvas");
    const rect = canvas.getBoundingClientRect();
    let best = null, bestDist = Infinity;
    graphState.nodes.forEach((n) => {
      const p = toScreen(n, rect.width, rect.height);
      const d = Math.hypot(p.x - point.x, p.y - point.y);
      if (d < n.radius + 8 && d < bestDist) { best = n; bestDist = d; }
    });
    return best;
  }

  async function loadRelatedDocs(docId) {
    const box = $("#gp-related");
    if (!box) return;
    try {
      const data = await api("/api/documents/" + docId + "/related?limit=8");
      const list = data.related || [];
      if (!list.length) {
        box.innerHTML = '<div class="gp-sub">关联文档</div><p class="muted">暂无关联</p>';
        return;
      }
      box.innerHTML = '<div class="gp-sub">关联文档</div>' + list.map((r) => {
        const note = r.shared.length ? "共同实体：" + r.shared.join("、")
          : (r.relations[0] ? r.relations[0].rel + "：" + r.relations[0].via + " → " + r.relations[0].to : "");
        return '<button class="gp-link" data-doc="' + r.doc_id + '" data-title="' + esc(r.title) + '"><b>' + esc(r.title) +
          "</b><span>" + esc(note) + "</span></button>";
      }).join("");
    } catch (_) {
      box.innerHTML = "";
    }
  }

  function showGraphNode(node) {
    const panel = $("#graph-panel");
    if (!node) {
      panel.innerHTML = '<p class="muted">点击节点查看详情</p>';
      return;
    }
    if (node.kind === "doc") {
      const ents = graphState.links
        .filter((l) => l.s.id === node.id && l.kind === "mention")
        .map((l) => l.t.label);
      panel.innerHTML = "<h4>" + esc(node.label) + "</h4>" +
        '<p class="muted">文档 · ' + ents.length + " 个实体</p>" +
        '<div class="gp-tags">' + ents.slice(0, 24).map((t) => '<span class="chip">' + esc(t) + "</span>").join("") + "</div>" +
        '<div id="gp-related"><p class="muted">加载关联文档…</p></div>';
      loadRelatedDocs(node.ref);
      return;
    }
    const docs = graphState.links
      .filter((l) => l.t.id === node.id && l.kind === "mention")
      .map((l) => l.s.label);
    const rels = graphState.links
      .filter((l) => l.kind === "relation" && (l.s.id === node.id || l.t.id === node.id))
      .map((l) => l.s.id === node.id
        ? node.label + " —" + l.rel + "→ " + l.t.label
        : l.s.label + " —" + l.rel + "→ " + node.label);
    panel.innerHTML = "<h4>" + esc(node.label) + "</h4>" +
      '<p class="muted">' + esc(node.type || "实体") + " · 出现在 " + docs.length + " 篇文档</p>" +
      (docs.length ? '<div class="gp-sub">所属文档</div><div class="gp-tags">' +
        docs.slice(0, 12).map((t) => '<span class="chip">' + esc(t) + "</span>").join("") + "</div>" : "") +
      (rels.length ? '<div class="gp-sub">关系</div><ul class="gp-list">' +
        rels.slice(0, 12).map((t) => "<li>" + esc(t) + "</li>").join("") + "</ul>" : "");
  }

  async function openGraphContext(node, page = 1) {
    graphState.ctxNode = node;
    $("#modal-title").textContent = node.label + (node.kind === "doc" ? " · 文档片段" : " · 相关内容");
    $("#modal-body").innerHTML = '<p class="muted">加载中…</p>';
    $("#modal").classList.remove("hidden");
    try {
      const kind = node.kind === "doc" ? "doc" : "entity";
      const data = await api("/api/graph/context?kind=" + kind + "&ref=" + node.ref +
        "&page=" + page + "&size=5");
      renderGraphContext(data);
    } catch (err) {
      $("#modal-body").innerHTML = '<p class="test-result err">' + esc(err.message) + "</p>";
    }
  }

  function renderGraphContext(data) {
    const body = $("#modal-body");
    if (!data.items || !data.items.length) {
      body.textContent = "没有找到包含「" + data.title + "」的正文片段。";
      return;
    }
    const items = data.items.map((it) => {
      const leaf = String(it.heading || "").split(">").pop().trim();
      const meta = [it.doc_title, it.page ? "第 " + it.page + " 页" : "", leaf].filter(Boolean).join(" · ");
      return '<div class="ctx-item"><div class="ctx-head">' + esc(meta) +
        '</div><div class="ctx-text">' + esc(it.text) + "</div></div>";
    }).join("");
    const pager = '<div class="pager">' +
      '<button class="ghost" data-page="' + (data.page - 1) + '"' + (data.page <= 1 ? " disabled" : "") + ">上一页</button>" +
      '<span class="muted">第 ' + data.page + " / " + data.pages + " 页 · 共 " + data.total + " 段</span>" +
      '<button class="ghost" data-page="' + (data.page + 1) + '"' + (data.page >= data.pages ? " disabled" : "") + ">下一页</button>" +
      "</div>";
    body.innerHTML = items + pager;
  }

  function graphZoomAt(factor, point) {
    const canvas = $("#graph-canvas");
    const rect = canvas.getBoundingClientRect();
    const next = Math.max(0.3, Math.min(3.2, graphState.scale * factor));
    // 保持鼠标所指的那一点在缩放前后位置不变
    const wx = (point.x - rect.width / 2 - graphState.ox) / graphState.scale;
    const wy = (point.y - rect.height / 2 - graphState.oy) / graphState.scale;
    graphState.scale = next;
    graphState.ox = point.x - rect.width / 2 - wx * next;
    graphState.oy = point.y - rect.height / 2 - wy * next;
  }

  (function bindGraph() {
    const canvas = $("#graph-canvas");
    if (!canvas) return;
    canvas.addEventListener("mousedown", (e) => {
      const point = graphPoint(e);
      const node = graphPick(point);
      if (node) {
        graphState.drag = node;
        graphState.selected = node;
        showGraphNode(node);
      } else {
        graphState.pan = { x: e.clientX, y: e.clientY, ox: graphState.ox, oy: graphState.oy };
      }
    });
    canvas.addEventListener("mousemove", (e) => {
      const point = graphPoint(e);
      if (graphState.drag) {
        const rect = canvas.getBoundingClientRect();
        graphState.drag.x = (point.x - rect.width / 2 - graphState.ox) / graphState.scale;
        graphState.drag.y = (point.y - rect.height / 2 - graphState.oy) / graphState.scale;
        graphState.alpha = Math.max(graphState.alpha, 0.25);
        return;
      }
      if (graphState.pan) {
        graphState.ox = graphState.pan.ox + (e.clientX - graphState.pan.x);
        graphState.oy = graphState.pan.oy + (e.clientY - graphState.pan.y);
        return;
      }
      graphState.hover = graphPick(point);
      canvas.style.cursor = graphState.hover ? "pointer" : "grab";
    });
    const release = () => { graphState.drag = null; graphState.pan = null; };
    canvas.addEventListener("mouseup", release);
    canvas.addEventListener("mouseleave", release);
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      graphZoomAt(e.deltaY < 0 ? 1.12 : 0.89, graphPoint(e));
    }, { passive: false });
    canvas.addEventListener("dblclick", (e) => {
      const node = graphPick(graphPoint(e));
      if (node) openGraphContext(node);
    });

    $("#graph-refresh").addEventListener("click", () => loadGraph());
    $("#graph-rebuild").addEventListener("click", async () => {
      if (!confirm("会用对话模型重新抽取全部文档的实体与关系，耗时较长，确定继续？")) return;
      const btn = $("#graph-rebuild");
      btn.disabled = true;
      btn.textContent = "重建中…";
      try {
        const data = await api("/api/graph/rebuild", { method: "POST", body: {} });
        const total = data.queued || 0;
        if (data.probe && data.probe.status !== "ok") {
          toast("第一篇没有抽出实体（" + data.probe.status + "），检查对话模型", "err");
        }
        toast("已排队 " + total + " 篇，抽取中…", "ok");
        await watchRebuild(total);
        toast("图谱重建完成", "ok");
      } catch (err) {
        toast("重建失败：" + err.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "重建图谱";
      }
    });
    $("#graph-panel").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-doc]");
      if (!btn) return;
      openGraphContext({ kind: "doc", ref: Number(btn.dataset.doc), label: btn.dataset.title || "文档" });
    });
    $("#modal-body").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-page]");
      if (!btn || !graphState.ctxNode) return;
      openGraphContext(graphState.ctxNode, Number(btn.dataset.page));
    });
    window.addEventListener("resize", () => { if (state.view === "graph") drawGraph(); });
  })();

  // ---------- 设置 ----------
  const CONFIG_FIELDS = [
    "embed_base_url", "embed_model", "llm_base_url", "llm_model",
    "chunk_size", "chunk_overlap", "top_k_vector", "top_k_keyword",
    "final_k", "max_context_chars",
  ];

  function fillConfigForm(cfg) {
    CONFIG_FIELDS.forEach((key) => {
      const el = document.getElementById(key);
      if (el && cfg[key] !== undefined && cfg[key] !== null) el.value = cfg[key];
    });
    const embedKey = $("#embed_api_key");
    embedKey.value = "";
    embedKey.placeholder = cfg.embed_api_key_set ? "已设置，留空则不修改" : "sk-...";
    const llmKey = $("#llm_api_key");
    llmKey.value = "";
    llmKey.placeholder = cfg.llm_api_key_set ? "已设置，留空则不修改" : "...";
    $("#query_rewrite").checked = !!cfg.query_rewrite;
    $("#graph_enabled").checked = cfg.graph_enabled !== false;
    $("#graph_expand").checked = cfg.graph_expand !== false;
    const hasToken = !!cfg.api_token_set;
    $("#api_token_toggle").checked = hasToken;
    $("#api_token_wrap").classList.toggle("hidden", !hasToken);
    $("#api_token").value = "";
    $("#api_token").placeholder = hasToken ? "已设置，留空则不修改" : "Bearer Token";

    const embedState = $("#embed-state");
    embedState.textContent = cfg.embed_ready ? (cfg.embed_dim ? `可用 · ${cfg.embed_dim} 维` : "已配置") : "未配置";
    embedState.className = "tag " + (cfg.embed_ready ? "ok" : "err");
    const llmState = $("#llm-state");
    llmState.textContent = cfg.llm_ready ? "已配置" : "未配置";
    llmState.className = "tag " + (cfg.llm_ready ? "ok" : "err");

    const summary = { ...cfg };
    ["llm_api_key", "embed_api_key", "api_token"].forEach((k) => delete summary[k]);
    $("#cfg-summary").textContent = JSON.stringify(summary, null, 2);
  }

  $("#cfg-save").addEventListener("click", async () => {
    const patch = {};
    CONFIG_FIELDS.forEach((key) => {
      const el = document.getElementById(key);
      if (!el) return;
      const raw = el.value;
      patch[key] = el.type === "number" ? Number(raw) : raw;
    });
    const embedKey = $("#embed_api_key").value.trim();
    const llmKey = $("#llm_api_key").value.trim();
    if (embedKey) patch.embed_api_key = embedKey;
    if (llmKey) patch.llm_api_key = llmKey;
    patch.query_rewrite = $("#query_rewrite").checked;
    patch.graph_enabled = $("#graph_enabled").checked;
    patch.graph_expand = $("#graph_expand").checked;
    if ($("#api_token_toggle").checked) {
      const token = $("#api_token").value.trim();
      if (token) patch.api_token = token;
    } else {
      patch.api_token = "";
    }
    try {
      state.config = await api("/api/config", { method: "PUT", body: { patch } });
      fillConfigForm(state.config);
      renderStatus();
      $("#cfg-banner").classList.toggle("hidden", !!state.config.embed_ready);
      $("#llm-banner").classList.toggle("hidden", !(state.config.embed_ready && !state.config.llm_ready));
      toast("配置已保存", "ok");
    } catch (err) {
      toast("保存失败：" + err.message, "err");
    }
  });

  $("#cfg-reload").addEventListener("click", loadConfig);
  $("#api_token_toggle").addEventListener("change", (e) => {
    $("#api_token_wrap").classList.toggle("hidden", !e.target.checked);
  });

  $$("button[data-test]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.test;
      const out = $(target === "embed" ? "#embed-test" : "#llm-test");
      out.textContent = "测试中…";
      out.className = "test-result";
      btn.disabled = true;
      try {
        const data = await api("/api/config/test", { method: "POST", body: { target } });
        out.textContent = data.message || "可用";
        out.className = "test-result ok";
      } catch (err) {
        out.textContent = err.message;
        out.className = "test-result err";
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.addEventListener("click", (e) => {
    const goto = e.target.closest("[data-goto]");
    if (goto) setView(goto.dataset.goto);
  });

  $("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").classList.add("hidden"); });

  // ---------- 账号 ----------
  async function loadAccount() {
    try {
      const data = await api("/api/auth/status");
      $("#account-name").textContent = data.username || "未登录";
    } catch (_) { /* 401 时 api() 已跳转登录页 */ }
  }

  $("#logout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* ignore */ }
    location.replace("/login");
  });

  $("#pwd-save").addEventListener("click", async () => {
    const out = $("#pwd-result");
    const oldPwd = $("#pwd-old").value;
    const newPwd = $("#pwd-new").value;
    const fail = (msg) => { out.textContent = msg; out.className = "test-result err"; };
    if (!oldPwd || !newPwd) return fail("请填写原密码与新密码");
    if (newPwd !== $("#pwd-confirm").value) return fail("两次输入的新密码不一致");
    out.textContent = "提交中…";
    out.className = "test-result";
    try {
      await api("/api/auth/password", { method: "POST", body: { old_password: oldPwd, new_password: newPwd } });
      out.textContent = "密码已更新";
      out.className = "test-result ok";
      ["#pwd-old", "#pwd-new", "#pwd-confirm"].forEach((s) => { $(s).value = ""; });
    } catch (err) {
      fail(err.message);
    }
  });

  // ---------- 备份与迁移 ----------
  $("#backup-export").addEventListener("click", () => {
    window.location.href = "/api/backup/export";
  });

  $("#backup-import").addEventListener("click", () => $("#backup-file").click());

  $("#backup-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const out = $("#backup-result");
    if (!confirm("导入会覆盖当前全部文档、笔记、账号与模型配置，确定继续？")) return;
    out.textContent = "导入中…";
    out.className = "test-result";
    const form = new FormData();
    form.append("file", file);
    try {
      const data = await api("/api/backup/import", { method: "POST", body: form });
      const warn = (data.warnings || []).join("；");
      out.textContent = `已导入 ${data.documents} 篇文档 / ${data.chunks} 个片段${warn ? "（" + warn + "）" : ""}`;
      out.className = "test-result ok";
      const status = await api("/api/auth/status").catch(() => ({ authenticated: false }));
      setTimeout(() => location.replace(status.authenticated ? "/" : "/login"), 1200);
    } catch (err) {
      out.textContent = err.message;
      out.className = "test-result err";
    }
  });

  // ---------- 启动 ----------
  (async function init() {
    await Promise.all([loadStats(), loadConfig(), loadDocuments(), loadAccount()]);
    setInterval(loadStats, 15000);
  })();
})();
