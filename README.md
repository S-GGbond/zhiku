# 本地知识库 RAG

一个可自托管的个人/团队知识库：上传文件 → 自动解析切分 → 向量化入库 → 带引用的问答，
提供 WebUI 管理界面、REST API，以及可直接给 Agent 使用的 MCP 工具。

- **前端**：原生单页（无构建），FastAPI 托管
- **存储**：单个 SQLite（元数据 + 原始文件 + 分块文本 + float32 向量），零外部依赖，备份/迁移只搬一个文件
- **检索**：向量召回 + BM25 关键词召回，RRF 融合；中文用 jieba 分词
- **模型**：对话与向量都走任意 OpenAI 兼容接口（不绑定供应商，不下载本地模型）
- **两种问答模式**：`AI 回答` 生成带引用的答案；`仅检索` 直接返回命中原文，不调用对话模型（未配置对话模型时自动降级）

## 快速开始

```bash
cd /home/chen/Project/zhiku
cp .env.example .env          # 已存在则跳过
./start.sh                    # 或 python3 cli.py serve
```

打开 http://127.0.0.1:8000 ：**首次访问会先让你创建账号**（用户名 + 密码，至少 6 位），
之后所有页面与 `/api` 都需要登录。忘记密码用 `python3 cli.py reset-password` 本地重置。

登录后进入「设置」：

1. **向量模型**：填 Base URL / API Key / 模型名，点「测试连接」
   （需要服务端支持 `POST /v1/embeddings`，例如 OpenAI、智谱、硅基流动、vLLM、Xinference 等）
2. **对话模型**：填 `/v1/chat/completions` 对应的地址与模型名，点「测试连接」
3. 保存后即可在「文档」页上传资料

> 更换向量模型或维度后，必须在文档页点「重建全部向量」，否则维度不一致会退回关键词检索。

## 支持的文件

| 类型 | 说明 |
| --- | --- |
| PDF | 按页解析，保留页码引用；扫描件/图片型 PDF 不支持（无 OCR） |
| Word (.docx) | 识别标题层级与表格 |
| Excel (.xlsx) | 每个工作表一个块，最多 3000 行 |
| PPT (.pptx) | 按页解析，保留页码 |
| Markdown / TXT / CSV | 按标题与段落切分 |
| 代码/配置文件 | .py .js .ts .java .go .rs .sql .yaml .json 等 |
| 网页 | 粘贴 URL，抓取正文后入库 |

单文件默认上限 50MB（`MAX_UPLOAD_MB`）。上传后后台异步解析、向量化，列表实时显示进度。

## 目录结构

```
app/
  config.py     配置解析（.env → 环境变量 → WebUI 覆盖）
  runtime.py    运行时单例（模型客户端、维度校验、连通性测试）
  db.py         SQLite 表结构与读写
  parsers.py    PDF/Office/文本/网页解析
  chunker.py    标题感知的滑窗分块
  index.py      向量 + BM25 混合检索（RRF 融合）
  rag.py        查询改写、上下文拼装、流式回答
  ingest.py     后台入库流水线
  api.py        REST 接口
  main.py       应用装配
static/         WebUI（index.html / app.js / style.css）
mcp_server.py   MCP 工具（stdio）
cli.py          命令行：serve / ingest / search / ask / status
data/           kb.sqlite3（含原始文件与向量）与 backups/（不入库 git）
```

## API

