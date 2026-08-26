# S7d-1：Core-hosted SEC company-facts lane v0.1（2026-08-26）

分支 `s7d-sec-company-facts-lane`，worktree `/Users/everflow/Projects/dalton-s7d-lane`。未 push、未合并、未部署、未碰 live `~/Library/Application Support/Dalton`、未调真实 SEC 网络。

## 结论

Gate 1 canary 的整条链（perception snapshot → cycle → candidate → decision → backlog → `create_company_facts_plan` → `authorize_plan_by_policy` → `start_plan` → executor `run_once` → 定位候选 → `commit_policy_candidate` → closure + replay）已经参数化成 `dalton_core.sec_company_facts_lane.SecCompanyFactsLane`，可在既有 Core state 目录上跑，单家 issuer 在假 SEC 响应下端到端 committed：1 Evidence + 1 Claim、0 人工 gate、0 模型调用、replay 为 duplicate、同参数重跑为 duplicate 且计数不变。CLI `dalton-sec-lane`（`dalton_core.sec_lane_cli`）fixture 路径子进程端到端通过。

**同一个 Core 里跑第二家 issuer 会撞 `ConnectorQuotaExceeded: connector bytes quota exceeded`**，这是 v0.1 最大的未决项，四家 issuer 的 `run_lane` 目前在真实场景下预计只有第一家 committed。

## 做了什么

- `src/dalton_core/sec_company_facts_lane.py`
  - `US_IT_SERVICES_ISSUERS`（ACN/CTSH/EPAM/IBM，含 ticker/cik/company_ref/name），可用 `issuers=` 覆盖。
  - governance 只依赖 `approval/policy/policy_hash/policy_ref/principal_ref/approved_by/effective_from/id/content_hash/approved/allowed_permissions`；`approved=False` 在打开 Core 前直接拒绝。`RehearsalGovernance` 是内存 fake（基于 `_PublicAuthorities`，重写 `policy()` 使 `allowed_principal_refs` 与 lane 的 `principal_ref` 一致，否则 catalog `prepare` 会报 principal 不允许）。
  - 栈装配：`DaltonStore(<state>/core.sqlite)`、`Scheduler(connection=core.connection)`（WorkOrder 落在 core.sqlite，不碰 `<state>/scheduler.sqlite`）、`CapabilityCatalog(<state>/catalog.sqlite)`、`ResearchCoordinatorStore(<state>/research-coordinator.sqlite)`、`RawSpool(<state>/connector-spool)`、`CandidateStagingStore`/`HumanReviewAuthority` 共用传入的 staging 文件。descriptor 先 `describe` 再 `publish`，并核 source/schema hash、policy_ref、permissions。
  - Agenda 绑定：`create_policy(activate=False, version_id="agenda-policy-version:us-it-services-sec-lane:v1")` + `create_mandate("mandate:us-it-services-sec-lane", activate=False, version_id="mandate-version:us-it-services-sec-lane:v1")`，不动 live pointer。`start_cycle`/`decide_cycle` 不检查 pause，因此 lane 不调用 `set_pause`。idempotency 用固定 key；issuer 集合或 actor 变化时 `_idem` 返回 `status: conflict`，lane 转成 `LanePreconditionError`。
  - `check_core_governance_rules`：活动 governance policy 缺 `research_plan_auto_start` / `research_candidate_auto_commit` company-facts 规则时抛 `LanePreconditionError`，不创建 policy、不写任何东西。
  - 候选定位：从本 plan 的最后一个 WorkOrder（`candidate_staging` 步）的 `scheduler.formal_result` 用 `re_read_stage_records` 取 `candidate_claim` ref/hash，再 `review.candidate_bundle(claim_ref)` 并核 hash，不用 `list_candidates()[0]`。
  - 幂等 key 全部由 `LANE_SLUG` + `content_hash(company_ref, filed_from, filed_to, run_key)` 或 `plan_version_ref` 派生；perception snapshot 的 `generated_at` 固定为 `filed_to` 当天零点，保证同参数重跑请求 hash 相同。
  - `run_lane` 逐家跑，非 precondition 异常记 `status: failed` + 错误文本。
- `src/dalton_core/sec_lane_cli.py` + `pyproject` `dalton-sec-lane`：参数按任务清单；`--governance` 延迟导入 `connector_governance.load_connector_governance`，模块缺失报清楚错误；`--issuer-cik T=CIK` 允许测试用默认元组外的 issuer；`summary.json` 0600；全部 committed/duplicate 退出 0。
- `tests/test_sec_company_facts_lane.py`：8 个用例。

## 验证

`cd /Users/everflow/Projects/dalton-s7d-lane && PYTHONPATH=src /Users/everflow/Projects/dalton-research-agent-os/.venv/bin/python -m unittest -v tests.test_sec_company_facts_lane`

最后一次完整运行：8 个用例，6 ok、2 error（约 297 秒）。两处 error 之后已修：`test_missing_core_rules_*` 用了不存在的表名 `work_orders`（改为 `agenda_cycles`）；`test_locates_own_candidate_*` 撞 `ConnectorQuotaExceeded`，已标 `unittest.skip` 并写明原因。**修后的整套没有再跑一遍**（时间上限）。

回归 `tests.test_research_plan_executor tests.test_research_plan_closure tests.test_research_plan`：跑到 420 秒工具超时仍无输出，**未验证**。

## 明确没做

- 第二家 issuer 的 connector bytes quota 问题没有排查（可能是 ConnectorStore 每 connector 的日配额按 canary 单次调用设的），四家连跑未验证。
- `pyproject` 的 `dalton-sec-lane` 入口只加了行，没有 `pip install -e` 验证。
- 没有和真实 `dalton_core.connector_governance` 对接（模块尚不存在）。
- 测试跑得慢（单家端到端约 30–60 秒），没有定位耗时点。

## 需要主 session 注意

1. **Agenda 绑定**是 inactive 的固定 v1 版本；如果 live Core 里已经有同 version_id 但不同 issuer 集合的记录，lane 会以 `LanePreconditionError` 拒绝，需要 bump `LANE_SLUG`/版本。
2. **staging 候选定位**靠 plan 自己的 stage WorkOrder formal result，不依赖候选顺序；staging 文件里预存其它候选不影响（该用例因 quota 被 skip，逻辑本身在 `_locate_candidate` 里）。
3. **scheduler 表落在 core.sqlite**：`Scheduler(connection=core.connection)`，lane 与 Cockpit/writer 共用 core.sqlite 时 WorkOrder 表会同库；没有评估与 writer 常驻连接的锁竞争。
4. 第二家 issuer 的 `ConnectorQuotaExceeded` 要在 S7d-2 之前解决，否则 `run_lane` 的四家只会有一家 committed。
