# 2026-09-27：提交评审截断、检索预算耗尽，四仓库只交付两篇

## 核验范围

- 用户请求：研究 DeepSeek Harness、Codex CLI、Pi、Hermes 的短期记忆、compaction 策略与提示词、长期记忆和例子，并分别写入 Wiki。
- Run：`1343f65f-13e2-4b9e-9c57-a7c90fb6e762`；session：`8b406216-a930-46fd-88e2-f3479f272d6d`。
- 时间：北京时间 2026-09-27 14:18:14–14:45:35，约 27 分 21 秒。
- 只读核对 `sessions.db` 的运行、工具结果、模型事件、修订记录；核对两篇 Markdown 与当前 committed revision 完全一致。
- 本次复盘未修改运行代码、未再次调用研究模型、未补写 Hermes/DSH。原始私有推理内容和凭据不导出。
- 证据摘要：[evidence-summary.json](<C:/Users/付同学/Desktop/paper wiki测试/queries/audits/trace-repository-memory-2026-09-27-review-budget/evidence-summary.json>)。

## 结论

这轮完成 2/4，Codex/Pi 已提交并读回成功，Hermes/DSH 没有调用写入。GitHub 认证有效，本轮没有 403/429。失败链由三个问题叠加：评审 high 思考与固定输出预算不匹配；补证据导致已经通过的小节重新评审；前两个项目用完全轮 64 次检索预算，剩余项目无法继续。

### 实际交付

| 项目 | 最终状态 | 依据 |
| --- | --- | --- |
| Codex CLI | 已提交 | 工具调用 70、result 287；card `repo-1d0ee676f74eb68605c9ec0e`；revision `d6d172ef-4867-4b0b-b49d-eb4ce4696c30` |
| Pi | 已提交 | 工具调用 82、result 299；card `repo-0375aa13a93734d6346df1cc`；revision `04ddea3f-ee94-4cfb-b23e-0f0ed3f7ebb8` |
| Hermes | 只打开仓库，未读取文件、未写入 | `NousResearch/hermes-agent` 的 open 成功；之后没有针对该仓库的研究调用 |
| DSH | 读取社区索引、尝试一个地址，未研究实现、未写入 | 读了 `0xsline/awesome-deepseek-harness` README；`deepseek-ai/dsh` 返回 404；没有执行计划中的 npm 解包或官方文档读取 |

DSH 某个地址返回 404 不能证明“没有公开核心仓库”。原计划和最终回答把这一观察扩大成了全局结论，仍需纠正；这里只确认该地址本次访问失败。

## 1. 截图中的主要失败：评审尚未给出结论就耗尽输出预算

本轮共 84 次实际工具调用，69 次成功、15 次错误：58 次 repository、6 次 local_shell、16 次 wiki_write、3 次计划更新和 1 次项目记忆更新。15 次错误中，14 次是 Wiki 写入，1 次是上述 404。

Codex 有 14 次写入尝试，第 14 次成功；Pi 有 2 次，第 2 次成功。写入次数不等于评审调用次数：结构错误不会调用模型，长文章也可能分成多个评审批次。

提交评审模型调用共 17 次，其中 8 次发生输出截断，涉及 6 次失败写入。8 次均有如下原始用量：

```json
{
  "finish_reason": "length",
  "max_tokens": 16000,
  "completion_tokens": 16000,
  "completion_tokens_details": {"reasoning_tokens": 16000},
  "response_chars": 0
}
```

因此是模型把 16,000 输出 token 全部用于思考，还没生成检查 JSON 就结束；并非文章结论已经被评审判错，也不是 HTTP 超时、GitHub 故障或输入上下文溢出。8 次合计约 9 分 21 秒，全部 17 次评审约 15 分 05 秒。

代码链：

- `system/wiki/repository_verification.py:103`：评审使用 `max_tokens=16000, max_attempts=1, thinking=True`。
- `system/core/llm_call.py:61` / `system/core/thinking.py`：评审继承当前任务选择。本轮是 `thinking_effort=high`。
- 评审批次只按 90,000 字符分组；此次失败的输入约 51,887–89,803 字符。多个小节重复包含引用正文和全量 read_coverage。
- `_review` 捕获异常后给整批小节设置 `semantic_result=error`，同时设置 `result=unsupported`、`repository_reviewer_unavailable`、`retryable=true`。主模型因此进入改文/重试循环。

