# Doctrine 与 Planner ContextPack v1 实施报告

日期：2026-08-23
状态：development candidate；未部署 live

## 裁决

此前的总体方案继续有效：开放式研究方法、人类给出的研究方向和研究中的临时修正需要 LLM 做语义理解，但 LLM 只能提交受限 proposal，不能修改 scope、能力、参数、权限、预算、证据事实或 Claim 准入规则。

本轮把这条边界做成了可执行合同：

`human DoctrinePack / override → exact PlannerContextPack → deterministic proposal → Core admission`

Doctrine 只回答“本轮优先看什么、偏好哪些来源、期望多少独立来源”。它不是 Evidence、Claim 或 Thesis，也不能把一次 miss 写成“不存在”。

## 新增 authority

- `DoctrinePackVersion`：human-only、不可变、可继续版本链。每个 lens 冻结 objective、`priority_topics` 和 evidence preference；负面 Claim 规则固定为 `candidate_only_until_separate_claim_admission`，人类和模型都不能用 Doctrine 降低 Core gate。
- `DoctrineOverrideVersion`：human-only，绑定 exact DoctrinePack ref/hash、exact loop ref/hash、一个 pack 内已有 lens 和明确的 `effective_from/effective_until`。同一 loop 同一时点出现多个 active override 时 fail closed；过期或已撤销 override 不再生效。
- `PlannerContextPackVersion`：由 Core 机器生成，冻结本轮 exact ResearchQuestion、Doctrine、选中 lens、可选 override、可选 Industry Driver Pack、可选 ThesisVersion、历史 ResearchOutcome、已生效 directive、剩余预算和已批准 ProbeTemplate catalog。正文都放在 `quoted_data` 中，不是可执行 prompt。
- `PlannerProposalVersion 0.2`：在 0.1 基础上新增 exact PlannerContextPack ref/hash，并固定 doctrine-aware deterministic planner ref/hash。旧的 0.1 proposal 与 legacy planner 保持可用。

现有通用 `ContextPack 0.1` 没有扩字段。它服务 connector/research coordinator，输入类型、截断和 materializer 已有另一套闭合语义；本轮新增 Planner 专用 ContextPack，避免用一次 schema 扩展同时改变两个消费者。

## Planner 的实际消费

doctrine-aware reference planner 的顺序固定为：

1. human directive：`request_replan` / `deprioritize` / `focus_coverage_item`；
2. selected lens 的 `priority_topics`；
3. immutable loop 原有 checklist 顺序；
4. coverage 完成后进入既有 closed terminal candidate。

lens 只能对尚未完成、且已经在 loop catalog 内的 coverage item 重排。它不能增加 topic 对应的 probe，不能改 template 的 source、operation、参数、permission、side effects 或 cost，也不能扩预算。ContextPack 形成后如新增 directive、产生 Outcome 或消耗预算，旧 ContextPack 会因 stale round state 被 Planner 和 Core admission 同时拒绝。

## 同一问题的路径差异

专项测试使用同一个 ResearchQuestion、同一组三个 SEC read-only ProbeTemplate 和同一预算：

- `短期催化` lens 首轮选择 `capital-lease-keyword`；
- 限时 `资产负债表防御` override 首轮选择 `commitments`。

两条 proposal 的 question input 和 catalog input 完全相同，差异只来自 exact lens binding。Core 对两条 proposal 继续执行同一 permission、parameter、budget 和 WorkOrder gate。

## Driver Pack 与 Thesis

PlannerContextPack 可选绑定：

- exact `IndustryDriverPackVersion` ref/hash，并复核 canonical record；
- exact `ThesisVersion` ref/hash，并复核 version、authority kind/ref、committer 和 content binding。

这两类输入都是 quoted authority data。Planner 可以根据它们决定下一项已批准 probe，但不能把 Driver Pack 或 Thesis 的语句当成事实，也不能修改 Thesis current pointer。

## Schema 与迁移

本轮只增加 `doctrine_pack_versions`、`doctrine_override_versions`、`planner_context_pack_versions` 三张 append-only 表和对应 writer/immutability trigger；没有修改历史表或历史记录。旧数据库初始化新 authority 时只执行 `CREATE TABLE IF NOT EXISTS`，旧 Bounded Planner Loop 0.1 不需要升级或重写。

## 本地验证

Doctrine 专项 9/9 通过，覆盖：

- 同一问题和 catalog 在两个 lens 下生成不同首轮 proposal；
- 限时 override 过期后回到 pack default；
- ContextPack 生成后新增 human directive 会使旧 pack stale，刷新后 directive 优先；
- ContextPack 生成后新增同一时点生效的 doctrine override 也会使旧 pack stale；
- Doctrine 无法扩 catalog、permission、source、参数或降低负面 Claim gate；
- 非 human authority、直接 update 和 append-only 篡改 fail closed；
- exact Driver Pack 与 human-admitted Thesis 进入 quoted input；
- pack/context replay 幂等；
- legacy 0.1 planner/proposal 保持兼容。

contracts、Bounded Planner、Coverage Admission、Context materializer、Research Coordinator、Scheduler 和 Observability 关联回归 95/95 通过。最终 exact-authority revalidation 补强前，全仓回归 732/732 通过；补强后专项与关联回归、`compileall`、全部 JSON schema 解析、sdist/wheel 和 wheel-only import 均通过，最终代码的全仓矩阵交给同一提交的独立 CI。完整回归出现仓库原有的 SQLite `ResourceWarning`，但测试退出码为 0。

## 下一笔

下一笔是 LLM Research Planner：模型读取本轮 exact PlannerContextPack，只能输出 `PlannerProposalVersion 0.2` 的 candidate action；Core 继续复核 context freshness、catalog、参数、权限、重复 probe、预算和 terminal gate。deterministic planner 保留为 fallback 和 replay oracle。

StatementSnapshot v1 与 AlphaEngine TranscriptPolishWorker 继续排在 LLM Planner 之后，并接入同一个 bounded loop。本轮没有真实模型、connector、付费调用、Ledger 写入或 live 部署。
