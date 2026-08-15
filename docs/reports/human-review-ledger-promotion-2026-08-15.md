# Human Review 与无损 Ledger promotion

日期：2026-08-15

状态：隔离开发候选已跑通；未部署，未接 Agenda 或现有 cron

## 结果

本轮接受独立复审的主判断：下一阶段要为已有 authority 骨架接消费者，不再横向扩 connector。
实施顺序已冻结为 Human Review → 索引/检索 → Question Backlog → Planner → Interrupt →
Reflection。完整方向见 `architecture-review-and-next-phase-v0.3-2026-08-15.md`。

Slice 1 已增加 HumanReviewAuthority 和 Tailscale/CSRF HTML 入口。每个 exact candidate version 只能收到
一个 `accept / revise / reject` 终态决定。只有经过 Tailscale 身份派生的显式 human accept 会生成
durable commit intent；timeout、Agenda agree、automation principal 和 Discord reaction 都不能授权入库。

复审报告没有指出 candidate 与旧 Ledger 0.1 不是无损同构。CandidateClaim 使用 canonical Decimal
string、currency/scale 和结构化 period；CandidateEvidence 还保留 SourceEnvelope 与 source-verification
hash。因此实现新增 additive EvidenceVersion/ClaimVersion 0.2，没有把 candidate 硬塞进 0.1 丢精度或
审计链。

Core writer 从正式 SourceEnvelope 反查 producer ExecutionInvocation，并重验 exact Artifact ref/hash；调用方
不能自报 producer。EvidenceVersion、ClaimVersion、`supports` relation 和 promotion receipt 在同一个
SQLite 事务提交。接受后的 Claim 初始状态仍是 `proposed`；人工语义核对不等于独立交叉验证。

## 安全与故障语义

- review service 只持有 `commit_reviewed_candidate` scoped writer token，不拿 Core DB path；
- candidate/ref/hash、审阅语义、reviewer 身份、SourceEnvelope、Artifact 和 producer execution 全部重验；
- accept 与 commit intent 同事务写入 review authority，writer 失败后留下 append-only failed event 并可重试；
- formal commit 的四个故障缝隙均会整体回滚，不留部分 Evidence、Claim、relation 或 receipt；
- recorded fixture 不能进 formal Ledger；connector producer 也不能冒充 ModelInvocation 通过 adjudication。

## 验证

- review/writer/packaging 专项：22/22 通过；
- Python 全量：380/380 通过；OpenClaw broker：15/15 通过；
- `compileall`、91 份 contract JSON 解析、15 份 packaged SQL、HTML JavaScript 语法和 `git diff --check`
  通过；
- Python 3.13 两次 wheel 逐位一致，SHA-256 均为
  `2ea5d57833df079c03a61f600c42cf3c10c4cb781137b095748c1596058f7553`，580,346 bytes；
- wheel 在干净 venv 安装成功，`pip check` 无冲突，新模块、review SQL 和 HTML 均可从 package 读取；
- HTML 入口已用本地 HTTP 服务做视觉检查；数值、来源、审阅状态和三种决定入口均可辨认。

实现提交：`4aacbcc`。GitHub CI `31882476054` 的 Python 3.11、Python 3.13 和 broker 三个
job 全部通过：<https://github.com/everflowinv/dalton-research-agent-os/actions/runs/31882476054>。

## 未完成项

- 代码没有部署，没有 live staging/review authority，也没有修改 Agenda 或关闭旧 cron；
- `revise` 已留下不可变审阅决定，但根据修订意见生成下一 candidate version 的 workflow 还未实现；
- ClaimIndex 仍接收 caller-provided status，不允许在修复前为 Planner 提供生产上下文；
- DocumentIndex FTS、ContextPack materializer、Question Backlog、Planner、Interrupt 和 Reflection 按冻结顺序后续实现。

## 下一步

先修 ClaimIndex status 派生，使索引只能从 exact Ledger snapshot 得到 claim 状态。然后将
DocumentIndex FTS 和 ContextPack materializer 分成两笔可独立验收的提交。
