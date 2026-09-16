# Dalton Research OS 线上运行与研究推进审计（2026-09-16）

审计时点：2026-09-16 15:20–16:00 UTC。全程只读（SQLite 一律 `mode=ro`），未修改任何文件、数据库、launchd 服务，未重启进程。
由 4 个并行子代理分别审计运行时健康、研究内容进展、模型路由与预算、调度吞吐，主代理交叉核对后汇总。

## 0. 结论

**系统在跑，但研究没有在推进。** 五家公司（ACN/CTSH/EPAM/IBM/DXC）全部停在第 1 阶段（Initial Screen）出口之后、第 2 阶段（Deep Insight Gate）入口之前，已停 3 天；09-14T20:14 之后 43 小时内没有产出任何一份正式研究交付物新版本（唯一例外是一条 event_note）。

三类根因叠加：

1. **人类裁决缺位**：5 份深度认知门草稿等待 owner 裁决（`deep_insight_gate_decisions` 0 行），门车道 57/57 次 idle。这是阶段阶梯的唯一阀门。
2. **机器侧写不进**：dossier 今日 49 次运行、正式写入 0 次（裸 ValueError 崩溃 + 起草模型违反 schema）；debate map 冻结 4 天；planner 401 轮只对应 8 个不同状态、正式写入 0 次。
3. **基础设施在退化**：writer 单线程队列被"注定失败的 lane"每 tick 独占 60 秒，tick 耗时 21s → 188s，6–11 天后调度层追尾；磁盘只剩 3.6 GiB（99%）；预算账本 09-16 00:00–06:00 UTC 在零次成功调用的情况下记了 286 USD 花费。

真实模型开销极低（抽取链 48h 合计 0.78 USD），**钱和配额都不是瓶颈**。

## 1. 紧急（今天）

| # | 事项 | 证据 | 动作 |
|---|---|---|---|
| 1 | **磁盘 99%，余 3.6 GiB**；每日备份约 2.1 GB 将在 17:25 UTC 触发；发布节奏约 1 GB/小时 | `df -h /System/Volumes/Data`；`~/Projects/dalton-owner-activation-20260910/workspace-runtime-release-20260914/` 12 份回滚快照约 24 GB（仅 `legacy-upgrade-20260915l` 被 current-release.json 引用）；`~/.dalton/runtime/releases` 38 个 venv 14 GB，其中 30 个未被任何 plist / manager.json 引用（11.3 GB） | 先 `lsof +D` 校验，再删除未引用的回滚快照 a–k 与未引用 release venv（保留最近 3 个）。不要对 core/scheduler 做 VACUUM（freelist=0，回收不到空间且需等量临时空间） |
| 2 | **gpt-6-astra 已 44 小时 100% HTTP 429，仍在 5 条 brain 链里，且 429 失败按预留上限计费** | `model_route_chain_links`：最后成功 09-14T19:58，此后 574 次 provider_failure；`thesis_impact_day_settlements` 09-16 plan lane 00–06 UTC 286.17 USD、0 次成功；06:00 后仍有 4 笔整额扣款 | 配置：从 planner-decisions / event-judgement / company-dossier / deliverable-drafting / extraction 五条 policy 的 `fallback_chains.tiers.brain` 与 `allowed_profile_ids` 移除 `profile:gpt-6-astra`；owner 核查 `credential-slot:openclaw:openai` 状态。代码：`cockpit_model.py:2360` 把 `RATE_LIMITED` + `dispatch_proof.state=provider_completed_failure` + 零 token 归为可证零成本，不按 ceiling 结算；`:2305–2312` retry 分支同理 |
| 3 | **brain 链第 4 位是死配置**：`model-profile:claude-opus-5` 只有 8 月的 v1（legacy），当前有效 id 是 `profile:claude-opus-5` v5 | `model_endpoint_profile_versions`；policy v43/v44/v50 的 brain 链；route decision 里 `model-profile-version:claude-opus-5:1` 仅 `availability_expired`，`broker-claude-opus-5…:5` 被判 `profile_not_in_tier_chain` 192 次 | 五条 policy 中把 `model-profile:claude-opus-5` 改为 `profile:claude-opus-5`。修完后 brain 档才有除 deepseek-v4-flash 以外真正可用的回退 |
| 4 | **裁决 5 份 Deep Insight Gate 草稿** | `deep_insight_gate_versions` 5 行 v1，decisions 0 行；ACN/EPAM q1 = `insufficient_evidence`；IBM/CTSH 12 题中 9 题 unknown、evidence_refs 仅 26–29 | owner 动作。建议 IBM/CTSH 直接退回；ACN/EPAM 按 README 规则 q1 不成立应退回重拟；DXC 可裁决。同时批量关闭 47 条过期 `gate_reopen_proposals`（全部 `evidence_thicker`，指向已被 09-14 新版取代的 passed_version_ref） |
| 5 | **`projection_min_interval_seconds` 2 → 60** | `dashboard_projector.py:345` 每轮 `SELECT * FROM scheduler_work_orders`（660 MB，实测 4.37s）+ formal_results + model_invocations 无索引排序，总计 ≥ 7.5s，却允许每 2s 跑一次；controller 长期 `U` 态；core.sqlite-wal 75 MB 无法 checkpoint（阈值 4 MB） | 改 `~/Library/Application Support/Dalton/config/service.json`，零代码，最高杠杆 |

