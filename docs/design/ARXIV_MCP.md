# arXiv MCP 文献入口

## 目标

这个 MCP 解决的是“让 Agent 找论文并把用户选中的论文交给现有 Wiki 工作流”，不是再实现一套 RAG 或论文解析器。

```text
Agent / MCP Client
  -> search official arXiv metadata
  -> inspect one paper
  -> download a selected PDF to managed cache
  -> submit the PDF to LLM-WIKI API
  -> existing dedupe + parser router + verifier + merge + revision + index
```

搜索和查看详情是只读能力；下载只写自动管理的缓存；只有明确调用 `arxiv_import_paper` 才会创建论文入库任务。

## 工具

| MCP tool | 用途 | 副作用 |
| --- | --- | --- |
| `arxiv_search` | 按主题、作者、分类、年份搜索论文 | 无 |
| `arxiv_get_paper` | 查询一篇论文的完整元数据 | 无 |
| `arxiv_download_pdf` | 下载并校验 PDF，重复请求命中缓存 | 写本地缓存 |
| `arxiv_import_paper` | 将 PDF 提交到现有异步 Wiki 入库接口 | 创建入库任务；可能更新 Wiki |
| `arxiv_ingestion_status` | 查询解析/编译/审批/提交进度 | 无 |

`arxiv_import_paper` 默认使用 `approval_mode=risk`：证据闭合的普通新增自动提交，真正的知识冲突、覆盖旧结论或核验异常才进入 Review Center。同一份 PDF 再次提交由现有 SHA-256 去重逻辑返回 `already_exists`。

## 为什么 MCP 不直接运行解析器

前端上传已经有一条统一入库生命线。MCP 如果直接调用 HTML/MinerU 解析器、数据库和 worker，会产生第二套任务创建、去重和恢复语义，而且 `stdio` Server 内任何意外 stdout 都可能破坏 MCP JSON-RPC。

因此 MCP 只向 `POST /api/papers/ingest` 上传文件。长任务继续由 FastAPI 侧的有界 worker、状态机、checkpoint、lease 和恢复扫描处理；MCP 拿到 `job_id` 后通过状态工具轮询。

## arXiv 访问约束

- 默认 API：`https://export.arxiv.org/api/query`。
- 默认 PDF：`https://arxiv.org/pdf/{id}.pdf`。
- API 与 PDF 请求共享进程内 3.1 秒 courtesy interval。
- 单个 PDF 默认上限 50 MiB。
- 下载先写临时文件，检查 `%PDF-` 文件头后原子替换。
- 缓存同时记录 arXiv ID、来源 URL、下载时间、字节数和 SHA-256。
- 这条链路面向交互式少量论文，不用于批量抓取；大规模语料应使用 arXiv 官方 bulk access。

## 配置

```env
ARXIV_API_URL=https://export.arxiv.org/api/query
ARXIV_PDF_BASE_URL=https://arxiv.org/pdf
ARXIV_USER_AGENT=LLM-WIKI/1.0 (+https://github.com/ouchfys/LLM-WIKI; contact: you@example.com)
ARXIV_REQUEST_INTERVAL_SECONDS=3.1
ARXIV_TIMEOUT_SECONDS=30
ARXIV_MAX_PDF_MB=50
ARXIV_MCP_CACHE_DIR=
LLM_WIKI_API_URL=http://127.0.0.1:8000
```

`ARXIV_MCP_CACHE_DIR` 留空时，缓存位于 `sources/papers/arxiv-cache`。这是程序管理目录，用户不需要手工维护；运行数据已被 Git 忽略。

调用 `arxiv_import_paper` 后，原始 PDF 和编译产物会进入当前 `STORAGE_TENANT_ID` 对应的 `users/{tenant_id}/...` 对象存储前缀。本地 arXiv 缓存只是解析输入与失败重试来源，不是云部署的数据主存。arXiv 来源优先使用结构化 HTML，完整性检查失败时才调用 MinerU。

请将 `ARXIV_USER_AGENT` 中的联系信息改成自己的邮箱，便于 arXiv 在请求异常时联系维护者。

## 启动与接入

安装依赖后，本机客户端让 MCP 进程使用 `stdio`：

```powershell
python -m mcp_servers.arxiv_server
```

支持常见 `mcpServers` JSON 配置的客户端可使用：

```json
{
  "mcpServers": {
    "llm-wiki-arxiv": {
      "command": "python",
      "args": ["-m", "mcp_servers.arxiv_server"],
      "cwd": "E:/Agent-learn/agent项目/agent项目"
    }
  }
}
```

服务化运行：

```powershell
python -m mcp_servers.arxiv_server --transport streamable-http --host 127.0.0.1 --port 8011
```

## 一次真实调用的顺序

```text
1. arxiv_search(query="agent memory", categories=["cs.AI"], max_results=5)
2. arxiv_get_paper(arxiv_id="...")
3. 用户明确选择后：arxiv_import_paper(arxiv_id="...", approval_mode="risk")
4. arxiv_ingestion_status(job_id="...")
5. status=waiting 时去 Review Center；status=done 时在 Wiki 中检索新页面
```

调用方不应该根据搜索结果自动批量导入，也不应该在用户只要求“找论文”时调用 `arxiv_import_paper`。
