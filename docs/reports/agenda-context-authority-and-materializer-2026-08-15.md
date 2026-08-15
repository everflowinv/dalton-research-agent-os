# Agenda context authority 与统一 materializer（2026-08-15）

## 状态

开发候选，未部署、未接 ResearchQuestionBacklog/Planner、未改 cron，也没有放宽 plan 启动或 Ledger commit 权限。

## 结果

AgendaCoordinator 的事实输入已统一到 ContextPack/ContextMaterializer 路径。PerceptionSnapshot 先登记进 Core
append-only authority，AgendaCycle 再冻结 exact PerceptionSnapshot、MandateVersion 和 AgendaPolicyVersion
ref/hash。即使有人绕过 immutable trigger，把 Mandate/Policy row 和自身 hash 一起改成另一份内部一致的记录，
cycle-frozen hash 仍会拒绝重放。
模型最终只收到固定 instruction/output contract 与 materializer 生成的 quoted JSONL；旧的手工
`MANDATE=`/`PERCEPTION=` 数据拼接已删除。

新增 `AgendaContextBinding`，直接绑定 exact Cycle/Policy/Mandate/Perception ref/hash。它是独立 closed contract，
不会把 Agenda 伪装成 CompiledConnectorPlan。ContextMaterializer 使用受控 binding union：旧 connector-bound
ContextPack 0.1 的 replay 语义不变，Agenda 则只允许 mandate/perception 两个 required input。调用方不能提交正文、
resolver callback、数据库路径、DocumentIndex FTS body 或自选 materialization timestamp。

PerceptionSnapshot 的 canonical record、content hash 和查询列都由 exact reader 复核，表只允许 DaltonStore 写入，
并由 no-update/no-delete trigger 保持不可变。Mandate、Policy 和 Cycle 也从各自 canonical row/hash 重算；active
policy/mandate 不再直接信任 JSON 列。writer 只给 core principal 开放 snapshot 注册、exact read 和 cycle-scoped
materialization，其他 principal 与未知字段都拒绝。

Agenda 专用 renderer 以 AgendaContextBinding 为模型可见 envelope。ContextPack 0.1 的必填 ClaimIndex 字段使用
只允许 Agenda binding 消费的显式 no-index sentinel，不伪造 Core ClaimIndex，也不扫描整个 Ledger。无关 Claim
写入不会改变 pack、manifest、prompt 或 WorkOrder。这个修正来自主审；初稿会为 Agenda 构建随 Ledger 变化的空
ClaimIndex，只保证正文不变，没有保证完整 prompt 和 WorkOrder 不变。

`max_input_tokens` 现在覆盖固定 wrapper、materializer envelope 和正文，统一使用冻结的
`tokenizer:dalton-search-token:0.1`。Mandate 或 Perception 任一被预算丢弃、ref/hash 漂移、binding 被替换，整个
cycle 都 fail closed，不截断、不重选。candidate 的 company 与 allowed source refs 只从 exact PerceptionSnapshot
authority 派生，不再重新读取可变 snapshot 文件。

## 主要文件

- `src/dalton_core/agenda.py`
- `src/dalton_core/agenda_schema.sql`
- `src/dalton_core/agenda_context.py`
- `src/dalton_core/agenda_coordinator.py`
- `src/dalton_core/context_materializer.py`
- `src/dalton_core/research_context.py`
- `src/dalton_core/writer_client.py`
- `src/dalton_core/writer_server.py`
- `SPEC.md`
- `contracts/agenda-context-binding.schema.json`
- `contracts/context-materialization.schema.json`
- `tests/agenda_fixtures.py`
- `tests/test_agenda_context.py`
- `tests/test_agenda_coordinator.py`
- `tests/test_writer_service.py`

## 验证

- Agenda context/coordinator/writer 专项：51/51。
- Python 全量：460/460。
- OpenClaw model broker：15/15。
- `compileall`、96 份 JSON schema、16 份 SQL schema、`git diff --check`：通过。
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel：SHA-256 均为
  `b66589f8e28f6b10fd7f0c44bffe37ba6de97ce5b1c95add57dbe9da59dd0ba9`，大小均为 622,505 bytes。
- Python 3.13 clean venv：安装、公开导入、Agenda schema、Perception authority table、
  AgendaContextBinding contract 和扩展后的 ContextMaterialization contract 均通过。首次安装探针错误地在
  `site-packages/share` 查 data files；wheel 按标准装到 venv `share/dalton-core/contracts`，修正探针路径后通过。

## 未决边界

- 仍没有 ResearchQuestionBacklog，问题不能跨 cycle 保持稳定 id、状态和答案引用；这是下一笔。
- AgendaDecision 仍不产生 ResearchPlanVersion 或 WorkOrder DAG；auto-accept/timeout 继续只作反馈事实，不授权执行。
- 旧 live cycle 没有 Core PerceptionSnapshot authority 时会 fail closed。部署前必须单独裁决 backfill 或从新 cycle
  开始，不能把旧 snapshot 文件静默提升为 replay authority。
- `dalton-search-token` 是冻结的确定性预算 tokenizer，不是模型原生 tokenizer。
- quoted JSONL 把外部文字明确隔离成数据，但不宣称解决一般 prompt injection。
