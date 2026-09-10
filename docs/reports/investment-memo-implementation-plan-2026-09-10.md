# Investment Memo 闭环实施方案 — 2026-09-10

基线：只读审查集成树 `3a6144760b052ef892497076031c256cbb78e892`。本报告不修改源码、配置或 live。

## 1. 最重要的发现

Investment Memo 不需要新 authority。`MissionDeliverableAuthority` 已接受 `investment_memo`，提供 append-only
版本、mission/playbook/hash 绑定、Claim/figure/forecast-cell 数字校验、空壳拒绝和幂等发布；
`CoverageMissionAuthority.record_stage()` 已提供顺序门、active mission 绑定和 human checkpoint 强制。

真正缺的是三个消费者：memo 的选取/起草/核验 lane、memo 的 Cockpit 人工裁决入口、以及裁决后进入
`active_coverage` 的有序写入。另有一个必须先解决的阶段断点：当前没有生产代码把 `industry_model` 和
`company_model` 写成 `gate_passed`。全库 `record_stage()` 调用只有 Initial Screen、Deep Insight 人审、SEC
证据入阶段和 writer 通用入口；行业框架与公司预测 lane 发布产物但不推进阶段。因此严格按现有阶梯，
memo lane 即使写好也没有合法候选。不能绕过这个断点把“有一个 forecast row”当成完整公司模型。

## 2. 应复用的现有合同

### 阶段与人审

- `research_playbook.py:41-51` 冻结顺序为 Initial Screen → Deep Insight Gate → Industry Model → Company
  Model → Investment Memo → Active Coverage；Deep Insight 和 Investment Memo 永远是 human checkpoint。
- `coverage_mission.py:4002-4094` 已强制 active mission hash、universe、前一阶段 `gate_passed`、先 entered
  后决定，以及 automation 不得通过 human checkpoint。
- `coverage_mission.py:3925-3964` 已跨 mission version 折叠阶段历史；新实现必须调用它，不能只查当前
  mission version 的行。
- Deep Insight 的 writer 决策路径（`writer_server.py:2837-2915`）给出了正确的 crash-healing 顺序：先写
  stage，再写决定。Memo 不需要第二份 decision authority；stage record 本身已经保存 actor、rationale、
  exact evidence refs 和不可变 hash。

### 文档 authority 与数字纪律

- `mission_deliverable.py:730-835` 可直接发布 `kind=investment_memo`。它验证 automation grant、subject 属于
  mission、1..N sections、非空、live Claim refs、cell refs、unsourced numbers、mission/playbook/template
  绑定，并将 gate 作为对文档的评价而非正文 hash 的一部分。
- Memo 必须复用这一个 chain：`mission-deliverable:investment_memo:<company>`。不得新增
  `InvestmentMemoVersion` 或另一份表。
- `deliverable_templates.investment_memo` 是 12 节：Key Information、Executive Summary、S1–S9、附录专家
  联系清单。它不是 Deep Insight 的十二问模板。
- Playbook 的 `key_questions` 才是 memo exit gate 所说的“12 问”：业务、driver/隐藏假设、管理层、三年
  股价、高频数据、consensus bull/bear、variant view、event pathway、期限、对冲和风险。Deep Insight 的
  12 问答案应作为输入证据，但不可替代这组 memo Key Questions，也不可把旧答案无条件复制成当前结论。
- `deep_insight_gate.py:107-153` 已有 dossier/debate/forecast/valuation 的 typed-source 映射；
  `deep_insight_gate_draft.py:612-723` 已有“verifier 看完整草稿和实际引用行”的闭合范式。

### 已有输入

Memo 的一次 frozen input 应至少包含：active mission + bound playbook/constitution；当前获批 Deep Insight
Gate；CompanyDossier；DebateMap；Industry Framework；CompanyModelSpec、model input table 和当前 forecast
model；sensitivity 与 consensus bridge；valuation snapshot、market-price as-of；最新 theses、falsifiers、
ResearchEvents/event judgements、CatalystCalendar、guidance profile、insider/buyback；以及 prior research 中
明确标为 prior 的 memo/模型。每一项记录 exact version ref/hash；缺表或缺产物形成 `gaps`，不能用空字符串
或模型常识补齐。

