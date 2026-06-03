# Docling 统一摄入方案

## 1. 文档定位

本文件用于定义“统一知识摄入层”的解析方案，重点说明 Docling 在系统中的职责、边界和落地方式。

它是主设计文档 [PERSONAL_RESEARCH_AGENT_PLAN.md](</e:/Agent-learn/agent项目/agent项目/docs/design/PERSONAL_RESEARCH_AGENT_PLAN.md>) 的专项补充，不单独定义产品主线。

## 2. 目标

统一处理以下内容源，并尽可能沉淀为 Markdown：

- 论文 PDF
- 本地 PDF
- 小红书链接与分享文本
- 小红书图片
- 用户上传图片
- 用户粘贴文本
- 后续扩展的 DOCX / PPTX / HTML

核心目标不是“引入 Docling 这个技术名词”，而是把不同来源的内容统一转成可入 Wiki、可检索、可再加工的 Markdown。

## 3. 核心结论

### 3.1 Docling 是增强解析器，不是产品主角

Docling 在系统中的角色是：

- PDF 解析增强
- 图片 OCR 增强
- 多格式文档转 Markdown 的统一入口

它不应该成为独立页面，也不应该让用户理解底层到底是 PyMuPDF、Docling 还是其他 OCR 工具在工作。

### 3.2 Markdown 才是最终统一输出

无论输入是什么格式，统一摄入层的最终目标都是生成：

1. 原始 Markdown
2. 标准化元数据
3. 可进入 Wiki 的结构化卡片
4. 可进入全文检索的 chunk

### 3.3 Docling 保持服务端可选，但在正式部署中建议默认安装

这是一个工程边界问题，不是产品策略问题。

- 本地开发时：允许缺失，系统优雅降级
- 正式部署时：建议安装，提升 PDF 和图片处理能力

原因很直接：Docling 依赖重，适合作为“增强能力”，不适合把整个系统启动链路绑死在它上面。

## 4. 当前产品语境下的真实需求

当前系统最需要解决的不是“论文解析多高级”，而是以下问题：

1. 小红书、截图、图片里的信息能不能进 Wiki
2. 论文 PDF 能不能快速转成 Markdown 并入库
3. 所有正文内容能不能进入统一检索面
4. 同一份内容能不能避免重复入库
5. 入库失败时能不能回退而不是整个功能不可用

Docling 的价值，正是在第 1、2、3 点。

## 5. 统一摄入架构

```text
用户输入
  - URL
  - PDF
  - 图片
  - 文本
        |
        v
Source Fetcher
  - 下载源文件
  - 解析分享文本
  - 保存附件
        |
        v
Parser Layer
  - Docling 优先
  - fallback parser / OCR 次之
        |
        v
Raw Markdown Builder
  - 生成原始 markdown
  - 附带来源元数据
        |
        v
Wiki Compiler
  - 生成 / 更新 Wiki 卡片
  - 判重
  - 生成摘要 / 标签 / 页面类型
        |
        v
Chunk Index
  - 全文分块
  - 进入 Wiki 检索
```

## 6. 输入源与处理策略

### 6.1 论文 PDF

目标：

- 解析正文
- 尽量保留标题、章节、摘要、参考信息
- 输出原始 Markdown
- 生成 `PaperPage`

策略：

- Docling 优先
- 若失败则回退到现有 PDF 解析器
- 无论走哪条路径，最终都要进入 Wiki Markdown 和 `wiki_chunks`

### 6.2 小红书链接

目标：

- 提取标题、描述、页面元数据
- 获取图片链接
- 下载可访问图片
- 对图片做 OCR
- 生成 `InterviewQA` 或 `SourceNote`

现实约束：

- 小红书公开页经常只能拿到部分正文
- 真正的面经内容可能在图里

所以这里最关键的不是 HTML 提取，而是“图片下载 + OCR + Markdown 化”。

### 6.3 用户上传图片

目标：

- OCR 识别
- 原图保留
- 生成原始 Markdown
- 可直接保存为 Wiki 卡片

适用场景：

