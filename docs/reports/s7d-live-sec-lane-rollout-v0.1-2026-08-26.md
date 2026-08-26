# S7d：US IT Services SEC lane 上 live（v0.1，2026-08-26）

## 结论

- S7d 已经在 live 跑通：ACN、EPAM、CTSH 三家的最新季度收入同比由 Core-hosted SEC `get_company_facts` lane 取数、验证、staging，
  并由 governance policy `policy-2`（commit-gate v2）自动提交，0 人工 gate。live Core 现有 4 条正式 Claim / 4 条 Evidence：
  1 条 ACN transcript qualitative（S7c-4，owner accept）+ 3 条 SEC quantitative。
  - ACN `claim-version:1c4f31d8…`：Revenues Q3 FY2026（3/1–5/31）18,718.1M USD，同比 +5.59%，10-Q `0001467373-26-000032`（filed 6/18）
  - EPAM `claim-version:41a60259…`：RevenueFromContractWithCustomerExcludingAssessedTax Q2 2026（4/1–6/30）1,414.8M USD，同比 +4.53%，10-Q `0001352010-26-000046`
  - CTSH `claim-version:74ef310b…`：Revenues Q1 2026（1/1–3/31）5,413.0M USD，同比 +5.83%，10-Q `0001058290-26-000016`（filed 4/29；
    companyfacts API 还没收录 7/29 的 Q2 10-Q，所以窗口用 `filed_from 2026-04-01`）
- Phase 7 退出门槛「≥5 条 policy 自动提交的 SEC 正式 Claim」目前 3 条，未达。IBM 被 `SEC_MAX_RESPONSE_BYTES` 5 MiB 挡住
  （companyfacts 5,650,571 B），要 owner 决定是否提到 8 MiB；第五家 DXC 不在 lane 的 issuer universe 里，加进去要 bump 绑定版本。
- 上 live 后暴露并修掉两处性能根因（详见下），修复版 `ea160d6` 已部署（20:04 UTC）。

## 做了什么（时间线，UTC）

1. 18:29–18:41 合并 S7d-2（治理记录泛化）、S7d-3（writer 接线），SEC 治理记录重新 propose 为 `effective_from 2026-08-26T00:00Z`
   并由 `human:lumos` approve（content_hash `e57b25b6…8517f0`）；live 发布 governance policy v2（`policy-2`，prior `policy-1`，pointer 已指向）。
2. 18:46–18:57 合并 S7d-1（lane 主体），修 SEC quota 窗口（`advance_past_quota_window`，`c7cd10e`），隔离演练四家：ACN / EPAM committed，
   CTSH blocked（API 滞后），IBM blocked（5 MiB）。
3. 19:00 部署 `326a62f`；19:02 live run 1（ACN+EPAM）：ACN committed，EPAM 在 JSON 编解码里跑了 6 分钟以上没完，19:11 手动中断（exit -2）。
4. 19:16 live run 2（ACN+EPAM）：ACN duplicate、EPAM committed，473 秒。ticket 收口发现 `completed_at` 记录的是 `sec_lane_status` 轮询时刻而非子进程退出时刻。
5. 19:45 部署 `abff89f`：lane agenda 绑定改 v2（绑整个 coverage universe，分批跑不同 ticker 共用一个不可变绑定；live 原 v1 绑定是 ACN+EPAM，
   单跑 CTSH 会冲突）；子进程每 60 秒把 Python 栈写进 run.log；`--foreground`（后证无效，已撤）。
6. 19:48 live run 3（CTSH，`filed_from 2026-04-01`）：committed，391 秒；运行期间 writer 的只读 `sec_lane_status` 也 30 秒超时。
7. 19:5x 发现 thesis-impact worker 自 19:07 起每 5 分钟都 `RemoteError`：`thesis_impact_targets` 在 writer 里超过 30 秒。
   cProfile：`ResearchPlanAuthority.plans()` 对 3 个 SEC plan 做 12 次 `read_exact_research_plan_version`，每次经 `_revalidate_execution_scope → _sec_template()`
   重新从磁盘加载并校验整个 packaged connector inventory（共 72 次，每次 0.42 秒），前台 11.5 秒，Background writer 里超过 30 秒。
8. 根因二：writer LaunchAgent `ProcessType: Background`。实测（6M 次循环 CPU 探针）Background 1.98 秒 vs Standard 0.32 秒；
   子进程里 `setpriority(PRIO_DARWIN_PROCESS, 0, 0)`、外部 `taskpolicy -B -p <pid>`、`taskpolicy -c utility` 都清不掉 launchd 的 clamp。
9. 20:04 部署 `ea160d6`：packaged inventory 每进程缓存一次、按 deepcopy 发放（`plans()` 11.5 秒 → 0.30 秒）；writer plist 改 `Standard`
   （controller / control / thesis-impact 仍是 Background）；撤掉无效的 `--foreground`。部署后 `thesis_impact_targets` 0.25 秒，
   worker 20:04 的定时运行回到 `idle`；live run 4（CTSH 重跑）duplicate，1 秒内返回。

## 验证

- 新增测试：`test_partial_issuer_runs_within_the_universe_share_one_binding`（lane v2 绑定）、
  `test_writer_launchagent_is_standard_process_type_others_background`（plist）。定向回归：connector_inventory + service +
  research_plan_executor + connector_governance 48/48；lane + launcher + writer_service + transcript ops + thesis_impact* 101 项中 100 通过，
  唯一失败 `test_partial_frame_does_not_block_valid_client_and_connection_limit` 在本机对 `84ffc70`（S7d 之前）同样失败，属本机环境问题，
  CI `dc747de` 那轮通过。
- live：`sqlite` 只读核对 claim_versions 4 / evidence_versions 4；governance pointer `policy-2`；agenda 绑定 v1、v2 并存（v1 保留，不可变）。
- 未测：writer 改 Standard 后新 issuer 的 lane 耗时（run 4 走 duplicate 短路，不构成样本），要等 IBM 或下一家。

## 明确没做 / 待 owner

- IBM：`SEC_MAX_RESPONSE_BYTES` 5 MiB → 8 MiB 是否放开，owner 决定；放开后 lane 一跑就能到 4 条。
- brief v3 live：lane 只出 XBRL 口径季度收入同比，v2 manifest 的 21 条 exhibit KPI 不在其中；driver pack 要么加 `revenue-growth-usd-gaap`
  只用 lane 的 Claim，要么 exhibit KPI 另走人工 gate 的定量候选路径。等 owner 选。
- 未定位：19:45:45 / 19:46:24 两次 `run_sec_company_facts_lane` 在 writer 重启后 53–92 秒内客户端 10 秒超时且没生成 ticket，
  19:48:22 同一调用 0 秒返回；run 3 期间只读 `sec_lane_status` 30 秒超时。writer 没有请求日志，建议 S7e 前补。
- ticket `completed_at` 应改为子进程退出时刻（launcher 收割时用 `os.wait4` 的时间或 summary mtime）。
- S7d-2 子任务 worktree 里 `17bdaab`（报告）、`9f4d7c4`（别名 + `propose` 的 human principal 校验）未合并，与主 session 补写的报告冲突，按需 cherry-pick。
- CI：`32989659672`（`51d5820`）的 `test_input_token_budget_fails_closed_without_truncating` 因 writer socket 超时 error，后续 `dc747de` 通过；
  本轮 15 个 commit 尚未 push 前的 CI 未跑。