## 3. 前置门设计

Memo 候选必须同时满足：

1. 公司仍在 active mission universe，mission deliverables 包含 `investment_memo`，automation 有
   `deliverable` grant，human checkpoints 包含 `investment_memo`。
2. folded stage state 中 `deep_insight_gate`、`industry_model`、`company_model` 都为 `gate_passed`，当前
   `next_stage == investment_memo`。
3. 有 owner-approved Deep Insight Gate 版本；有当前 Industry Framework；公司模型 readiness 的历史核对、
   assumptions、consensus divergence 三项确定性 gate 全通过。
4. 有非 provisional market price、valuation snapshot、forecast head 和 sensitivity。缺 consensus/mosaic 可以
   形成明确 gap，但 exit gate 必须失败，不能生成正式通过候选。
5. 当前 memo head 不存在，或 frozen input hash 相对 head 有真实变化。模型 route、budget、policy、template 或
   input 变化都进入 launch/work identity；同输入拒绝或失败走现有 LaneFailureBudget，不重复付费。

### 必须补的 industry/company 阶段桥

最小实现应先增加一个纯确定性 `model_stage_readiness` evaluator，而不是让 memo lane自行宣布前置阶段完成：

- `industry_model`：Industry Framework 当前版本存在；playbook 三问逐项有 machine-checkable result；所有输入
  带 source/as-of；同行差距和高频日历缺失则 `gate_failed`。
- `company_model`：statement/model invariant checks 全绿；forecast readiness 非 unavailable；每项 assumption
  有 because/refs；consensus bridge 量化。三问全是才由 mission automation 写 `gate_passed`。
- 每阶段先写 `entered`，再写一次 `gate_passed` 或 `gate_failed`；evidence refs 必须是 framework/model/
  readiness 的 exact refs。阶段判断的版本/hash 变化才重算，不能每 tick 追加失败。

这是现有 playbook 的非人工门，使用 `stage_record` grant，不扩大治理词表。

## 4. 起草和核验

建议新增 purposes：`investment_memo` 与 `investment_memo_verifier`。模型配置复用已安装的
`dossier-model-config.json` 和 `company-dossier-verifier-model-config.json`，通过 purpose override 让 owner
可单独选择；不要再增加一对物理配置文件。需同时登记到 purpose→config 映射和 Cockpit model/budget 页面。

调用按 memo section 分组，避免单次超长，也不能 12 节逐节重复发送全量材料。建议四组：

1. identity/background：Key Information、Executive Summary、S1、S2；
2. view：S3、S4；
3. economics：S5、S6、S7、S8；
4. monitoring：S9、附录。

每组输入是预先冻结的 typed rows；输出 closed JSON：section title/body/claim refs/cell refs/numbers/gaps，以及
12 个 memo Key Questions 的 answer/refs/unknown/falsifier。发布前执行：

- `MissionDeliverableAuthority` 的现有数字与引用校验；
- deterministic exit-gate 四问：12 问齐全、variant-vs-consensus 明确、Anti-thesis 是完整反向观点、风险回报
  满足 playbook standards；任何 unknown 使相应项失败；
- independent verifier 一次读取**完整组装 memo、12 问、每个引用的实际文本/数值**。它只返回
  `pass|reject + closed finding codes`，不得改写正文；producer 的全部 route refs 都交给
  `independent_model_call`，在预算前排除同 family/unknown family；
- verifier 通过才发布 draft。Verifier provenance 和 verified body hash 放在已有
  `model_invocation_refs`/gate metadata；若现有 gate closed shape不够，只扩 `MissionDeliverable` 的 gate metadata，
  不建新 authority。

预算使用中央 `resolve_call_budget(config, purpose, defaults=legacy)` 与 `resolve_run_budget`；四个 producer unit
加一次 verifier 的 run bound，全部记 coverage pool。call/run budget fingerprint、prompt hash、所有 input refs
进入 WorkOrder/launch identity。默认值应在实现时按真实 prompt fixture测量后提交集中 catalog；不能沿用
Deep Insight 的 120k/3k/$0.60 作为未经测量的事实。无法预留 verifier 时整版不发布。

## 5. Cockpit 裁决与进入 active coverage

