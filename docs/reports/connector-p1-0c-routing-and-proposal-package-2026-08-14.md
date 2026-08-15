# Connector P1-0c：轻量路由与可扩展 proposal package

日期：2026-08-14

状态：本地候选验收通过，待 Claude Fable 5 committed-tree 增量复核
范围：离线 contract/package 验证；未部署、未访问真实数据源、未授予执行权限

## 架构裁决

Claude Fable 5 复核最早愿景、当前实现和 connector 扩展成本后，收敛为两条规则：

1. Dalton 不为每个 physical call 建语义 Router。Planner 在 WorkOrder/协调器边界一次冻结 source、operation、
   parameters、completeness 和 fallback；Runner 每次调用只做本地确定性的 use-time authority gate。
2. `findata` 一类多数据能力是 research recipe。同一稳定 source/transport/auth boundary 可在一个 connector 中提供
   多个 operation；跨 source、auth 或 provenance boundary 的步骤拆成多个 connector，由 recipe/coordinator 编排。

现阶段不提前实现 `CompiledConnectorPlan`。它等 P2 coordinator 有真实消费者时再落地，避免先建无人使用的
route authority。只有首条 live connector 实测每页 admission p50 超过 100ms、占总耗时超过 10%，或 resolver
变成跨进程 RPC，才优化 Runner 当前的重复 validation。

## 实现

新增 `load_connector_proposal_package(root)`，接受恰好三份 data-only JSON：

- `profile.json`；
- `fixture.json`；
- `proposal.json`。

loader 复用闭合 profile/fixture/proposal validator，但不要求新 connector 出现在冻结的十类
`PROFILE_DEFINITIONS`。它精确绑定 created time、source、transport target/host policy、auth boundary、
operation schema/pagination/completeness、fixture ref/hash、proposal ref/hash 和 offline promotion policy。

这条路径只接受 `inventory_connected + proposal_only + requested_canary=null`，operation 只允许
`read:recorded-fixture`，fixture 必须为 synthetic。它不导入 adapter code，不注册 Catalog，不生成 lease，
不改变 frozen ten-profile loader。

## 本地候选验收

- 新 synthetic connector 不进入 `PROFILE_DEFINITIONS`，仍能通过三文件 package 验证；
- frozen ten-profile inventory 在加载新 package 前后逐对象相同；
- lease escalation、schema hash fork、非 synthetic fixture、credential-shaped builder ref、adapter graph fork、
  额外文件均 fail closed；
- connector inventory 专项：14/14；
- Python 全量：319/319；
- `git diff --check`：通过。

## 保持 No-Go

- proposal 自动 promotion 或 Catalog publish；
- 动态 import/exec proposal adapter；
- networked canary、authenticated MCP/host-tool；
- 真实 CNINFO、SEC、雪球、AlphaEngine、Guidepoint 调用；
- Research WorkOrder 与 Evidence/Claim/Thesis commit；
- 部署或旧 cron cutover。
