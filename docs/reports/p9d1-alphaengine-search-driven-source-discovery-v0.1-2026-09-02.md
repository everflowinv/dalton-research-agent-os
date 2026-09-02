# P9d-1：AlphaEngine search 驱动的 mission 来源发现 v0.1

日期：2026-09-02
状态：development candidate；专项与 live Core 只读副本 canary 通过；**未部署、未写 live**
上游：[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)、[ADR-0003](../adr/0003-transcript-candidate-admission.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)、[M1 / AE-1](m1-model-engine-and-ae-probe-v0.1-2026-09-01.md)

## 这一片解决什么

AE-1 只能按已知 `alphaengine-doc:<id>` 获取原文，不能自己找资料。P9d-1 接上 AlphaEngine `search_library`：
CoverageMission 按 owner 写好的 DiscoveryPlan，定期为 universe 内公司搜索财报电话会和卖方报告，把搜索调用、命中文档、
后续原文获取全部写进 Core authority。自动化不能临时发明 query、扩大日期或文档类型；每条记录都绑定 exact mission
版本、plan hash、connector invocation 和 source envelope。

这一片只完成「发现 → 获取原文」。它不把搜索摘要当事实，也不自动把 transcript 变成 Claim。原文进入既有 connector
authority 后，仍由 transcript candidate / Cockpit 人工 accept 路径处理，ADR-0003 B 不变。

## 冻结边界

- `MissionDiscoveryPlan 0.1` 是 closed-shape、带 `created_at` 和 content hash 的 manifest。首个 plan 覆盖 ACN、CTSH、
  EPAM、IBM、DXC；每家公司有固定 search terms；两条 spec 分别搜 `meeting_minutes`（回看 400 天）和
  `sell_side_report`（回看 180 天），成功后 7 天再搜，失败后 1 天再试。
- `source_discovery` 加入 ADR-0004 的 `autonomy.may_write` 冻结词表。automation 只有在 active mission 同时满足
  `source:alphaengine=connected`、`source_discovery`、`observation` 时才能运行；human 可在 `probe_only` 下排练。
- search capability 与 get_document capability 各有自己的 governance record、schema hash、descriptor、profile、rate
  policy 和 launcher slot。两者的调用数合并计入 mission 与 owner 的 24 小时上限，实际 cap 取两者较小值；运行中的
  child 先按 reserved call 占位，避免并发超额。
- controller 每 tick 先 settle 已结束 child，再优先获取最老的已发现文档，然后最多启动一条新搜索。cadence、预算、
  mission 拒绝、launcher busy、child 失败都显式写进 tick 结果或账本，不静默重试。
- `coverage_mission_source_discoveries` 是 append-only；`coverage_mission_discovery_dispatches` 与
  `coverage_mission_discovered_documents` 只有 authority 可做有限状态迁移，SQL trigger 拒绝直写和删除。

## 实现

- `alphaengine_core_search.py`：Core-hosted `search_library` authority，写入 ConnectorInvocation、usage/cost/quota、原始
  ArtifactVersion 和 SourceEnvelope；只接受冻结字段与映射过的 AlphaEngine document type。
- `alphaengine_search_cli.py` + `AlphaEngineSearchLauncher`：writer 启动的单槽子进程。launcher 在 spawn 前重验 human
  approved governance、plan hash、mission authorization 与 company/spec；child 再从 Core 重算 active mission 授权。
- `mission_source_discovery.py`：plan validator/builder、确定性 query 编译、dispatch settlement、cadence、共享预算和
  search/get_document 协调器。
- `coverage_mission.py`：发现 authorization、dispatch 账本、source discovery 记录、待获取文档状态与 mission progress
  计数。
- writer ops：core-only controller tick `dispatch_mission_source_discovery`；human-only probe
  `run_mission_source_discovery`；ticket/status 与只读投影。`bounded_planner_driver` 每 tick 调一次 coordinator。
- `install.sh` 只在文件不存在时 seed proposed search governance 与 discovery plan；LaunchAgent 始终传入两条路径。
  proposed governance 未经 owner approve 时，launcher 在进程启动前拒绝。
- `build_mission_v2_params.py` 支持重复 `--add-scope` 和 `--set-source-status source:alphaengine=connected`，可把 P9c 与
  P9d 的授权合并进一个 mission 新版本。

## 提交前发现并修复的两个问题

1. live 共用 `catalog.sqlite` 的当前 epoch 已被 SEC capability 推到 2，而 AlphaEngine get_document descriptor 仍绑定
   epoch 1；下一次 acquisition 会在 `prepare` 报 `StaleCatalog`。search/get_document launcher 现各用独立 capability
   catalog，descriptor 与 catalog epoch 同步；canary 保留对旧 shared catalog 的失败复现，防止以后回退。
2. DiscoveryPlan 第一版漏了仓库所有根合同都要求的 `created_at`。全仓合同测试抓到后，已补进 schema、validator、
   builder 和 committed plan，并重算 plan hash。

## 验收

- 专项：mission discovery、writer ops、AlphaEngine acquisition/governance/budget 共 31 项通过；合同专项 24 项通过。
- live Core 只读副本 canary `temp/p9d-canary.json`（`ok=true`）：
  - mission v1 的 AlphaEngine 是 `probe_only` 且没有 `source_discovery`，automation tick 明确拒绝，调用与三类研究
    authority 数量不变；
  - human probe 运行一条真实 launcher/child 链（fake search transport），返回一个 Core 已有文档和一个新文档，
    dispatch settled；v1 下自动获取仍被拒绝；
  - 副本发布 mission v2（加 `source_discovery`，AlphaEngine 改 `connected`）后，10 个 company/spec dispatch 全部
    `succeeded` 并进入 idle；新文档由既有 acquisition child 获取，Core 能从完整/部分 SourceEnvelope 认定原文存在；
  - Claim 6、Evidence 6、Thesis 2，前后不变；`PRAGMA integrity_check=ok`；0 网络、0 付费、0 live 写入。
- 全仓 unittest **1030/1030**；`git diff --check`、compileall、全部 JSON 解析、wheel + sdist 构建及 Python 3.13
  干净 wheel 安装通过。wheel SHA-256 `86e57602e1f0461ec96eed487a4c688c5c06d29e9afafb77cb11cf13a01e43de`。

## 未做与 owner gate

- **未部署**。第一次 install/restart 只会建表、seed proposed governance/plan；search 因治理记录未批准仍 fail closed。
- live 激活需 owner 分两步批准：① `dalton-connector-governance approve` 批准
  `alphaengine-search-library-v1.json`；② 发布 mission 新版本，至少加 `source_discovery` 并把
  `source:alphaengine` 从 `probe_only` 改为 `connected`。可与 P9c 的 `forecast_reconciliation` 合并为同一个 mission v2。
- 真实 AlphaEngine search 尚未调用；当前验证只用 fake transport，因此 query 的命中质量和 AlphaEngine 当前全文额度
  尚未验证。真实 live canary 会花 AlphaEngine 调用额度，必须在 owner gate 后做。
- 搜索命中原文后不会自动 stage transcript candidate；那是 P9d-1 的下一个小片，且必须保持人工 accept 边界。
- web search 与 Guidepoint 尚未接入。Phase 9 顺序保持 web search 先于 Guidepoint。
