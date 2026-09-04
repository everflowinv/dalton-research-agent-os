# P9c/P9d-1 live 激活与首次自主来源发现 v0.1

日期：2026-09-04
状态：已部署并激活；首次真实 AlphaEngine search/discovery/acquisition 链路已跑通；三个 live 暴露的缺陷当日修复（1033/1033）
上游：[P9d-1](p9d1-alphaengine-search-driven-source-discovery-v0.1-2026-09-02.md)、[P9c](p9c-forecast-reconciliation-v0.1-2026-09-02.md)、[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)

## 观测点兑现：9/3 首个自动 weekly brief

Phase 8 留下的 2026-09-03 07:00 ET 窗口已完整自动走通：admission → issue
`weekly-brief-version:9eafec9e9796671cd2ed9fcbae0ef596` → outbox → Discord
`discord:1545026666730889226` → DeliveryReceipt
`weekly-brief-delivery:6e5b88a1f0d5aabaf8b9cd580641b996`（2026-09-03T11:04:22Z，actor core）。
后续每 tick 幂等返回 duplicate，零重复投递。这是 S7f coordinator 激活后的首个无人值守窗口。

## Agenda（万华 shadow）失败根因与修复

9/3、9/4 两个 cycle 均 `model_output_contract_rejected`。根因不是预算：模型输出 token 随输入单调增长
（input 7.2k→output 924；input 14.1k→output 2000+），policy v4 的 `max_output_tokens: 2000`
从 9/3 起稳定截断 JSON。已发布 `agenda-policy-version:phase1-shadow-v5`
（`max_output_tokens` 2000→4000，其余不变；version 8，pointer 已切，`human:lumos`）。
9/4 当日剩余 tick 以同日 cycle_key 幂等 conflict 拒绝（无费用）；9/5 00:05 UTC 起新 cycle 用 v5。
成本影响约 +$0.002/cycle，日预算 $0.5 不变。

附带记录两个非本次修复的坑：①writer RPC 的 `create_agenda_policy` 缺 `effective_until`
键会以 TypeError 落到未映射 generic 错误（调用方必须显式传 null）；②发布未来 `effective_from`
且 activate 会复现 8/26 的指针空窗（本轮 16:00Z 前的 tick 如实报 not found，无费用，按预期自愈）。

## 激活（owner 常设授权下执行）

1. `install.sh` 部署 P9c + P9d-1（writer/controller 重启，seed proposed search governance 与 DiscoveryPlan）。
2. `dalton-connector-governance approve` 批准 `alphaengine-search-library-v1.json`
   （hash 重算 `f6dff246…`，approved_by human:lumos）。
3. `build_mission_v2_params.py` 生成并发布 **CoverageMission v2**
   （`coverage-mission-version:us-it-services:2`，hash `36877f2c…`）：
   may_write 追加 `forecast_reconciliation` + `source_discovery`；`source:alphaengine` → **connected**；
   其余绑定（constitution v2 / p8a mandate / playbook v1）与预算（AE 24h/30 次、日成本 $5、40 次付费调用）不变。

## 首次真实 search 暴露的三个缺陷（当日修复）

1. **SourceEnvelope 的 partial/ranked 配对被拒**（烧掉 3 次真实 search）。
   `live_mcp_connector` 对带 cursor 的 search_library 观察冻结为 `("partial","ranked")`，
   而 `connector.record_source_envelope` 的旧规则按 get_document 分页语义硬性要求
   `status=partial ⟹ completeness=partial`。fake transport canary 的响应没有 cursor，未覆盖该路径。
   真实 ACN 搜索命中超过 max_records → 必带 cursor → 必崩。修复：允许 `partial` 与
   `{"partial","ranked"}` 配对（gate 合同本就如此冻结）；新增回归测试
   `test_source_envelope_partial_pairs_with_ranked_search_completeness`。
