# 初筛与备忘录核验失败如何进入研究规划

时间：2026-10-11 UTC。本报告记录失败反馈机制及其验收方法。初筛的第一部分已在 v0.11.14 部署，等待新任务的实际反馈验收；备忘录反馈仍待开发。当前线上进展另见[研究推进记录](research-execution-recovery-2026-10-11.md)。

## 本批实现范围

新初筛调用保存核验所用的公司、材料、旧稿和来源版本。草稿未通过确定性检查时，系统从原任务、正式模型返回和结算记录重新核对，再根据冻结材料重算具体问题。规划最多收到每公司八项问题及完整结果的摘要指纹。问题进入普通研究规划，不增加修正次数或预算。

引用原文按实际使用的段落检查，每份文档只读取一次；单公司最多检查 64 个段落、16 份文档和 64 MB 原件。只读验证进程限时四秒，超时回收并注明无法验证，其他研究继续。相同问题不会因为票据编号或观察时间变化重新触发规划；正式初筛完成后，旧问题可清除。

旧调用继续复用原请求。缺少完整冻结核验上下文的历史初筛记录标为无法验证，因此本批没有把旧 AMZN 的失败摘要直接认证为规划依据。现有正式 IT 五公司规划输入的纯投影检查保留全部公司、原数字和来源；每公司增加八项诊断后为 108,799 字节，低于现有 128,000 字节上限。该检查验证输入容量，线上实际采用仍须通过自然任务观察。

## 已确定的缺口

AMZN 初筛 `initial-screen:1d2a896b7a77d158bdcb458b` 的已结束失败摘要包含 15 条 findings、5 类具体失败。14:38:25 成功的正式规划 Work `work:cockpit-plan-5221ef90f470e4dd8e2ef5b9fac1eee9` 原始冻结 prompt 中，这五类 code 均不存在。规划器看不到具体引用、数字与推导缺因，并非这些检查已经通过。

该初筛 ticket 终态时间为 14:21:00.461821 UTC。summary 的 `created_at=13:57:08.025408` 是批次开始时间，不能当作失败发生时间。summary `formal_authority_writes=0`，没有发布初筛产物。该摘要本身没有 producer Work、formal、route、invocation 引用，不能仅凭摘要文本认证失败。

五类失败：

- `quote_not_in_passage`：引用文字不在对应原文窗口内。
- `citation_or_contract`：数字引用缺少 figure 或数字没有来源。
- `machine_precision_number`：把机器精度数值直接展示。
- `forecast_gap_without_expression`：预测缺口没有相应表达式。
- `appendix_request_not_cited`：未知事项未引用对应 MaterialRequest。

这些属于不同处理路径。格式或引用错误不能被统一翻译成“缺资料”，从而让规划器重复买材料。

## 现有反馈的覆盖边界

| 入口 | 当前覆盖 | 不覆盖的部分 |
| --- | --- | --- |
| `model_execution_failure_feedback.last_failures` | `mission_model_spec`、`mission_model_forecast` 的强 hash 运维事件 | 初筛、备忘录；该 ledger 明确不绑定 Work formal，不能冒充正式模型证明 |
| `document_execution_failures` | 文档 admission 与文档模型执行失败 | 产品初筛及备忘录核验失败 |
| `dossier_repair_feedback` | 已成功结束 Dossier 子票据的有界 repair targets；有稳定语义/节流机制 | 失败初筛或备忘录；不能改变其“成功 Dossier”认证契约 |

v0.11.13 及更早的初筛 `_call_json` 返回可用的模型调用引用，但旧失败 summary 没有完整保存它们。v0.11.14 为新调用保存冻结上下文和独立诊断记录；旧记录仍按其实际证据判断。

备忘录有两种不同覆盖情况：最终独立 verifier 拒绝返回 `verification.model` 的 Work、route、envelope、invocation 引用，适合强重读；`draft_group` 解析/引用拒绝、`authority_validation_failed`、`deterministic_gate_failed` 分支则通常仅保留 reason/checks/cost，未保留 producer calls。备忘录这些失败均未接入 planner company state。META 的具体现场记录仍须用 exact run 核定；此报告不把源码可能路径当作已验证 META 的实际分支。

## 可复用入口

