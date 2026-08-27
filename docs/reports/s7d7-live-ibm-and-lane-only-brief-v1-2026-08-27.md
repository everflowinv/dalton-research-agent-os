# S7d-7：IBM live SEC lane 与 lane-only industry brief v1

## 结论

owner 批准的两项变更都已完成：SEC Company Facts 单次响应上限以追加式 authority 从 5 MiB 升到 8 MiB；US IT Services brief 只使用 SEC lane 产生的 `quarterly_revenue_yoy_growth` 正式 Claim。IBM 已在 live 自动提交，四家公司 brief 已发布并可重放。

live Core 当前有 5 条正式 Claim / 5 条 Evidence：其中 4 条是 policy-2 自动提交的 SEC quantitative Claim，1 条是 owner 人工接受的 ACN transcript qualitative Claim。不能把这个总数写成 5 条 policy 自动提交 SEC Claim。

## 8 MiB authority 与部署

- 旧 budget v1 保留为 5,242,880 bytes，用于完整重验 ACN、EPAM、CTSH 的不可变历史 plan。
- 新 budget v2 为 8,388,608 bytes；新 plan 使用独立 profile v2、zero-price authority、rate-policy 和 runner environment ref。两个版本继续共用 SEC quota scope。
- 部署前以新代码只读打开 live Core，三条旧 SEC plan 均按 5 MiB 完成 exact revalidation。
- 真实 SEC 演练先在 live SQLite 一致性副本完成；随后 `ab894ee` 于 2026-08-27 06:31 UTC 部署 live。install health 返回 `ok=true / state=running`，Agenda 重启后的 cycle 正常交付。
- 直接匿名请求 Cockpit `/health` 返回 403，是 Tailscale 身份访问控制，不是服务异常。

## IBM live 结果

- ticket：`sec-lane-run:2a6c518b28cdf11987ba1629`
- 运行：2026-08-27 06:31:56–06:32:12 UTC，exit 0。
- filing：IBM 10-Q `0000051143-26-000078`，filed 2026-07-23。
- observation：Q2 2026 Revenues USD 17,162.0M，去年同期 USD 16,977.0M，同比 +1.09%，期间 2026-04-01..2026-06-30。
- source verification / numeric verification：pass；policy-2 自动提交；closure replay 为 duplicate。
- Claim：`claim-version:44332d104e23e7563bfae9dcd9f7b4740ac7c6c749915787265000db53495d67`，hash `fc6e75dfb1ca7a26e70274487f5a0340a6fd3271138c89eca5ab185cff6637ea`。
- Evidence：`evidence-version:6ce3cb1830305a2527038c8769096bc93f5f31cb4b5838ac14e56829f38b0b5d`，hash `18976af6ab1293f5aabdf1a06ad516d1afb98dbff9329f204235ed83ea2a7a9d`。
- relation：`relation:reviewed:7dc270eda5afa61b3732e2ca2911d34deb9db8fddb1892bea7d26553c29e4a8b`，hash `17d9d378dd643ffe33cf639bf4bb1da8c45561591aa879afab206d1f00ea66a4`。

## Lane-only brief live authority

发布输入为 `deploy/coverage/us-it-services-industry-evidence-v4.json`，对应 commit `2cdcb9e`。口径只保留一个 driver：

- driver：`driver:revenue-growth-usd-gaap`
- formal binding key：`quarterly_revenue_yoy_growth`
- companies：ACN、CTSH、EPAM、IBM
- 不含 ACN transcript Claim，也不含隔离 canary 手工整理的 8-K exhibit KPI。

live 首次发布的 authority：

- driver pack：`driver-pack-version:us-it-services:live-sec-lane-v1`，hash `3eeb852920f69c33dc76bcacb909529bd0b2440a36cbed4200665d6378d27e9d`。
- evidence pack：`industry-evidence-pack-version:us-it-services:live-sec-lane-v1`，hash `5e4dd0a8c6ccc6c5e16ebbbb44f01494d89a6c35a9fc2bc9438cb77de2536c8b`。
- company overlays：ACN / CTSH / EPAM / IBM 各一份 v1；四个 driver view 都引用本公司的正式 SEC Claim。

## 验证

- S7d-5 定向与关联回归 183 项通过；main 合并后的快速超集 51/51；`compileall`、`git diff --check` 通过。
- manifest 中 4 条 Claim 和 4 条 relation 的 ref/hash 均与 live Core 逐项一致。
- fresh live 副本 canary：driver pack v1、evidence pack v1、4 个 overlay v1 全部注册；4 Claim / 4 source / 1 driver / 4 KPI cell；Markdown 9,279 bytes，重放逐字节一致。
- live writer 连续渲染两次，结果逐字节一致：render hash `c37a8482522285cbf2800242fd818ec299193d77f23ecf3c56b0ef34613c1714`；snapshot hash `1b6b2586fdc3d9b16b02a58f58f569fac2212366af01ac1c58f522eda20c9105`；integrity `ok=true`、issues 为空。
- live authority 计数：1 个 driver pack、1 个 industry evidence pack、4 个 overlay；pointer 均指向上述 live v1。

## 下一步

- 四家公司计划内 universe 已完成。若 Phase 7 的硬门槛仍严格要求“至少 5 条 policy 自动提交 SEC Claim”，还差第 5 家 issuer；不能用 ACN transcript Claim 充数。候选可沿用已核过 filing 的 DXC。
- S7e 应把 live industry brief 接入每周交付，并新增内容反馈的 exact authority binding；现有 `record_agenda_feedback` 只能绑定 Agenda decision，不能记录 brief 内容反馈。
- writer 仍缺 per-request latency / ticket creation 日志；之前重启窗口和长 lane 期间出现过客户端超时但无法回溯，S7e 前应补最小观测。