这是上次加入思考强度时没有同步处理评审工作负载与输出预算的配置缺陷。此前真实冒烟验证覆盖工具续传，未覆盖长源码证据的提交评审，不能据此声称这条验收 query 已通过。

## 2. 补证据让已通过的小节重新进入大批评审

`RepositoryEvidenceVerifier` 的输入签名包含正文、引用、操作回执，以及当前仓库**全部** `read_coverage`（`repository_verification.py:47–52`）。新读一个文件就可能改变每一节的签名。

本轮有可复核的具体实例：

- revision `49f90d80-9ff8-48a4-bed8-778fa93d8d53` 已有 section-1、2、4、5 四节通过。
- 模型补读后只修 section-0、3；上述四节正文和引用与之前完全相同。
- 下一修订 `3e7b1017-3790-4055-9d6d-692519bd21ab` 的 context_hash 改变，四节缓存全部失效。
- 整篇六节重新评审，两个批次都截断，六节全部变为 `unsupported / error`。

这使“局部修复”再次变成整篇重审。为检查涉及未读范围的断言，保留相关观察是合理的，但不应让不相关的新阅读使全部肯定性结论失效。

## 3. 其他写入错误：有真实内容问题，也有接口问题

不能把所有拒绝都当成误拒：

- 评审指出 Codex 草稿把 `compaction_model_hash` 归到了不正确的结构字段；部分恢复、输入组装等结论缺少所引用片段中的实现证据。
- Pi 草稿对独立记忆库不存在、context files 的发现方式及 `/tree` 可见性作了超出已读片段的判断。主模型删除或收窄这些表述后提交成功。

另外 4 次结构/修复协议错误：

1. 调用 53 使用 `repo_251`、`repo_264`、`repo_266`、`repo_269` 作为 span_id，实际是把工具结果编号拼成了证据 ID，引用不存在。
2. 调用 55 的原始参数 JSON 中，“上一条助手消息”两边的双引号未转义，位置 1453 解析失败。`_normalize_native_tool_calls` 把解析失败静默替换成 `{}`，执行器于是返回缺 title/repository/topic/sections；真正的格式错误被掩盖。不是已证实的“多包了一层 arguments”，不要照抄模型写入计划的这个归因。
3. 调用 61 同时修改了已通过的 section-2，违反当前“只能替换失败小节”规则，被代码拒绝。
4. 调用 63 重新提交整篇却没有 revision_id，再次被局部修复协议拒绝。

## 4. 另外两篇为什么没有导入

运行保存的真实配置为：

```json
{
  "research_mode": false,
  "thinking_effort": "high",
  "budget": {"max_calls": 64, "used_calls": 64},
  "assessments": 0,
  "planner_steps": 50
}
```

64 次检索的分布：Codex 41、Pi 10、Hermes 1、DSH 索引/失败地址 5、仓库发现 7。Repository 的 open/list/search/read/checkout 和 local_shell 都占此预算，包括本地读取；16 次 wiki_write 不占检索预算，但显著增加时间与模型调用。

Pi 阶段计划读取 `packages/coding-agent/docs/how-pi-works.md`、搜索 `AGENTS` 的两次调用被宿主拦截。`wiki_chat.py:1111–1117` 给模型的反馈是：

> Retrieval budget exhausted; finish ready Wiki writes and report remaining work.

模型随后提交 Pi，记录 Hermes/DSH 未完成，再结束。既不是本轮被逐工具充分度评估器误停（评估次数为零），也不是已生成另外两篇却丢失提交。普通模式默认 64，研究模式默认 128，但只加大预算不能解决评审反复截断和分配失衡。

## 5. 运行状态与回答还有两处问题

### 预算耗尽后仍标为正常结束

预算拦截只增加 observation，没有设置专门的结束原因。模型最后不再调用工具，仍保留 `stop_reason=model_finished`；`_complete_chat_runtime` 只要答案保存就进入 `COMPLETED`（`wiki_chat.py:711–724`）。因此 UI/运行状态反映“这一轮已结束”，不表示“四个交付物已完成”。最终答案和计划如实承认只有两篇，结构化结果也应保留 `budget_exhausted / partial`。