2. **weekly brief 渲染不稳定导致每 tick KeyError**。P9c 给 `render_markdown` 加「预测对账」节时
   未按 issue 记录里冻结的 `sections` 门控：9/3 旧 issue（6 节）重渲染长出第 7 节 → body hash 变 →
   outbox 幂等 conflict → `outbox["message_id"]` KeyError → 心跳 weekly_brief error。
   修复：该节只在冻结 `sections` 含「预测对账」时渲染；旧 issue 重渲染恢复**逐字节一致**
   （9/3 issue 重渲染 sha256 = 投递 artifact `4c8266c6…`，已验证）；coordinator 对非重放形状
   改抛类型化 `WeeklyBriefCoordinatorError`。新增回归测试
   `test_render_omits_reconciliation_section_for_pre_p9c_issues`。
3. **`acquisition_failed` 文档永久卡死**。`next_discovered_document` 只挑 `discovered`；本轮两次
   部署重启把在飞 acquisition child 杀成 orphaned（exit None），该文档从此不再重试。
   修复：`retryable_failed_document(older_than, as_of)` + `mark_failed_document_retry_launched`
   （authority 状态迁移 `acquisition_failed → acquisition_launched`，清 failure_reason 换新 ticket）；
   coordinator 在无新文档时按 `ACQUISITION_RETRY_INTERVAL = 1 天` 有界重试，新文档永远优先。
   新增回归测试 `test_failed_acquisition_is_retried_after_interval`。

测试基建顺带修复：`test_mission_source_discovery` 的冻结时钟（2026-09-02）与 authority 真实
`_now()` 锚点耦合，日期过了 09-02 就红（cadence 断言差 2 天）；测试时钟改为从真实 now 起算。

## 首次自主发现链的真实结果（2026-09-04 15:52–17:0x UTC）

- **search**：修复部署后每个 company/spec dispatch 真实调用 AlphaEngine `search_library` 并成功 settle；
  单次命中最多 **20 个文档**（meeting_minutes 400 天回看 / sell_side_report 180 天回看）。
- **发现账本**：`coverage_mission_source_discoveries` / `coverage_mission_discovered_documents` 当日累计
  **92 个 discovered + 1 acquired + 2 acquisition_failed（部署孤儿，明日起自动重试）+ 1 in flight**。
  首个完整获取文档 `alphaengine-doc:320000610044534`（CTSH）已进 connector authority。
- **预算**：当日 AE 共享计数 18/30（3 次烧在缺陷 1、其余为成功 search 与 acquisition）；超限拒绝与
  失败 cadence（当日 1 天重试 / 成功 7 天重搜）均如实记录在 tick 结果。
- **P9c 对账**：mission v2 已授予 `forecast_reconciliation`，tick 对账块 idle（ACN Q4 FY2026 实际数
  10/1 10-K 落库后自动触发），weekly brief「预测对账」节待下一期 issue 首次出现。

## 验收

- 全仓 unittest **1033/1033**（新增 3：envelope ranked 配对、pre-P9c 渲染字节稳定、获取失败重试）。
- live：weekly_brief ready（重放全链 duplicate）；bounded_planner idle；outbox/backup ready；
  degraded 仅剩 agenda 同日幂等 conflict（预期，9/5 自愈）。
- 修复验证均在 live Core 只读副本上先行复现与确认（agenda v5 参数、brief 逐字节重渲染、cycle 重放）。

## 未做与下一步

- **P9d-2**：发现文档 → 自动 stage 语义候选（保持 ADR-0003 B 人工 accept 与 correction/citation
  human-only 链），需要独立设计片。
- 92 个已发现文档将在预算内逐个获取（每 tick ≤1）；2 个孤儿明天重试。
- 周报「预测对账」节、mission 进度投影里的新文档可见性待下一期窗口与 Cockpit 检查。
- M2 市场数据 connector 仍冻结待 owner 解冻；web search 源在 Guidepoint 之前。
