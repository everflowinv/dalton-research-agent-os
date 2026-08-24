# ACN 研究轨迹只读投影 v0.1

日期：2026-08-24

## 结论

Dalton Cockpit 已增加第一条 ACN Q3 FY2026 研究轨迹。轨迹由已验证的 transcript review packet、AlphaEngine source manifest、Core correction/citation state 和 candidate staging state 即时重建，不建新表、不写新 authority，也没有对应的 POST 接口。

真实 ACN packet 已渲染为 11 个 research-event 节点：

1. Agenda；
2. 研究问题；
3. Plan / Work Order；
4. AlphaEngine connector；
5. raw transcript artifact；
6. transcript correction review；
7. Claim citation binding；
8. 候选 Evidence / Claim；
9. 人工 Claim review；
10. 正式 Evidence / Claim；
11. US IT Services brief v3。

这条 acquire-only canary 没有 Agenda、PlanRound、WorkOrder 或 WorkflowRun authority。投影把这些节点明确标为 `unrecorded`，没有从采集结果反推或补造上游事件。

## 投影边界

- 每个节点至少绑定一个 exact ref/hash；connector 节点继续绑定每页 connector invocation、raw page artifact 和 SourceEnvelope。
- packet 内的 research target、proposed correction set 和 candidate Claim 只标为 `projection fragment`，不能冒充正式 authority。
- projection 自身有确定性 SHA-256，但 `projection_only=true`、`admission_effect=false`。
- 修改返回 JSON 不会改变下一次重建；transcript admission 仍重新读取 packet/manifest，并校验 exact packet hash。
- `/v1/research-trajectory` 只有 GET。对该路径发 POST 返回 404。
- Cockpit 继续不持有 Core 数据库路径；现有 scoped writer principal 和 human governance gate 未扩大。

## ACN 真实样本复验

- packet：`transcript-review-packet:acn:q3fy26:1`
- AlphaEngine document：`alphaengine-doc:130000095976806`
- 原文：51,034 字，2 页
- 当前投影状态：`awaiting_transcript_review`
- 当前已完成节点：AlphaEngine connector、raw transcript artifact
- 当前阻断：authenticated human correction review、citation binding、candidate staging、Claim review、正式 Ledger、brief v3
- 正式 Evidence / Claim / Thesis 写入：0
- production pointer：关闭

本轮没有执行人工 correction review，没有代填 `human:*` actor，也没有部署 live `:8793`。

## 验证

- 关联回归：80/80 通过；
- 真实 ACN packet/manifest validation：通过；
- 真实 ACN 轨迹：11 个节点，2 页、51,034 字，状态与当前 admission gate 一致；
- Cockpit inline JavaScript 语法：通过；
- `compileall`：通过；
- `git diff --check`：通过。

本轮没有重跑全仓慢回归。仓库此前的 `thesis_impact_control` connector inventory `canonical_json` 热点仍未处理。当前 venv 没有安装 `pyflakes`，因此该项未执行。

## 下一步

S3 开始前先冻结自然语言 intent taxonomy 和 sufficiency/freshness closed contract。随后接入 composer；模型只提交 context-bound typed candidate，任何 directive、priority、question draft 或 approval 仍由 Core 复核并分派。