### 已写入完整正文，回答阶段却只见截短内容

实物核验：Codex Markdown 14,124 字符，Pi 10,133 字符，均与 committed revision 完全一致，包含长期记忆部分。不是保存时丢段落。

写入工具仅把 `get_card()` 加入 cards，未加载 `_full_text`（`wiki_chat.py:2879–2881`）；`WikiCitationContext` 因而回退到 `_compact_content`，其固定截取 1,600 字符（`wiki_chat.py:4868`）。最终回答明确说长期部分正文不可见，要求用户再打开卡片。新提交卡片应把已核验正文直接交给回答上下文。

## 建议修复顺序

1. 独立设置评审的思考与输出预算；按小节/证据 token 规模分批、重复证据只传一次。截断时由评审器在预算内拆批重试，保留已完成检查，明确 `review_incomplete`，不要要求主模型凭空改事实。
2. 缓存绑定小节正文、实际引用与相关观察。新补读无关文件不应使整篇已通过小节失效；保留真正依赖覆盖信息的检查。
3. 在主模型可见上下文中提供剩余总预算和未完成项目，让模型自行调整研究深度与顺序；多交付物预算应可配置。耗尽后明确部分完成，支持带原结果续跑。无需恢复逐工具语义充分度评估器。
4. JSON 解析失败返回明确格式错误；纠正证据 ID 的结果提示；对未改变的已通过小节可安全复用，减少刚性修复协议的无效往返。
5. 写入成功后把完整已核验正文交给答案生成，避免刚写完还声称看不到长期部分。

验收仍应使用用户原始四仓库 query。故障回归需包含“review high 只产生 reasoning 导致 length”“补证据不重审无关已通过小节”“预算耗尽报告部分完成”“新提交卡片全文进入答案上下文”；夹具通过后再跑真实任务，四篇均有 committed/readback 回执才算全交付。

## 后续按用户澄清实施：完整读一遍

用户进一步明确“写完之后，完整看一遍就行”，据此将仓库复核简化为：

```text
完整草稿 + 全部引用证据（共享片段只传一次）
→ 一次完整模型复核
→ 主模型按意见修正
→ 保存并读取实际 Markdown 确认
```

- 删除分批重复评审、全局阅读记录参与缓存失效的机制；修正后的草稿不调用评审模型。
- 不再限制只替换失败小节。支持完整修正版或对任意已有小节的替换；仍检查真实引用、单一来源版本、修订父版本及文件读回。
- 复核状态持久化在 revision 中，恢复/重启后修正也不重审。修正版标明由作者修正、语义未再次独立运行，不把结构检查伪称为语义通过。
- 输出截断/服务不可用为 `review_incomplete`。返回主模型后由主模型完整自查并提交，不自动重试同一个评审请求，不把运行故障写成事实错误。
- 保留用户选择的思考强度，单次复核输出上限 32,768，最多一次请求。开发中关闭思考的对照试验漏检了旧草稿的问题，因此没有采用为默认配置。
- 保存回执包含已读回的完整正文，并更新回答上下文中的卡片，修正 1,600 字符简介导致长期部分不可见的问题。

### 验证

- 144 项相关回归通过，覆盖一次阅读全文、共享证据去重、完整修正/修改已通过小节、补读不重审、评审输出截断后的作者处理、恢复不重审、四项目脚本化写入、完整正文进入回答、普通修订与工具协议。
- 使用原失败修订 `63b308f4-0072-49a7-b46c-0779c4ed18f1` 的完整六节草稿及实际引用做真实 V4 Flash high 回放：一次请求，18 个独立证据片段，prompt 75,474 字符；约 57.7 秒返回全部六节意见，无截断。completion 13,589 tokens，其中 reasoning 13,274。指出了窗口推进时机表述的问题。
- 回放结果：测试工作区内 `projects/research/single-review-high-smoke-20260927.json`。它验证一次调用可以完成并返回具体意见，不代表每个事实均正确或模型复核没有漏检。
- 回放未修改生产 Wiki。此次实现没有修改 64 次总检索预算及部分完成状态，也没有补写 Hermes/DSH；完整四仓库验收仍未完成。
- 重启后端后新流程生效。
