# P9a ResearchPlaybook 与 CoverageMission authority v0.1

日期：2026-09-02  
状态：development candidate 完成；隔离 canary（in-memory + live Core 只读副本）通过；**未部署、未写 live、未激活任何自动化写入**  
上游裁决：[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)

## 做了什么

### 1. ResearchPlaybook authority（`src/dalton_core/research_playbook.py` + `research_playbook_schema.sql`）

把 chem agent 的团队分析师手册转写为 `ResearchPlaybookVersion 0.1`，human-only、append-only、带 pointer 与 idempotency：

- **六个冻结阶段**（`STAGE_ORDER`）：initial_screen → deep_insight_gate → industry_model → company_model → investment_memo → active_coverage。每阶段 closed shape：label / objective / required_readings / required_outputs（非空）/ exit_gate{questions, pass_rule} / human_checkpoint。顺序、数量错一个就拒绝。
- **人类检查点不可去掉**：deep_insight_gate 与 investment_memo 的 `human_checkpoint` 必须为 true。
- **五词决定词汇冻结**：`decision_vocabulary` 必须逐字等于 NO_CHANGE / THESIS_STRENGTHENED / THESIS_WEAKENED / THESIS_BROKEN / NEW_THESIS。
- **证据纪律**：`number_provenance_rule` 冻结为 `every_timely_number_traces_to_a_tool_result_or_primary_filing`；`banned_sources`、`source_hierarchy`、`minimum_independent_sources_for_key_numbers`。
- 其余字段：`key_questions`（memo 12 问）、`deliverable_templates{initial_screen, investment_memo}`、`analyst_levels`（Basic 1–4 + Advanced 1–4）、`tracker_classes`（含 agent_binding 如实写 connected / probe_only / not_connected）、`risk_reward_standards`、`model_discipline`。
- 读路径 `read_exact_playbook_version` / `read_active_playbook_version` 重验 canonical JSON、content_hash 与 SQL 列一致性；SQL 侧 authorized-guard 函数 + 不可变触发器。

### 2. CoverageMission authority（`src/dalton_core/coverage_mission.py` + `coverage_mission_schema.sql`）

- **`CoverageMissionVersion 0.1`**：title / objective / industry_ref / universe（company_ref、ticker、tier A|B|C、priority P0|P1|P2）/ research_questions / deliverables（冻结词表 7 项）/ source_plan（status 冻结 connected|probe_only|not_connected）/ bindings{playbook_version, constitution_version, mandate_version}（exact ref+hash，且必须是各自 active 版本、同一 industry、mandate 在有效期内）/ autonomy / budget。
- **autonomy 合同**：`automation_principal` 必须是 `automation:` 命名空间；`may_write` 只能从 `evidence / claim / forecast_line / model_run / research_question / observation / stage_record` 中选，thesis、constitution、playbook、mission、mandate、policy 由合同排除；`human_checkpoints` 必须包含 `thesis_admission / thesis_revision / scope_expansion / budget_expansion` 与 playbook 标为 human_checkpoint 的阶段，只能追加。
- **阶段账本**（`record_stage`）：绑定 active mission 版本 + hash；公司必须在 universe；进入第 k 阶段要求第 k−1 阶段 gate_passed；gate 决定前必须 entered；gate_passed 不可重复、必须带 evidence_refs；automation actor 必须等于 mission 声明的 principal 且持有 `stage_record` 写权，且不能通过人类检查点阶段的 gate。record id 由请求内容派生，重复请求返回 duplicate。
- **进度投影**（`mission_progress`）：每家公司的 current_stage / current_status / completed_stages / next_stage / record_count。

### 3. writer ops（`writer_server.py`）

human-governed + core：`publish_research_playbook`、`get_research_playbook`、`get_active_research_playbook`、`research_playbook_report`、`create_coverage_mission`、`get_coverage_mission`、`get_active_coverage_mission`、`record_mission_stage`、`coverage_mission_progress`、`coverage_mission_stage_records`。新增 `MISSION_AUTOMATION_OPERATIONS` 豁免：writer 原有「HUMAN_GOVERNANCE_OPERATIONS 必须 human: actor」硬检查对这 5 个 op 放行 `automation:` principal，再由 authority 拒绝非 mission principal 与人类检查点。错误码映射补齐两套新错误类。

### 4. 合同、manifest 与 canary