## 2. 本周（配置 + 小代码）

### 2.1 调度层

- **治理短路**：`sales_notes_feed`（`FeedLaunchRejected: list_notes governance record is not approved` 252 次）与 `company_wiki_feed`、`prior_research` 每 tick 各占单写者线程 30s 后超时（writer.stderr 超时榜前三：330 / 193 / 136）。在 lane dispatch 入口对 `not_permitted` / `ConnectorQuotaExceeded` 做缓存短路，`dependency_ok` 前跳过整条 lane。预计 tick 从 ~150s 回到 ~30s。
- **只读操作不走 store 队列**：`list_agenda_feedback_targets` 等 `list_*` 纯查询单开只读连接；客户端 30s 与服务端 `STORE_REQUEST_TIMEOUT=30` 同值导致 BrokenPipe 并丢失归因，拆开两端超时。
- **索引**（须先清磁盘，停机或低峰执行）：`scheduler_leases(work_order_id, attempt_number)`、`scheduler_result_envelopes(work_order_id, attempt_number)`、`scheduler_formal_results(work_order_id)`、core `model_invocations(created_at, invocation_id)`。
- **僵尸子进程**：`lane_child_launcher.py:329-347` 只持有最近一个 Popen，旧子进程永不 `wait()`；`pid_alive()`（`:96-106`）对僵尸返回 True。writer 启动 20 分钟已累积 9 个僵尸，与卡在 running 的 ticket 1:1 对应；全局 1,049 个 `running` ticket（1,039 个在 `research-plans/`，最老 7 天）。改为 dict 持有 + 启动时对账。
- **关闭强制本地化或限池**：`research_localization`/`research_language_*` 占 48h work order 的 49.8%（4,232/8,491），`language_check` 失败率 36%。`research-language-policy.json` 改为按需，并在 `budget_pools.py` 单列 presentation 池 ≤ 10%。
- **publication worker**：gate 通过，exit 1 是"有待办"的设计语义；真实问题是 329 个 batch 全部 deferred、0 完成，主因 `language checker served an unexpected transport or model`（421 次），重试无上限（已到 17 次）。修 `research-language-check-model-config.json` 路由契约，加重试上限，plist 补 `StandardErrorPath`。

### 2.2 研究内容层

- **dossier 裸崩溃**：`company_dossier_cli.py:1238` `unit_provenance.{unit}.producer input binding drifted` 抛未捕获 ValueError，整 run 报废并丢弃已起草单元（今日 ACN 连续 4 次）。改为可恢复的业务拒绝。
- **起草模型违反 schema**：dossier `sentences 11 > cap 8`、`keys ['slots'] vs ['gaps','slots']`；debate `bear.statement > 600`、`lean is not a lean`、`did not return one JSON object`；model spec `assessment > 1200`。brain 档已整体降级为 deepseek-v4-flash（06:00 UTC 起服务率 99.3%，但质量失败取代了可用性失败，debate map 验证驳回率 34.9%）。建议对 debate_map / dossier / model_spec 用 `purpose_overrides` 显式钉 `["profile:claude-opus-5","profile:deepseek-v4-flash"]`，并在起草侧加 schema 约束/截断重试。
- **antigravity 提示词贴着 30k 上限**：debate-map `prompt_bytes` p50 29,434、35/112 ≥ 30,000，`gemini-3-8-flash-antigravity-high` 成功率 1.0%（2/207）。裁剪阈值下调到 26,000，或先从 debate_map/plan 链移除。
- **planner 重复规划**：401 轮 → 8 个 distinct digest，单一状态重复 317 次，`formal_authority_writes = 0`；`TypeError: 'set' object is not subscriptable` 33 次；`planner_call_budget.max_output_tokens=4000` 直接触发 `PROVIDER_BUDGET_EXCEEDED`。把 digest 去重提到入队前；修 TypeError；max_output_tokens 提到 16000+；401 份 ticket 从不结算。
- **文档抽取 fail-fast**：432 轮中 411 轮失败，`a terminal model execution failed; remaining windows were not launched`（176 次）；30,372 个 window 只落地 517 条记录、8 个 figure；24% window 是 replay。改 per-window 隔离 + `(review_id, offset)` 持久去重；`IDLE_HOLD` 在 `awaiting > 0` 时不应生效。
- **阅读优先级倒置**：415 份阅读证明中 60% 是 management-changes 新闻，年报仅 11 份、电话会 42 份；日配额 2000 只用 246。把 transcripts / 10-K 提到队首。
- **深度文献研究车道死锁**：`mission_document_research` 557/557 tick `recovery_required`，`{"held":10}`，`controlled_recovery_authorizations` 0 行，累计 23 次 start、0 次 outcome；`mission_annual_research_*` 全 0。需要 owner 授权记录或代码逃生门。
- **claim 退役停摆 9 天**：72 条退役全部停在 09-07（误归属 69），此后 claim 从 ~1,000 涨到 6,390、退役 0 条。