Approvals 页新增 `kind=investment_memo`，展示完整 12 节、12 问、四项 gate、gaps、verifier、模型与费用，按钮为
`approve / return_for_more_work / reject`，全部要求 rationale。

writer 新增窄操作 `decide_investment_memo`，请求绑定 memo version ref/hash 和 active mission。执行前重读：该版本
仍为 chain head、company_model 已 passed、memo 尚未决定、verifier pass、四项 gate 全 pass。写入顺序：

1. 若无 `investment_memo entered`，以 memo ref 为 evidence 写 entered；
2. approve：以 memo ref/hash 为 evidence，由 human actor 写 `investment_memo gate_passed`；
3. approve 后写 `active_coverage entered`，evidence 同时包含 memo version 和 memo stage record；
4. reject：写 memo `gate_failed`，不进入 active coverage；
5. return：不写 gate decision，保留 entered，等待新 evidence/input hash 后重稿。

三步都用由 memo ref/hash/decision 派生的稳定 idempotency key。重试必须能补齐“memo 已 passed、active 尚未
entered”的半完成状态。不要把 daily tracking 与 active coverage 混为一谈：`tracking_cadence.py:6-20` 明确
前者在 Initial Screen 后已常驻，memo approval 只是研究阶段迁移。

## 6. 最小完整切片与文件归属

### Slice A：前置阶段闭合（应先合）

- 新：`model_stage_readiness.py`、`mission_model_stage_lane.py`、launcher/CLI（也可一个 lane 顺序处理两阶段）。
- 改：`lane_registry.py` 的一行注册、部署 argv/install 接线、focused tests。
- 不改 forecast/industry authority；只读它们并写既有 stage ledger。
- 验收：从已批准 Deep Insight + framework + ready forecast 出发，industry/company 两阶段依次 entered/pass；
  一个 invariant 或 consensus gap 时停在 gate_failed；重启/重复 tick 零重复记录、零模型调用。

### Slice B：Memo producer（核心）

- 新：`investment_memo_draft.py`、`investment_memo_cli.py`、`investment_memo_launcher.py`、
  `mission_investment_memo_lane.py`、focused tests。
- 小改：`model_selection.py` purpose mapping、`model_configurations`/budget defaults、lane registry、service/install
  wiring。复用现有 dossier producer/verifier config。
- `mission_deliverable.py` 仅在 gate metadata 的 closed shape确实不足时做兼容迁移；优先零修改。
- 验收：真实临时 SQLite 从 authority inputs 生成一份 memo；所有数字/refs 可解；same-family verifier 零调用；
  verifier reject 零发布；同输入重启幂等；任一未引用但实际 prompt input 改变会重判。

### Slice C：Human decision 与产品展示

- 改：`writer_server.py`（一个 operation）、`cockpit_plane.py`、`cockpit_control.html` 和对应 tests。
- 不新增 decision table；stage ledger 是裁决记录。
- 验收：Approvals 能读 exact memo；tampered/stale/non-head hash拒绝且零写；approve 完成 memo pass + active entered；
  中途故障重试补齐；return/reject 不进入 active；automation principal 无法 approve。

### Slice D：端到端产品验收

- 更新 activation/readiness 与 owner checker，将 memo 加入目标，而不是只检查 schema/tick。
- ACN 先跑：前置两阶段 → memo verified draft → Cockpit owner decision → active coverage entered；随后证明 daily
  tracking 未中断，下一条事件仍按五词判断。
- 质量验收：人抽 10 个数字零错误；12 个 Key Questions 全部 answered 或明确 unknown；Anti-thesis 单独可读；
  variant/consensus、event pathway、期限/对冲和风险回报均可定位到 authority；总费用不超过配置 run cap。

## 7. 明确不做

- 不新增 InvestmentMemo authority、第二套阶段账本或新的审批数据库。
- 不让模型或 verifier 写 `gate_passed`，不以“文档发布成功”冒充 owner 通过。
- 不因 memo 尚未完成停止 Initial Screen 后已常驻的 tracking。
- 不把 prior memo/Excel 当 actual，也不把 Deep Insight 12 问误当 memo Key Questions。
- 不在此切片实现 Excel 导出、Feishu/Discord 投递或新 connector；它们有独立验收边界。