- `contracts/research-playbook-version.schema.json`、`coverage-mission-version.schema.json`、`coverage-mission-stage-record.schema.json`（Draft 2020-12，closed；测试断言 required 集合与运行时记录键完全一致）。
- `deploy/phase9/p9a-research-playbook-v1.json`：手册全文转写（provenance 注明来源文件）。
- `deploy/phase9/p9a-us-it-services-mission-v1.json`：ACN(A/P0)、CTSH(A/P1)、EPAM(B/P1)、IBM(B/P2)、DXC(C/P2)；source_plan 如实：SEC connected、AlphaEngine probe_only、company-IR / Guidepoint / web-search not_connected；automation `automation:coverage-mission`；预算 40 次/日、5 USD/日、AE 30 次/24h。
- `scripts/run_p9a_playbook_mission_canary.py`：in-memory 模式从 P8a manifest 起 mandate / driver pack / doctrine / constitution；`--source-core` 模式把现有 Core 用 sqlite backup 复制到临时目录（源以 `mode=ro` 打开），绑定副本里真正 active 的 constitution 与其 mandate。两种模式都做 playbook / mission fresh→duplicate、ACN 阶段演练、integrity check。

## 验收

- 新测试 **17/17**：`test_research_playbook`（9：manifest 发布与重放、JSON 合同同形、阶段顺序冻结、人类检查点不可删、词表不可弱化、只接受 human actor、版本链与 idempotency 冲突、触发器挡直写、篡改行读取时被识别）、`test_coverage_mission`（7：manifest 建 mission 与重放、两份合同同形、绑定必须 exact 且 active、autonomy 逃不出 human-only 对象、阶段账本顺序/证据/人类门、只绑 active 版本、触发器）、`test_p9a_writer_ops`（1：governance / worker / automation 三种 principal 的权限边界与阶段演练）。
- 全仓 **986 个测试，985 通过，1 个 error**：`tests.test_writer_service.WriterServiceTests.test_partial_frame_does_not_block_valid_client_and_connection_limit` 报 `TimeoutError`。该用例在本机把 `writer_server.py` / `pyproject.toml` 改动 stash 回 HEAD 后同样失败（3 次复现），与 P9a 无关，属于本机 macOS 上 UNIX socket 超限连接未被立即关闭的环境差异；最近 5 次 GitHub CI（ubuntu）均为绿，本次以 CI 结果为准。
- `scripts/run_hermetic_research_replay_canary.py` passed；`git diff --check`、`compileall` 通过。
- canary：in-memory `ok=true`；live 只读副本 `ok=true`——绑定 `constitution-version:us-it-services:2`（hash `6cb95dd2…`）与 `mandate-version:us-it-services-constitution-p8a:1`，ACN 演练 automation 过 initial_screen → 进入 deep_insight_gate → automation gate_passed 被拒（`deep_insight_gate is a human checkpoint; gate_passed requires a human: actor`）→ human 通过 → next_stage `industry_model`；integrity ok；0 付费调用、0 网络、0 live 写入。摘要在 `temp/p9a-canary-{inmemory,livecopy}.json`（temp 不入 git）。

## 边界与未做

- 没有部署（wheel 未重装、controller / writer 未重启），live Core 没有 playbook 表和 mission 表；发布 playbook v1 与 mission v1 到 live 是 owner gate。
- 没有让任何 automation 真的写 Claim：P9b 才把 `automation:coverage-mission` 接到 SEC lane 触发。
- playbook `minimum_independent_sources_for_key_numbers=2` 是手册对「关键数据」的要求；live constitution v2 的 `minimum_independent_sources=1` 是 SEC lane 单一 filing 的现行强制口径。两者不冲突（前者管 thesis-bearing 结论的交叉验证，后者管 lane 自动提交），P9b 在 lane 触发合同里写明适用范围。
- `docs/external/thoughts-on-ai-for-hedge-fund-2026-08-25.md` 与 `src/dalton_core/llm_research_planner_worker.py` 的既有未提交改动不在本次 commit 内。

## 下一片

P9b：`record_observation_followup` 发现新 accession → 在 mission `may_write` 与预算内以 `automation:coverage-mission` 触发 SEC lane；company facts scope 追加 10-K 与 Q4 = FY − 9M 冻结公式；每条自动 Claim 同步写 mission 阶段账本。目标 2026-10-01 ACN Q4 业绩前就位。