所有接口前缀 `/api`，交互式文档见 `/api/docs`。若在设置里启用了访问令牌，
请求需带 `Authorization: Bearer <token>`（或 `X-API-Key`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats` | 文档数、片段数、向量数 |
| GET/PUT | `/api/config` | 读取/更新配置（密钥以 `********` 掩码返回） |
| POST | `/api/config/test` | `{"target":"embed"}\|{"target":"llm"}` 连通性测试 |
| GET | `/api/documents` | 文档列表 |
| POST | `/api/documents` | 上传文件（multipart，字段名 `files`，可多选） |
| POST | `/api/documents/url` | `{"url":"https://..."}` 抓取网页 |
| POST | `/api/documents/sync` | `{"path":"/abs/file.md","tags":["规范"]}` 按本机路径入库，同路径覆盖旧版本 |
| POST | `/api/notes` | `{"title":"发布规范","text":"...","tags":["规则"]}` 直接存一段结论/规则 |
| GET | `/api/documents/{id}/chunks` | 查看分块结果 |
| DELETE | `/api/documents/{id}` | 删除文档及片段 |
| POST | `/api/documents/{id}/reindex` | 重新解析并向量化 |
| POST | `/api/reindex` | `{"scope":"embeddings"}` 全部重算向量 |
| POST | `/api/search` | 只检索：`{"query":"...","top_k":6,"doc_ids":[1,2]}` |
| POST | `/api/chat` | 问答：`{"query":"...","stream":true,"mode":"answer"}`（SSE 流式；`mode=retrieve` 只返回检索片段） |
| GET | `/api/auth/status` | 是否已初始化、当前是否已登录 |
| POST | `/api/auth/setup` | `{"username":"...","password":"..."}` 首次创建账号（仅未初始化时可用） |
| POST | `/api/auth/login` | 登录，成功后下发 `kb_session` Cookie（HttpOnly，7 天） |
| POST | `/api/auth/logout` | 退出登录 |
| POST | `/api/auth/password` | `{"old_password":"...","new_password":"..."}` 修改密码 |
| GET | `/api/backup/export` | 导出整库备份 zip（数据库 + 原始文件 + 笔记 + 账号配置） |
| POST | `/api/backup/import` | 上传备份 zip 并整体覆盖恢复（multipart 字段 `file`） |

### 示例

```bash
# 检索
curl -s -X POST http://127.0.0.1:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"部署需要多少内存","top_k":4}'

# 问答（流式，事件类型 meta / delta / done / error）
curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"query":"回滚策略是什么","stream":true}'

# 只检索，不调用对话模型
curl -s -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"query":"回滚策略","mode":"retrieve","stream":false}'

