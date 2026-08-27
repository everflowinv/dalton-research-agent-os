# S7e：Weekly Brief 正式 authority（2026-08-27）

## 结论

Dalton 的 weekly brief 不是开发周报。它只回答：正式研究证据、现有 Thesis、公司分化、证据缺口、争议和下一步研究问题在本期发生了什么变化。代码、部署和测试进度继续记录在 `docs/PROJECT_STATUS.md`。

首期 issue 是基线，不能把此前积累的正式 Claim 冒充为“本周新增”。第二期开始，Core 才能按上一期 exact issue 逐项计算新增、延续、移出、driver 变化、缺口变化、问题变化和 ThesisVersion 变化。

## 合同

新增三类 append-only authority：

- `WeeklyBriefIssueVersion`：绑定 exact IndustryEvidencePack、CompanyOverlay、快照 hash、基础 Markdown hash、研究窗口和可选的当前 ThesisVersion。
- `WeeklyBriefDelivery`：绑定 exact issue、目标、外部消息和实际投递正文的 SHA-256。
- `WeeklyBriefFeedback`：绑定 exact issue 和 brief / company / driver / Claim 目标；verdict 为 `read / useful / needs_more_evidence / disagree / revise`，修改意见只能追加版本，不能覆盖旧记录或分叉。

WeeklyBriefIssue 的固定内容是：

1. 本期研究变化；
2. 对现有观点的影响；
3. 公司与 driver 分化；
4. 证据缺口；
5. 关键争议；
6. 下期研究问题；
7. 来源与 authority。

如果一家公司没有正式当前 `ThesisVersion`，brief 必须写 `insufficient`，不能从 SEC Claim 自动生成投资结论。

## 实现

- 新增 `src/dalton_core/weekly_brief.py` 和 `weekly_brief_schema.sql`。
- issue 读取会重放 exact IndustryEvidencePack / overlay 快照和 Markdown，并重验保存的 ThesisVersion、公司归属、前一期链和 change summary；任何 hash、版本或公司绑定漂移都 fail closed。
- writer 新增 human-governed `publish_weekly_brief`、`record_weekly_brief_delivery`、`record_weekly_brief_feedback`；Core 和 Cockpit 有只读 issue / render / feedback / integrity RPC。
- Cockpit 的 `dashboard-control` 只能替 exact `human:tailscale-*` subject 记录 weekly brief feedback；不能发布 issue 或伪造其他 human subject。
- bootstrap 会创建 schema，并在现有 token config 上追加受控 operation，不暴露 Core SQLite 路径。

## 验证

- `tests.test_weekly_brief`：5/5。
- `tests.test_writer_service`：22/22。
- `tests.test_industry_research`、`tests.test_industry_evidence_brief_v3_canary` 和 weekly brief 合并回归：13/13。
- packaging：1/1；`compileall`、`git diff --check` 通过。
- 临时 state 连续执行两次 `dalton-bootstrap`，五张 weekly brief 表存在，Core principal 持有 7 个 weekly brief operations。
- live Core 一致性副本 canary：绑定现有 lane-only evidence pack 和 ACN / CTSH / EPAM / IBM 四份 overlay，发布首期基线 issue；4 条 baseline Claim、0 条 new Claim、4 家 Thesis 均为 `insufficient`；连续渲染逐字节一致，5,180 bytes，SHA-256 `8d8532cc791d83779fe6d9e6762336b4848d4ebc01a6f0ecfd27e527af42f4b2`；integrity `ok=true`。
- `2cecd12` 于 2026-08-27 08:05 UTC 部署 live，health `ok=true / state=running`。live 首期 issue
  `weekly-brief-version:us-it-services:2026-w35` 绑定 4 条 baseline Claim、0 条 new Claim，4 家均无正式 ThesisVersion，状态为
  `insufficient`。实际投递 Markdown 5,180 bytes，SHA-256 `50d24d6815c778ee6280ceda82bb876634659e913beeec40008502abe39754d9`，
  Discord message `1542445868618223636`；DeliveryReceipt 已写入。owner 的“brief 写每周研究变化，不是开发周报”意见以 `revise`
  feedback 写入 exact issue。live integrity 为 1 issue / 1 delivery / 1 feedback、0 问题。

仓库没有 `tests.test_bootstrap` 模块；bootstrap 验收使用上面的临时 state 双启动替代，不把不存在的测试模块记成通过。

## 明确没做

- 未创建 weekly cron；本切片先建立正式 issue、投递和反馈 authority。
- 未生成任何新 Claim 或 ThesisVersion，未调用模型和外部 connector。
- PROJECT_STATUS 仍是开发进度的唯一记录，不混入投资研究 brief。
