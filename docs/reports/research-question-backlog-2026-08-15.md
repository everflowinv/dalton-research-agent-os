# ResearchQuestionBacklog 切片（2026-08-15）

## 状态

开发候选，未部署、未接 Planner/WorkOrder/cron、未改变 auto-accept/timeout 权限，也没有放宽 plan 启动或
Ledger commit 权限。Agenda Shadow 的既有 `research_question_versions` 写路径保持不变；本切片是独立的
append-only 长期问题账，不与 Phase 1 的 per-cycle 候选表混用。

## 结果

ResearchQuestionBacklog 让研究问题成为跨 Agenda cycle 存续的长期 authority，而不是每天被重新发明：

1. **稳定身份与不可变版本**：`question_ref` 由 canonical `{mandate_ref, company_ref, question}` 绑定
   确定性派生（`research-question:<hash32>`），caller 不能提供 question ref、version id、identity hash
   或 content hash；写入时全部从 exact MandateVersion 重算。同一逻辑 mandate 换版本后重新记录同一问题，
   仍解析到同一身份（已实测 mandate-version:1 → mandate-version:2 身份不变、不产生重复行）。
   内容版本行 `backlog_question_versions` append-only、不可变、带版本链（本切片只写 v1）。
2. **冻结状态机**：`open → selected → planned → in_progress → answered | blocked | retired`，无恢复迁移。
   每个合法迁移在同一 Core 事务内校验并追加不可变 event 行；非法或乱序迁移 fail closed，零残留。
   `blocked`/`retired` 是终态，终态问题必须作为新问题（如修订文本）重新提议。
3. **跨 cycle 去重**：`record_question` 对相同绑定+相同内容返回既有 head（duplicate，幂等重放）；
   相同绑定+不同内容 fail closed（conflict）。`backlog_idempotency` 沿用 agenda 幂等约定：同 key 同
   request 返回原结果，同 key 不同 request 返回 conflict。
4. **精确 AgendaDecision 链接**：`select_question` 只接受 open 问题，并在事务内重读 exact
   AgendaDecision（从列重建 canonical wire 并核对 content hash）、exact AgendaCycle（冻结 start hash）、
   exact MandateVersion；要求 cycle 的 mandate == 问题 mandate、cycle company == 问题 scope，且 decision
   的 selected candidate 与问题、回答标准及 `source_refs` 逐字一致。读取 selection link 时再次复核 event、
   decision、cycle、candidate、policy 和 backlog head，跨 mandate、跨公司、伪造 decision、decision 未选
   该问题或来源换绑一律 fail closed。
5. **answered 只绑正式 ClaimVersion**：`answer_question` 要求一个或多个正式 claim ref，逐条从 Core
   `claim_versions` 重读、重算 hash、核对 SQL 列与 canonical record，并按 schema_version 重新校验
   闭合形状（0.1 走 ClaimVersion contract，0.2 走 additive Ledger validator）。candidate/staging/
   缺失/篡改 claim 全部拒绝；`candidate-claim:` 前缀显式拒绝。读取 answer binding 时重新核对 answered
   event 与当前 exact ClaimVersion，绑定完成后的 claim authority 漂移也会 fail closed。AgendaDecision 永远
   不会成为 answer。
6. **无 plan / 无执行权限**：本切片不创建 ResearchPlanVersion、WorkOrder 或 DAG；`plan_question` 只推进
   状态机（Planner 切片再把 ResearchPlanVersion 绑到 planned 状态，plan 启动保持独立人工 gate）。
7. **Mandate 进度投影**：`mandate_progress` 是纯确定性重算（不写任何表、不改 MandateVersion authority、
   不成为替代 authority），绑定当前 active MandateVersion ref/hash，聚合该 mandate 下全部 backlog 问题
   的 state 计数、逐问题条目与 answered claim refs；`created_at` 取投影输入中的最新 authority 时间戳，
   因此同一 authority 状态下重建必然得到相同记录和相同 hash。
8. **schema/contracts/公开导出**：新增 `research_question_backlog_schema.sql`（7 张表 + authorized
   insert / no-update / no-delete trigger，与 agenda schema 约定一致），5 份 closed JSON contract，
   `__init__.py` lazy export 与 `__all__`，pyproject package-data 已登记。

## 主要文件

- `src/dalton_core/research_question_backlog.py`
- `src/dalton_core/research_question_backlog_schema.sql`
- `contracts/research-question-version.schema.json`
- `contracts/research-question-event.schema.json`
- `contracts/research-question-selection-link.schema.json`
- `contracts/research-question-answer-binding.schema.json`
- `contracts/mandate-question-progress.schema.json`
- `tests/test_research_question_backlog.py`
- `src/dalton_core/__init__.py`、`pyproject.toml`、`tests/test_packaging.py`

## 验证

- 专项 `tests/test_research_question_backlog.py`：34/34。覆盖：完整生命周期（含 0.2 claim 绑定）、
  跨 cycle 去重与终态重放、幂等 replay vs conflict、非法/乱序/重复迁移 fail closed、
  blocked/retired 终态、跨 mandate/跨问题引用 mixup、伪造 decision、无 claim/candidate claim/
  缺失 claim/篡改 claim、claim 篡改回滚、问题/事件/绑定/链接行篡改检测、来源换绑、pointer 换绑、
  历史状态篡改、绑定后 ClaimVersion 篡改、idempotency key canonicalization、未授权直写被 trigger 拒绝、
  投影确定性重建与零写入、投影 tamper fail closed、生命周期不改动任何非 backlog authority 表、
  无 plan/auto-accept API、exact reader 拒绝缺失与 caller 提供 id。
- 相关回归：agenda/agenda_context/agenda_coordinator/contracts/packaging/claim_index/materializer
  共 82/82（不含本专项）通过。
- Python 全量：494/494（原 460 + 本切片 34）。
- broker：15/15。
- `compileall`、101 份 JSON contract 全部可解析、16 份 Core SQL schema、`git diff --check`：通过。
- fresh DB：`PRAGMA integrity_check = ok`，`foreign_key_check` 无违规；既有库路径通过
  `CREATE TABLE IF NOT EXISTS` 幂等建表（所有测试均在先建 AgendaStore 再建 backlog 的顺序下运行）。
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel 构建完全一致，SHA-256
  `3df806fc10f00efd70e7f14cfb04c84ff7652ab40f25f196e15b0d439583ec46`，639,898 bytes；
  fresh venv clean install、公开导入和 packaged `research_question_backlog_schema.sql` 读取通过。

## 未决边界

- AgendaCoordinator 尚未调用 `record_question`/`select_question`；接线是独立步骤，本切片只交付 authority
  与确定性去重机制。Agenda Shadow 的 `add_candidates` 继续写旧的 `research_question_versions` 表，
  两者并存，未来接线时需裁决迁移/双写策略。
- `planned` 迁移暂不绑定 plan ref（Planner 切片引入 ResearchPlanVersion 时再绑）；`plan_question` 的
  reason 参数可被任意 caller 使用，不构成执行授权。
- answered 接受任何正式 ClaimVersion（含 derived status 为 proposed/contested 的历史版本）；
  若产品要求只允许 corroborated/最新版本，需要单独的 status-aware 过滤决策。
- 终态问题不能重开；重新提议必须用新问题文本（新身份）。这是产品决策，后续如需 re-open 治理要
  显式设计。
- `question()`/`mandate_progress` 的 answered claim 列表按 binding 写入顺序返回；跨问题去重（同一 claim
  回答多个问题）目前允许。
