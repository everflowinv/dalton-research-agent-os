# Planner SEC public 薄闭环（2026-08-15）

## 状态

开发候选，未部署、未接旧 cron，也没有开放凭据、能力租约或自动 Ledger commit。本切片把 exact selected
AgendaDecision 与 ResearchQuestionVersion 编译成不可变 ResearchPlanVersion，并把启动权限收紧到逐 plan 的
显式人工批准。

## 结果

1. **精确输入绑定**：`create_plan` 在同一 Core 事务内重读并核对 ResearchQuestionVersion、完整 backlog
   event history、AgendaDecision、AgendaCycle、AgendaPolicyVersion、MandateVersion、PerceptionSnapshot 和
   exact selected candidate。候选不必位于候选列表首位；问题、回答标准、`source_refs`、公司或 mandate
   任一换绑都会 fail closed。
2. **稳定且不可变的 plan**：plan 身份由 exact question version、AgendaDecision 和规范化 SEC request
   确定性派生。CIK 统一为 10 位；首版只允许 `10-K`、`10-Q`、`8-K`，查询窗口最多 366 天。
   `ResearchPlanVersion`、event、approval、start 与 idempotency 行均由 Core 单写者追加，禁止 update/delete。
3. **封闭执行范围**：首版固定为无凭据、公开只读的 SEC `list_filings`。plan 冻结 source/profile、operation、
   verifier、runtime、capability、输出 contract、资源上限和 `read:public-http` side effect；caller 不能添加
   host、credential、代码路径、写权限或额外步骤。
4. **四步任务树**：每份 plan 确定性生成 connector、authority resolver、source/numeric verifier、candidate
   staging 四个 WorkOrder，以及三条 WorkOrderLink。WorkflowRunVersion 与链接记录完整 DAG；启动时只把根
   connector WorkOrder 交给 Scheduler。三个子节点保持 `planned`，后续必须由 coordinator 在 exact 上游结果
   到齐后逐项 admission，不能越过依赖提前执行。
5. **人工启动门槛**：plan 初始为 `pending`，只有匹配 `human:<principal>` 的 actor 能写一次终态
   `accepted`/`rejected` decision。model、automation、timeout、Agenda approval 和 auto-accept 都不能授权启动；
   rejected plan 不能启动。
6. **崩溃恢复与幂等**：plan start 在外部 Scheduler、WorkflowRunVersion、WorkOrderLink 和 Core binding
   接缝使用确定性身份；在 enqueue、workflow、任一 link、start binding 或 backlog transition 后注入故障，
   重放都会收敛到同一个 start、同一棵树和一个根 WorkOrder，不重复产生执行 authority。
7. **读取时重新验权**：exact readers 每次从 SQL 列重建 canonical record 并重算 hash，同时复核 plan ↔
   question、approval、start、workflow、link 与 Scheduler root WorkOrder 的双向绑定。plan scope、pointer、链接、
   Scheduler 行或后续 authority 被篡改时均 fail closed。
8. **Backlog 联动**：`selected → planned` 与 plan 创建在同一事务中完成；`planned → in_progress` 与 plan start
   binding 同事务完成。Backlog 的完整历史读取现在也会重新核对 exact plan/start，不再接受无绑定的状态推进。

## 主要文件

- `src/dalton_core/research_plan.py`
- `src/dalton_core/research_plan_schema.sql`
- `contracts/research-plan-version.schema.json`
- `contracts/research-plan-event.schema.json`
- `contracts/research-plan-approval.schema.json`
- `contracts/research-plan-start.schema.json`
- `tests/test_research_plan.py`
- `src/dalton_core/research_question_backlog.py`
- `src/dalton_core/__init__.py`、`pyproject.toml`、`tests/test_packaging.py`

## 验证

- Planner 专项：13/13。覆盖非首位 selected candidate、错误 question/decision、非法 SEC scope、稳定 plan
  identity、人工批准、未批准/拒绝启动、四步树与 root-only admission、完成态重放、外部/事务故障恢复、
  plan scope/link/Scheduler tamper 和未授权 SQL 写入。
- Planner + ResearchQuestionBacklog：47/47；Python 全量：507/507。
- OpenClaw model broker：15/15。
- `compileall`、105 份 JSON contract 解析、18 份 Core SQL schema 和 `git diff --check`：通过。
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel 逐位一致，SHA-256
  `466935efa4684e7384b9b050002e642e648f848968442fb0a6a71850acb3ca38`，666,164 bytes；Python 3.13
  干净 venv 安装、公开导入和 packaged `research_plan_schema.sql` 读取通过。

## 未决边界

- 本切片只 admission 根 connector WorkOrder。resolver、verifier 和 candidate staging 的依赖满足检测与逐项
  Scheduler admission 要在 coordinator/executor 接线时实现；当前不能称为四步任务已经执行。
- rejected plan 是终态，但问题仍停在 `planned`。replan、park、resume、retire 的产品语义尚未设计，不能用
  隐式状态回退补洞。
- 本切片没有创建 CapabilityLease、CredentialGrant，没有访问真实 SEC，也没有写 EvidenceVersion、
  ClaimVersion 或 RelationVersion。正式 Ledger commit 仍由既有 HumanReviewAuthority 单独 gate。
- 下一笔按冻结顺序进入 Interrupt / park / resume；部署、旧 cron cutover 和生产权限继续独立验收。
