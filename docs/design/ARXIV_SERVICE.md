# arXiv 论文服务

PaperWiki 在进程内调用 `system.discovery.arxiv_service.ArxivService`，不再提供独立 MCP 服务。

## 当前聊天入口

只注册一个 `arxiv` 工具，通过 `action` 选择操作：

| action | 必需参数 | 用途 |
| --- | --- | --- |
| `search` | `query` | 按论文名称或主题搜索候选；可加作者、分类、年份和结果数量 |
| `lookup` | `arxiv_ids` | 一次查询最多 20 个已知论文编号 |
| `import` | `arxiv_id` | 提交已确定论文的异步入库任务 |
| `status` | `job_id` | 查询入库进度和结果 |

例如用户说“RSIAgent 这篇论文入库”：模型搜索名字，对照候选的标题、作者和摘要确定身份，使用实际返回的编号提交入库，再查状态并打开产出的 Wiki。找不到时调整搜索词；候选仍有歧义时才询问用户。不编造编号，也不要求用户先提供编号或操作 MCP。

搜索结果不是身份确认，排名第一不一定是目标论文。模型负责语义判断；程序检查动作所需参数，并保留已有的编号、任务编号来源约束。查询条件不会绕过 arXiv 本身的检索限制。

## 调度、结果和恢复

搜索、元数据查询和状态查询按只读操作调度；入库为串行写操作。合并工具名称不改变副作用分类。结果和 trace 使用 `tool=arxiv`，通过 `arguments.action` 保留具体动作。

入库提交成功不代表解析完成。后台等待仍按任务编号轮询、退避，并支持取消；最终应检查任务结果和 Wiki 卡片。恢复逻辑同时识别历史 `arxiv_*` 调用和新的 `arxiv` 动作，避免提交响应丢失后重复入库。旧工具名不再向模型注册。

## 共用实现

`ArxivClient` 负责官方元数据查询、关键词检索、请求节流、重试和 PDF 缓存。`ArxivService.import_paper()` 下载 PDF 后，通过 `WikiIngestionClient` 提交现有 FastAPI 入库接口，复用解析、去重和 Wiki 编译流程。

缓存仍使用存储布局中的 `sources/papers/arxiv-cache`。历史环境变量 `ARXIV_MCP_CACHE_DIR` 暂时保留兼容，仅控制缓存路径，不会启动 MCP 服务。入库接口地址由 `LLM_WIKI_API_URL` 配置。

服务测试位于 `test/discovery/test_arxiv_service.py`；工具集成测试位于 `test/wiki/test_unified_arxiv_tool.py`，覆盖搜索到入库的调用流程、参数错误、编号来源、读写调度和状态处理。它们不需要 MCP SDK。模拟服务测试不代表完成了线上全文解析和 Wiki 编译。

架构取舍见 [Tool 与 MCP 的边界](TOOL_MCP_BOUNDARY_CASE.md)。