## 3. 需要 owner 决策 / 治理签字

| 事项 | 影响 |
|---|---|
| 裁决 5 份 Deep Insight Gate 草稿；关闭 47 条过期 gate_reopen 提案；驳回 IBM conviction call 提案 | 解锁阶段 2–6 |
| 批准 `sales_notes list_notes`、`company_wiki list_documents`、`sec form144_notices` 三条 governance record，或在批准前在 lane registry 直接 disable | 每 tick 省 ≥ 60s |
| 批准 Yahoo Finance / company-ir 数据源（mission v23 `source:company-ir = not_connected`） | 初筛第 4 问、S6 估值、conviction 第 3 道闸门在数据上永远无法满足 |
| 授权 `mission_document_research` 受控恢复 | 唯一能把年报/电话会全文变成高质量 claim 的通道 |
| 核查 OpenAI credential slot（连续 44h 429） | brain 链恢复 |
| brain 档质量取舍：是否接受 deepseek-v4-flash 作为首位 | 交付物质量 |

## 4. 研究内容的实质缺口（改代码解决不了）

- 全系统 6,390 条 claim 中定量只有 **22 条，且全部是 `quarterly_revenue_yoy_growth`**。没有分部收入、bookings、利用率、利润率、指引。
- 同时 SEC XBRL 车道健康：五家各 9 份 filing、16,903 行 statement_lines、2,432 条 metric_observations 持续写入，**但从未被提升为 claim**。这正是 dossier `numbers_without_refs` 反复失败、conviction `market_view_not_supported_by_cited_rows` 失败的共同根因。打通 statement_lines / metric_observations → 定量 claim 是投入产出比最高的一处修复。
- 事实底座 61% 来自 public_web 新闻，transcript 仅 32 条；近 48h 新增 2,465 条中 transcript 为 0。
- 522 次事件研判中 520 次 NO_CHANGE；09-15 产生 1,315 个事件只有 6 次研判。
- 四家（CTSH/EPAM/IBM/DXC）没有任何生效 thesis，conviction 第 1 道闸门永久关闭。

## 5. 被证伪的猜测

- 不是预算卡点：抽取链 48h 0.78 USD；AlphaEngine 用 35%、SEC 0%、Guidepoint 7%，零拒绝。账本上的 286 USD 是 429 失败按 ceiling 结算的幻影花费。
- 不是 writer 超时"当前风暴"：writer.stderr 1,410 次失败横跨 29 个 release、一个月未轮转；当前 release 约 40 次/小时，但 heartbeat 显示 9 条 lane `unavailable:RemoteError`，实际影响比日志显示更严重。
- `legacy agenda plane retired` 不是每 tick 空转，是每次启动打印一次（111 次 = 111 次重启）。
- `thesis-impact service config is invalid` 已在当前 release 修复（20 次全部来自旧包 8e910114）。
- `awaiting_human_extraction` 不是人工闸，是机器抽取队列的开放态（`document_extraction_launcher.py:348` 直接从该状态取件）。
- 线上服务跑的是最新源码（release 74fd6e14 与工作树零差异）；漂移的是账本：`manager.json` / `current-release.json` 记 23986f7a（落后 20 文件），publication worker 指向 d1f2f062（落后 8 文件），`point_services_at_release.sh` 的 label 模式漏掉 `com.dalton.*`。

## 6. 修复后预期（保守）

| 指标 | 现状 | 预期 |
|---|---|---|
| tick 耗时 | 92–188s | 20–30s |
| 抽取整轮成功率 | 4.9% | 50%+ |
| 阅读份数/天 | ~10 轮完成 | ~110 轮 |
| figure 产出/天 | 4 | 30–60 |
| planner 落地 directive/天 | 0 | 5–10 |
| 账本幻影花费/天 | ~286 USD | 0 |
| scheduler.sqlite 增长 | +80 MB/天 | +15 MB/天（归档 + payload 外置） |

## 关键文件

- `src/dalton_core/cockpit_model.py:2305-2312, 2360, 2427`（失败计费）
- `src/dalton_core/company_dossier_cli.py:1238`（裸 ValueError）
- `src/dalton_core/writer_server.py:376, 1927, 2439`；`src/dalton_core/writer_client.py:44-54`
- `src/dalton_core/dashboard_projector.py:335-352`
- `src/dalton_core/bounded_planner_driver.py:561-566`
- `src/dalton_core/lane_child_launcher.py:96-106, 288-307, 329-347, 370-377`
- `src/dalton_core/document_extraction_launcher.py:34-35, 288-297, 452-470`
- `src/dalton_core/mission_document_research_lane.py:470, 484, 493`
- `src/dalton_core/research_localization.py`、`budget_pools.py:80-233`
- `scripts/point_services_at_release.sh`
- `~/Library/Application Support/Dalton/config/service.json`
- `~/Library/Application Support/Dalton/state/dalton-core/research-language-policy.json`