# 上传
curl -X POST http://127.0.0.1:8000/api/documents -F 'files=@方案.pdf' -F 'files=@制度.docx'
```

## 给 Agent 使用（MCP）

`mcp_server.py` 通过 HTTP 调用本服务，暴露 8 个工具：
`search_knowledge_base`、`ask_knowledge_base`、`list_documents`、
`add_document`、`add_web_page`、`sync_document`、`save_note`、`kb_status`。

- `sync_document`：按路径覆盖更新，内容没变自动跳过，不会产生重复文档；
- `save_note`：一段结论/规则直接入库（落地为 `data/notes/*.md`），同标题覆盖。

先启动服务，再在 Agent 的 MCP 配置里加入（以 CodeBuddy / Claude Desktop 为例）。
开了登录后，二选一：设置页开启「访问令牌」并填 `RAG_API_TOKEN`，或填 `RAG_USERNAME` / `RAG_PASSWORD` 让 MCP 自己登录。

```json
{
  "mcpServers": {
    "local-knowledge-base": {
      "command": "python3",
      "args": ["/home/chen/Project/zhiku/mcp_server.py"],
      "env": {
        "RAG_BASE_URL": "http://127.0.0.1:8000",
        "RAG_API_TOKEN": "",
        "RAG_USERNAME": "",
        "RAG_PASSWORD": ""
      }
    }
  }
}
```

对话模型和向量模型都可被 Agent 直接复用：Agent 负责「怎么问」，知识库负责「答什么、引用哪一段」。

仓库里自带两份可复制的配置：

- `.mcp.json`：只含 `local-kb` 一个 server 的模板，路径按你实际克隆位置改；
- `skills/kb-memory/SKILL.md`：Agent 侧的行为约定（先查后写、什么该入库、怎么避免重复），
  装到 `~/.codebuddy/skills/` 后，支持 Skill 的 Agent 会自动读取。

```bash
cp -r skills/kb-memory ~/.codebuddy/skills/     # 安装 skill
```

### 让 Agent 自动维护知识库

判断「什么该入库」交给 Agent，不写死规则。落地方式是两层：

1. **能力层（MCP 工具）**：`sync_document` / `save_note` 提供「按身份覆盖」的写入口，
   先查后写就能避免同一份文档堆出多个版本；
2. **行为层（Skill）**：`~/.codebuddy/skills/kb-memory/SKILL.md`，约定动笔前先检索既有规范，
   收工后按「会复用 / 非即时 / 非原文可查 / 有归属 / 不含敏感信息」自己判断是否入库，
   同主题改旧不建新，并在文末记录所用模型与依据文档。

任何支持 MCP 的 Agent 都能读工具描述行事；支持 Skill 的 Agent 再叠加行为约定，效果更稳定。

> 新增 MCP 工具后需要重连 MCP（重启 Agent 或重新加载 server），当前会话不会自动看到新工具。

## 命令行

```bash
python3 cli.py serve                  # 启动服务
python3 cli.py ingest ~/文档 ~/notes   # 批量导入文件或整个目录
python3 cli.py search "回滚步骤"       # 只看检索结果
python3 cli.py ask "回滚步骤是什么"    # 生成带引用的回答
python3 cli.py status                 # 查看文档/向量状态
python3 cli.py reset-password         # 本地重置登录密码（忘记密码时用）
```

## 配置项（.env 或设置页）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RAG_HOST` / `RAG_PORT` | 127.0.0.1 / 8000 | 监听地址 |
| `RAG_API_TOKEN` | 空 | 非空时 API 需要 Bearer Token |
| `LLM_PRESET` | glm | 预设：glm / mx / kkrich / custom |
| `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` | - | 对话模型 |
| `EMBED_PRESET` `EMBED_BASE_URL` `EMBED_API_KEY` `EMBED_MODEL` | - | 向量模型 |
| `EMBED_BATCH` | 32 | 每次请求的向量化条数 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 800 / 120 | 分块大小与重叠（字符） |
| `TOP_K_VECTOR` / `TOP_K_KEYWORD` | 20 / 20 | 两路召回的候选数 |
| `FINAL_K` | 6 | 送给模型的片段数 |
| `MAX_CONTEXT_CHARS` | 6000 | 上下文长度上限 |
| `QUERY_REWRITE` | true | 多轮对话时先改写检索问题 |

设置页保存的值写入数据库，优先级高于 `.env`。

## 检索与回答是怎么做的

1. 文档解析成带页码/标题路径的文本块，按 800 字左右、120 字重叠滑窗切分，块首补标题上下文；
2. 每块调用向量接口，向量以 float32 存进 SQLite；检索时惰性加载成矩阵（进程内缓存，写入后自动失效）；
3. 查询同时走两路：向量余弦召回 Top-20、jieba 分词 BM25 召回 Top-20，用 RRF（k=60）融合排序；
4. 取前 N 块拼上下文，要求模型只依据资料回答并标注 `[编号]`；
5. 前端把编号渲染成可点击的来源标签，点开可看原始片段与页码。

## 账号与安全

- 单账号体系：首次访问 `/login` 创建，之后页面与 API 都要求登录；
- 会话是 HMAC 签名的 Cookie（HttpOnly / SameSite=Lax，7 天），密钥存在库里，改密码即让所有旧会话失效；
- 密码用 PBKDF2-SHA256（20 万轮 + 随机盐）存储，不存明文；
- **Agent / MCP 不受 Cookie 影响**：在设置页开启「访问令牌」后用 `Authorization: Bearer <token>` 调用 API，
  或在 MCP 配置里给 `RAG_USERNAME` / `RAG_PASSWORD` 让它自己换取会话；
- 忘记密码：`python3 cli.py reset-password`（需本机文件权限，走不通远程）。

## 备份与迁移

设置页「备份与迁移」：**导出备份**下载一个 zip；**导入备份**上传后整体覆盖当前知识库。

```
kb-backup-YYYYmmdd-HHMMSS.zip
├── manifest.json   格式版本、数据目录、文档/片段数、向量维度与模型
└── kb.sqlite3      原始文件 + 碎片正文 + 向量 + 账号 + 配置（SQLite backup API 快照）
```

原始文件与笔记正文都存库内（`doc_blobs` 表），所以 `data/` 下只剩数据库与备份；
旧版本备份包里的 `uploads/`、`notes/` 仍可导入，导入完成后会被自动并入库并清理孤儿文件。

- 换机器：新机器装好依赖启动服务 → 导入 zip → 用备份里的账号登录即可，`source` 路径会自动改写成新数据目录；
- 导入前会自动把当前数据库备份到 `data/backups/before-import-*.sqlite3`，导错了可以拿它回滚；
- 备份里的向量维度与当前模型不一致时，导入结果里会给出提示，执行「重建全部向量」即可；
- 正在入库文档时禁止导入（返回 409），等入库结束再操作。

## 常见问题

**向量接口 403 / 余额不足**：向量化失败会在文档列表显示原因，换一个可用的 embeddings 服务即可，
无需重新上传文件——配置好后点「重建全部向量」。

**PDF 解析为空**：多为扫描件（图片型），当前不做 OCR，可先转成文字版 PDF 再上传。

**换模型后检索报「向量维度变化」**：点文档页「重建全部向量」，会按新维度重算全部片段。

**能离线吗**：对话与向量模型都要求外部接口；若需要全离线，把两个 Base URL 换成自建
vLLM / Xinference / Ollama 的 OpenAI 兼容地址即可（如 `http://127.0.0.1:11434/v1`）。
