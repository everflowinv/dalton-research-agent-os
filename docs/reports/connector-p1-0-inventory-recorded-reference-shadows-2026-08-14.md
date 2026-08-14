# Connector P1-0：十类 Inventory 与 CNINFO/SEC recorded reference shadows

日期：2026-08-14

状态：P1-0a 已获独立 Go；P1-0b hardening candidate 待 committed-tree 增量复核
范围：离线 contract、synthetic/recorded fixture、authority replay；未部署、未访问真实数据源

## 阶段裁决

Fable 5 复核前期架构蓝图、P0-0 至 P0-4a 实现和当前 Connector Fabric 后，确定下一最小阶段为
`P1-0 Complete Connector Inventory + Recorded Public Reference Shadows`：

1. 先冻结完整 connector inventory，避免只接两个来源后再反复迁移 transport/auth/completeness contract；
2. 再让 CNINFO 与 SEC 走完整 recorded authority replay，稳定 official-source pagination、revision 和 provenance；
3. live public transport 继续等待可终止 process boundary 与贯穿 DNS/connect/TLS/redirect/read 的 total deadline；
4. AlphaEngine、Guidepoint、雪球等 host/MCP connector 等待独立 runner wire 和 credential revoke/max_calls
   use-time authority；
5. 第一条只读研究 WorkOrder 前完成 ContextPack、RunState/Checkpoint、ClaimIndex closed contract 和 builder。

## P1-0a：十类 Connector Inventory

Inventory 共十个 profile：

- CNINFO A 股公告；
- SEC filing/attachment/item/facts；
- AlphaEngine；
- X/xreach；
- X/x_search；
- Reddit/last30days keyless；
- Guidepoint；
- Gemini web search；
- public web fetch；
- 雪球 `agent-reach XueqiuChannel`。

X 的枚举与语义搜索、web search 与 fetch 分开建模。雪球 fallback 只允许 `get_hot_stocks` 路由到
`cn-hk-findata xq_hot_rank`，输出必须保留 fallback 的 source/adapter/provenance label。

每个 operation 都有闭合 input/output schema、pagination、completeness、side-effect 和 fixture。Fixture 覆盖
success、empty、partial、schema drift、429、timeout、malformed；分页 operation 另有 pagination；host-auth
profile 另有 permission denied/revoked。所有 proposal 都是 proposal-only，不能生成 lease 或 canary。

Fable 5 多轮 hostile review 先后复现并关闭逐 operation coverage、错误 pagination、graph hash/ref 断链、雪球
fallback scope、auth/boundedness 漂移、unsafe metadata 等缝隙。最终 loader 不再把可重算 content hash 当来源
认证，而是把完整 package graph 与 deterministic build 精确比较。

P1-0a 提交：`976548e`。独立裁决：**Go**。

- 专项：12/12；
- Python：296/296；
- archive wheel：31 个 inventory JSON 与 commit tree 逐字节一致；
- 干净 venv 安装后 `load == build`，十个 profile 完整；
- packaged JSON 敏感材料扫描为空。

## P1-0b：CNINFO/SEC recorded reference shadows

P1-0b 只实现两个 public official-source 的离线参考链：CNINFO `list_announcements` 与 SEC `list_filings`。
Fixture 是 synthetic recorded payload，不发网络请求。Runtime fixture 必须与 packaged deterministic fixture
逐对象一致，并冻结 parent operation、base parameters 和 parent query hash；plan 与 AdapterRequest 显式绑定
selected scenario ref/hash/behavior，adapter 不能靠隐藏 constructor state 切换场景。

每一页都是独立 physical attempt：

`Reservation → AdapterRequest 0.2 → Journal transport barrier → Observation → PhysicalAttempt → Usage → Cost → Settlement → ArtifactVersion`

多页共用一个 logical invocation。AdapterRequest 0.2 在后续页绑定 parent query、上一页 request/observation/
attempt hash、连续 cursor、当前页 parameters 和 page-specific query hash。每页 normalized output 都必须通过
inventory 冻结的 closed output schema。每页 raw object 形成同一 artifact ref 下的连续 version chain，并在
page commit 内独立注册 ArtifactVersion；SourceEnvelope 列出全部 physical attempts 和 record refs，最后一页
为 result attempt。

完整性规则：

- 观察到 terminal cursor 才写 `enumerated + complete/empty`；
- `max_pages` 用尽但 cursor 未终止时写 `partial`；
- revision 只能指向本次 shadow 前面已出现的 record；
- schema drift、429、timeout、malformed 只写 attempt/usage/cost/settlement/ResultEnvelope，不生成 raw artifact
  或 SourceEnvelope；
- 任何成功页没有 finalized raw object 时拒绝完成；
- 本阶段不写 Evidence、Claim、Thesis。

page recovery 覆盖 `reserved`、`transport_started`、`observed`、page artifact 与 page responded barrier：未执行的
reservation 可释放，transport_started 保守记为 indeterminate，observed 使用冻结的 commit context 完成；第二页
spool capacity failure 保留第一页已注册 artifact，并关闭 parent retryable completion。

跨库收敛顺序改为先把 closed response、ResultEnvelope、page receipts、commit context 和 hash 写入 parent
RunnerJournal，再幂等完成 Scheduler。恢复先完成闭合校验并与 immutable authority 交叉核对；受限 Scheduler
reconciliation 从 Scheduler 构造时绑定的 trusted RunnerJournal reader 读取 parent responded event、page receipt
和实际 completion event，caller 不能提交 event hash/time 充当 proof。lease 期内已持久化的完成事实可在 lease
过期后收敛，但 later attempt 已重新 claim 时 fail closed。Coordinator 只持有窄 AuthorityPort，不直接持有
ConnectorStore、Scheduler 或 SQLite connection。

## Hardening candidate 验证

- recorded reference shadow 专项：18/18；
- Runner/transport/inventory/contracts/Scheduler 组合：75/75；
- Python 全量：314/314；
- broker：15/15；
- `compileall`、`git diff --check`：通过。

committed candidates `9599ea8` 与 `c5fcd41` 经 Fable 5 hostile review 先后裁决为 No-Go。第二次裁决复现了
Scheduler 把 caller 提交的 event ref/hash/time 当 proof、可在零 journal 时伪造成功结果的问题。本次第二轮
hardening 改为 constructor-bound trusted journal proof，并新增零 journal 与非闭合 parent completion 回归；
形成新提交后仍需 Fable 5 对 committed tree 增量复核，并完成 deterministic wheel/clean install 和 GitHub CI。
这些检查完成前，本报告不把 P1-0b 写成 Go。

## 仍然 No-Go

- live exporter attach；
- networked CNINFO/SEC shadow 或 canary；
- authenticated MCP/host-tool 调用；
- 真实雪球、AlphaEngine、Guidepoint 数据访问；
- Research WorkOrder 与 Evidence/Claim/Thesis commit；
- 部署或旧 cron cutover。