- 面试截图
- 公式截图
- 公众号长图
- 笔记截图

### 6.4 纯文本或粘贴内容

目标：

- 直接生成 raw markdown
- 允许快速入库
- 适合临时摘抄、面经整理、读后总结

### 6.5 后续文档格式

DOCX / PPTX / HTML 不是当前第一优先级，但设计上应保持统一入口，避免后续又分裂出新摄入链路。

## 7. 输出规范

每次成功摄入，至少应生成以下对象：

### 7.1 原始 Markdown

建议目录：

```text
data/wiki/raw_sources/
  paper_pdf/
  xiaohongshu_note/
  image_note/
  pasted_note/
  web_article/
```

### 7.2 结构化 Wiki 页面

例如：

- `PaperPage`
- `InterviewQA`
- `SourceNote`
- `ProjectPage`

### 7.3 全文检索分块

统一进入 `wiki_chunks`，而不是保留成孤岛结构。

## 8. 依赖策略

### 8.1 正式建议

服务器环境建议安装 Docling。

因为你的真实目标就是：

- 处理图片
- 处理 PDF
- 尽量多内容转 Markdown

这种场景下，Docling 不是多余依赖，而是明确有价值的能力增强。

### 8.2 工程要求

即便如此，代码里仍然要保持“可降级”：

- Docling 未安装时，系统仍能启动
- PDF 仍可通过 fallback 解析
- 图片 OCR 状态可标记为 `pending`
- UI 不因底层解析器缺失直接报废

原因：

- 方便本地开发
- 方便 CI
- 方便后续拆分服务

### 8.3 不建议的做法

不建议把“是否安装 Docling”暴露成用户层配置项。用户不关心这个。

## 9. 与当前系统的收敛要求

### 9.1 不再维持双文本存储主链路

需要收敛为：

- Markdown 是正文事实源
- SQLite 是索引层
- `wiki_chunks` 是统一全文检索面

不应该继续让：

- `paper_blocks` 成为论文正文孤岛
- Wiki Chat 只搜摘要
- OCR 文本存在但无法被 Wiki 检索

### 9.2 论文内容必须打通 Wiki

论文一旦入库，应该至少具备：

- 原始 Markdown
- 对应 Wiki 卡片
- 可检索全文 chunk

否则“论文入库”只是个展示动作，不是真正进入知识库。

### 9.3 小红书图片 OCR 必须进入 Wiki 文本面

如果 OCR 文本只挂在某个 JSON 字段里，而不写回 Markdown / Wiki 内容，那对后续检索、问答、推荐都没有价值。

## 10. 模块职责建议

### `system/document/docling_parser.py`

职责：

- 安全包装 Docling
- 返回统一解析结果
- 隐藏底层依赖细节

### `system/document/source_files.py`

职责：

- 下载外部图片
- 推断扩展名
- 处理去重命名
- 保存附件

### `backend/api/wiki.py`

职责：

- 统一处理文本、图片、小红书类入库
- 触发编译与 reindex

### `backend/api/papers.py`

职责：

- 处理 PDF 摄入
- 触发原始 Markdown 生成
- 生成或更新 `PaperPage`
- 触发全文 reindex

## 11. 验收标准

完成统一摄入后，至少应满足：

1. PDF 入库后，正文能在 Wiki Chat 里被搜到。
2. 小红书链接入库后，标题、描述、OCR 文本能被写入 Markdown。
3. 上传图片后，OCR 文本能进入 Wiki 卡片和全文检索。
4. 相同来源不会重复生成多份卡片。
5. 解析失败时系统仍可给出可理解状态，而不是静默失败。

## 12. 非目标

当前阶段不追求：

- 把所有复杂版式 100% 还原
- OCR 完全零误差
- 一步到位支持所有文件格式
- 为 Docling 单独做产品层展示

## 13. 结论

在当前项目里，Docling 最合适的定位是：

```text
统一摄入层中的增强解析器
```

它的职责是帮助系统把更多真实世界内容转成 Markdown 并纳入统一 Wiki，而不是再造一个新的产品概念。
