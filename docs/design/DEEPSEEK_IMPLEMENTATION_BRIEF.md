# DeepSeek 改码任务说明

## 1. 任务背景

当前项目已经完成从“重型论文 RAG 主线”向“个人 Wiki + 推荐过滤 + 可选精读”的方向收敛。

DeepSeek 后续改代码时，必须遵守新的统一口径，不能再把系统往以下方向拉回去：

- 把 GraphRAG 当主产品
- 重新做一套独立论文库
- 把 PaperIndex 当成独立用户功能
- 为聊天风格堆很多无关偏好字段

主设计文档：

- [PERSONAL_RESEARCH_AGENT_PLAN.md](</e:/Agent-learn/agent项目/agent项目/docs/design/PERSONAL_RESEARCH_AGENT_PLAN.md>)

相关专项文档：

- [DOCLING_UNIFIED_INGESTION_TASK.md](</e:/Agent-learn/agent项目/agent项目/docs/design/DOCLING_UNIFIED_INGESTION_TASK.md>)
- [MONTHLY_READING_RECOMMENDER_TASK.md](</e:/Agent-learn/agent项目/agent项目/docs/design/MONTHLY_READING_RECOMMENDER_TASK.md>)
- [MEMORY_SESSION_PLAN.md](</e:/Agent-learn/agent项目/agent项目/docs/design/MEMORY_SESSION_PLAN.md>)
- [KARPATHY_STYLE_WEB_WIKI_PLAN.md](</e:/Agent-learn/agent项目/agent项目/docs/design/KARPATHY_STYLE_WEB_WIKI_PLAN.md>)

## 2. 产品主线

请严格按以下主线理解项目：

```text
内容采集
  -> Markdown 沉淀
  -> Wiki 卡片
  -> 全文 chunk 检索
  -> Wiki Copilot
  -> 推荐过滤
  -> 少量高价值论文进入精读
```

## 3. 当前架构原则

### 3.1 Markdown 是正文事实源

正文内容不要再主要存数据库摘要字段。

必须优先保证：

- 原始内容能落盘为 Markdown
- Wiki 页面能落盘为 Markdown
- 检索从全文 chunk 走

### 3.2 SQLite 是索引和状态层

数据库主要用于：

- 页面索引
- chunk 索引
- 会话历史
- 用户画像信号
- 推荐反馈
- 任务状态

### 3.3 GraphRAG 只做可选精读

不要再让任何页面、API 或存储结构默认依赖 GraphRAG。

### 3.4 推荐系统先做过滤，不做复杂预测

推荐系统的第一阶段目标是：

- 从 GitHub 热门、arXiv 热门/最新、技术媒体热点里抓候选
- 再按用户目标、兴趣、近期行为做过滤和排序

## 4. 当前必须遵守的非目标

不要做：

- 新的独立论文系统
- 新的独立 PaperIndex 前台
- 复杂知识图谱展示
- 无关的聊天人格系统
- 过多回答风格偏好字段

## 5. 当前代码改动优先级

如果继续开发，优先按以下顺序做：

### P0：统一知识沉淀链路

目标：

- PDF / 图片 / 小红书 / 文本都能尽量沉淀为 Markdown
- 进入 Wiki 卡片
- 进入 `wiki_chunks`

验收：

- Wiki Chat 能搜到正文，不只是摘要

### P1：推荐系统产品化

目标：

- 构建热门内容池
- 展示本月推荐
- 支持保存 / 已读 / 忽略 / 精读
- 推荐理由可解释

验收：

- 推荐页不是空列表
- 用户操作会写回反馈

### P2：记忆与画像去噪

目标：

- 记忆系统主要服务检索和推荐
- 清理低价值偏好字段
- 优先消费显式信号和行为信号

### P3：精读入口收敛

目标：

- 精读只针对少量高价值论文
- 保留异步进度与结果回写

## 6. 建议你让 DeepSeek 执行的方式

不要只给一句“帮我改代码”。

你应该给它四类信息：

1. 产品主线
2. 明确本次只改什么
3. 不允许动什么
4. 验收标准

## 7. 可直接复制给 DeepSeek 的提示词

```text
你现在要改一个 FastAPI + Vue 项目。

先不要重构整个项目，也不要发散设计。请严格按以下产品方向改代码：

1. 这是一个“私有化贾维斯”个人知识代理系统。
2. 主线是：内容采集 -> Markdown 沉淀 -> Wiki 卡片 -> 全文 chunk 检索 -> Wiki Copilot -> 推荐过滤 -> 可选精读。
3. Markdown 是正文事实源，SQLite 只是索引和状态层。
4. GraphRAG 不是主产品，只是少量高价值论文的可选精读能力。
5. 推荐系统当前只做“从热门公开内容中按用户画像过滤”，不做复杂工业推荐。

本次只做下面这些任务：

[在这里写你这次要它改的具体任务]

约束：

- 不要新增独立论文系统
- 不要把 PaperIndex 做成新的前台主功能
- 不要引入与主线无关的复杂偏好字段
- 不要改坏现有 Wiki -> Markdown -> chunk 检索链路
- 尽量复用现有模块和目录结构

你输出时请严格按这个顺序：

1. 先说明你理解到的当前架构
2. 再列出本次要改的文件
3. 再给出具体改动
4. 最后给出验证方式
```

## 8. 更好的下发方式

如果任务稍复杂，建议你再附上一段“本次任务单”：

```text
本次任务：
- 目标：
- 需要修改的 API：
- 需要修改的前端页面：
- 需要复用的现有模块：
- 验收标准：
- 非目标：
```

这样 DeepSeek 更不容易跑偏。

## 9. 我建议你下一步让 DeepSeek 改什么

优先级最高的实际任务是：

1. 把推荐页做实，不要只是有表结构
2. 把热门候选池接起来，至少接 GitHub + arXiv
3. 把“保存到 Wiki”后的闭环做扎实
4. 把图片 / 小红书 / PDF 入库后的 Markdown 与 chunk 更新再检查一遍

## 10. 结论

让 DeepSeek 改代码的关键不是“模型够不够强”，而是你给它的任务单是否收敛。

你只要给它：

- 统一产品方向
- 清晰边界
- 明确本次任务
- 明确验收标准

它就不容易把项目再次改散。
