# 应用数据目录与测试目录

默认运行数据位于用户目录下的 `~/.paperwiki`（Windows 通常是 `C:/Users/<用户名>/.paperwiki`）。测试时用安装目录的 `.env` 指定替代位置，重启后端后生效：

```dotenv
STORAGE_BACKEND=local
PAPERWIKI_HOME="D:/paperwiki-test"
```

这个测试目录本身就是数据根目录，里面不会再套一层 `.paperwiki`。只需这一个设置，会话、记忆、Wiki、原文、缓存和临时目录都跟随它。代码、前端构建和凭据配置留在安装目录；从其他目录运行 `paperwiki web` 也使用同一数据位置，启动时打印该位置。

`PAPERWIKI_HOME` 优先于旧的 `PAPERWIKI_WORKSPACE_ROOT`；两者均未设置时使用 `~/.paperwiki`。数据库、记忆及仓库缓存仍支持各自显式路径覆盖。环境变量优先于 `.env`。新数据库默认位于 `sessions/sessions.db`；指定的数据根若只有旧版 `sessions.db`，会继续使用它，避免切换成空库。已有旧版记忆目录也会继续读取。代码调用显式传入的临时数据库使用自己的记忆目录，防止修改真实用户记忆。

## 数据归属

- `sessions/sessions.db`：会话、消息、完整工具结果、结构化任务计划、运行 trace、恢复状态、知识索引、来源包和修订记录。不是每个会话一个文本文件。
- `memory/`：用户与项目 Markdown 记忆。
- `projects/`：用户明确需要的报告、脚本及其他交付文件。创建或更新计划不会在这里生成目录，写 Wiki 不额外导出研究报告。
- `wiki/`：正式卡片；`sources/`：原文、上传文件和相关资源。
- `cache/repos/`：仓库缓存；`tmp/<session-id>/`：当前会话的临时脚本、下载和中间文件。
- `archives/`：历史审计、备份、迁移清单和显式查询导出；`maintenance/`：知识维护产物；`logs/`：服务日志。

普通对话及完整工具结果保存在数据库，不再自动向 `queries/answered` 导出第二份 Markdown。独立维护功能仍可显式归档到 `archives/queries`。

Shell 默认使用当前会话的临时目录，仅实际运行 Shell 时才创建。简单对话和计划更新不会创建任务文件夹；模型的工作指令与工具使用同一路径。显式指定工作目录仍有效，Shell 仍拥有服务进程权限，这不是文件系统沙箱。目录集中管理不代表自动删除临时文件。

切换路径不会迁移旧数据。初始化空目录可得到独立测试项目；旧会话和 Wiki 保留在原位置。运行中的服务不会自动重载 `.env`，切换前应确认没有进行中的任务。

## 任务计划

计划由主模型按需使用：简单问答和短工具序列可以直接完成；多阶段任务可以提交步骤和状态，并在有实际进展时更新。同一任务复用已有计划，新会话不自动加载其他会话的计划。无需独立的问题分类器或强制规划阶段。

计划保存在会话数据库的 `task_plans` 和 `task_plan_sessions` 表，工具历史保留提交过程。删除或清空会话会在同一数据库事务中解除计划关联，没有其他会话使用的计划随之删除。

`PAPERWIKI_TASKS_ROOT` 仅用于导入旧版计划：启动时读取该路径里的 `PLAN.md` 和 `metadata.json`，将关联仍存在会话的计划导入数据库，原文件保留。导入记录防止重启后重复导入或恢复已删除的计划；新计划不再写入此目录。旧计划的完整正文保留在数据库中，直到模型用结构化步骤更新它。归档后的旧计划不自动扫描，避免因路径变化重新导入已删除计划。

## 存储与模型工具的分工

聊天默认开放 12 个工具：`local_shell`、`repository`、`wiki_write`、`wiki_open`、`evidence_lookup`、`arxiv_lookup`、`arxiv_import_paper`、`arxiv_ingestion_status`、`task_plan_read`、`task_plan_write`、`project_memory_update`、`read_tool_result`。

- 网页搜索和读取由模型通过 `local_shell` 执行。旧 `web_search` / `web_fetch` 不再注册，也不会在无结果时自动调用；底层服务仍用于独立的 Wiki 维护功能。
- 计划由程序保存到数据库，并把当前计划提供给模型。模型通过 `task_plan_write` 提交步骤变更；`task_plan_read` 用于按需找回其他已知任务，只有明确设置 `resume=true` 才挂接并续做。
- 记忆的路径、加载和原子保存由程序负责；需要记住什么由模型决定。`project_memory_update` 仅在用户要求或确有跨会话价值时调用，结束任务不会强制增加一次记忆复核请求。
- 每次工具调用的完整参数与结果自动保存。`read_tool_result` 用于补读被截断或压缩掉的原始内容，不是要求模型再次保存结果，也不重新执行原命令。

## 验证

`test/conftest.py` 在收集测试前设置临时运行目录，并使用 `PAPERWIKI_LOAD_DOTENV=0` 禁用自动加载本地 `.env`。未指定记忆和旧计划路径的测试跟随各自数据库目录，避免临时数据库刷新真实项目的记忆文件。测试结束后恢复环境变量；正式启动的默认配置行为不变。

`test/storage/test_workspace_isolation.py` 在含中文和空格的临时目录、不同启动目录下，验证默认数据库及显式数据库两种配置。实际执行会话/工具记录保存、记忆落盘、运行创建、原文写入、Wiki 写入和读回、Shell 文件生成，并核对 API 路径解析。

`GET /api/wiki/vault/info` 返回当前服务实际使用的 Wiki 路径，可在重启后核对。