- `investment_memo_quiesce.settled_call`：现有只读强核入口，检查 exact Work、formal/envelope、route 以及所有预算 admission/settlement，不构造写库 authority。复用时应明确其适用目的与冻结配置，不借“已结算”扩大权限。
- `dossier_repair_feedback` 的 owner-only、非 symlink、有界文件和稳定读取模式，可用于票据/诊断文件安全读取；不能复用其成功票据判定来错误认证失败产品。
- 现有公司 CORE 投影与 `_material_subject` 是 planner 可见性和自然唤醒入口，需要同时接线；仅增加 UI 文案不能闭环。

## 最小兼容设计

新增有界、版本化产品核验诊断 receipt，保留现有 summary、失败状态、已发布旧产物及费用。每条 receipt 绑定 company、mission、原 contract、冻结 pack/input hash、producer/verifier 的 exact Work/formal/envelope/route/invocation，以及 finding code/section/ref 和完整 finding hash。

读取端重新验证这些引用。确定性拒绝必须按当次冻结输入与正式输出重算，不能用当前材料重新解释旧失败；原来源/引用无法重放时明确不可验证。模型 verifier 拒绝必须重核正式 JSON、`verified_body_hash` 和 producer-route 绑定。单纯文件 hash、模型生成摘要或机器 reason 文本不能代替此证明。

旧 summary 不能自认证。仅当原 request/prompt、pack/material、contract 能与历史正式 Work 精确重建并强核时，可只读派生旧失败反馈；缺证据时单列 `unverified`，不编造调用引用、不补假成功、不允许据此开付费恢复窗口。

每公司最多保留初筛和备忘录各一条最新有效失败、每条至多 8 个 compact findings。完整内容以 hash 和 omitted count/hash 留审计，避免把整段模型输出塞回 planner。完成状态与缺项保持分离。

反馈身份采用 company、lane、contract、冻结有效输入、具体稳定 findings 的语义 hash。ticket ID、完成时间、普通轮询次数仅作 provenance，不应每次变更都唤醒收费规划。相同输入与相同失败跨票据重复不制造新实质状态；真实输入/缺因变化，或强验证完成清除旧失败，才改变实质规划状态。该逻辑需接入公司 CORE、压缩投影和 material pacing，保持现有字节限额与公司、来源、目标绑定。

反馈明确是待处理的产品核验诊断，**不是 Evidence、研究答案、重试授权或预算扩张**。planner 可据此调整下一项正常研究/引用任务；不自动重派模型，不清 hold，不退款，不改未知费用或现有最多修复次数。

## 验收边界

1. 用 AMZN 现存失败输入及三条正式调用做离线重放，五类缺因能进入最终有界 prompt；不能只验证 summary 被复制。
2. 覆盖 memo 最终 verifier reject、producer 解析/引用失败、authority/deterministic gate 分支；缺绑定的旧记录明确不可验证。
3. 篡改 finding、冻结输入、Work/envelope/route、来源引用，以及 formal 不完整时，不能认证反馈。
4. 未结算或费用不确定仍遵循现有账务门；反馈不创造任何新 paid retry。
5. 同语义失败跨 ticket/time 不重复唤醒；强完成/真实输入变化按规则更新，旧产品和旧费用原样保留。
6. 真实多公司投影维持现有限额，保留所有公司/source/target 权限身份、具体缺因及 omission hashes。

## 私有证据

- `/tmp/ar-product-validation-feedback-gap-exact-ro-r01-20261011.json`
  - SHA256 `dbcb92a53dedd8912d9d50291eb1a5a1ff563b1020705bb43b1bfdb3fd035100`
  - exact AMZN ticket/summary hash、15 findings、正式 planner Work/formal/hash、五类 code 在实际 prompt 中全部缺失；query_only、0 changes。
- `/tmp/ar-product-validation-feedback-next-batch-proposal-r01-20261011.json`
  - SHA256 `16e05b8b7b3a10d46fa4eac88cb218a0f40f0b4093852949d2527ed0dd2f9ce6`

上述初筛实现已通过 866 项整批测试和隔离回放，现在线上观察新任务的实际采用。备忘录的各类失败反馈、缺少冻结上下文的旧记录如何补足证明，以及规划是否据此完成后续修复，分别保留为待办。
