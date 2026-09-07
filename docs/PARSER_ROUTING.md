# 论文解析路由

当前主链只有三种结果：

```text
arXiv URL -> 完整 HTML -> arxiv-html
                       -> 不完整 -> MinerU VLM
普通上传 / 扫描 PDF   -> MinerU VLM
MinerU 不可用或失败   -> pymupdf-fallback（显式降级）
```

## HTML 质量门

只有同时满足以下条件才接受 arXiv HTML：标题存在、正文和段落达到最低长度、有摘要和参考文献、标题层级完整，以及论文包含较多公式时 TeX 源覆盖率不低于 90%。拒绝原因进入 `metadata.parser_attempts`。

HTML 路径提取标题、摘要、章节、段落、公式、表格和 anchor。它不伪造页码或 bbox。表格同时保存 Markdown 投影与 cell 记录，供精确表格查询。

## MinerU 路径

- arXiv 失败时用公开 PDF URL 创建单文件任务。
- 本地上传通过 MinerU 的签名上传 URL 发送，不要求项目提供公网文件地址。
- 开启公式和表格识别，轮询受总超时约束。
- 结果 zip 在内存中校验和读取，只保留 Markdown、结构化表格以及必要的任务元数据。
- 不把 zip、裁剪图片、layout JSON 或模型中间结果写进项目目录。

## 降级语义

PyMuPDF 只提供快速页面文本。使用它时必须设置：

```json
{
  "parser": "pymupdf-fallback",
  "degraded": true,
  "degradation_reason": "..."
}
```

这让任务可以完成，但调用方能够区分“精准解析成功”和“只有基础文本”，不会静默降低科研资料的准确性。

## 配置

```env
PAPER_PARSER_MODE=auto
ARXIV_HTML_BASE_URL=https://arxiv.org/html
ARXIV_HTML_TIMEOUT_SECONDS=60
MINERU_API_TOKEN=...
MINERU_API_BASE_URL=https://mineru.net/api/v4
MINERU_MODEL_VERSION=vlm
MINERU_TIMEOUT_SECONDS=600
MINERU_POLL_INTERVAL_SECONDS=4
```

`PAPER_PARSER_MODE` 还可设为 `arxiv_html`、`mineru` 或 `pymupdf`，用于诊断和对照实验。生产默认使用 `auto`。
