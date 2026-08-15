# ResearchPlan 下游逐项 admission coordinator（2026-08-15）

## 状态

开发候选，未部署。本切片只实现 ResearchPlan 四节点任务树的 authority-bound admission；没有访问真实 SEC、
创建凭据或能力租约、写正式 Ledger、修改 cron，也没有接上 resolver/verifier/candidate staging 的真实 executor。

## 结果

1. **只接受权威记录**：caller 只能提交 plan ref 和 upstream WorkOrder ref。coordinator 每次重读并核对 exact
   ResearchPlanVersion、approval、start、WorkflowRunVersion、WorkOrderLink、Scheduler WorkOrder、policy、
   attempt、lease、formal result 和 ResultEnvelope；caller 提供的 success boolean 或 opaque payload 没有授权力。
2. **connector 结果链**：根节点成功后，coordinator 从独立 `ResearchCoordinatorStore` 重读 exact compiled
   request 和 completion receipt。v0.2 receipt 还会从 Core runner journal 重读 actual request、完整事件 hash 链
   和终态 `responded` response；receipt 的 source/artifact refs 必须与 Scheduler ResultEnvelope 一致。
3. **内部节点输出证明**：resolver、verifier 和 candidate staging 的 ResultEnvelope 必须携带封闭、可哈希的
   `ResearchPlanStageOutput`，绑定 exact plan、step、output contract、直接上游 WorkOrder/result，以及该阶段
   规定的 typed ref/hash 记录。任意 `{success: true, payload: ...}` 不能推动下一节点。
4. **逐边放行**：每次只 admission 直接子节点；不能越级，也不能批量 enqueue。启动仍只 enqueue 根 connector，
   保留 Planner 现有行为和 API。
5. **重放与故障**：子 WorkOrder 身份和 idempotency key 均由不可变 plan 派生。重复调用返回同一节点；在 enqueue
   后崩溃，重放仍只有一棵任务树。既有子节点只要与 plan 有任何偏差就 fail closed。
6. **敌意输入**：错误 plan/workflow/upstream/result、attempt/formal result/receipt/WorkOrder 篡改、缺失 receipt、
   非终态、失败、过期或超过 plan attempt 上限都不能 admission。

## 主要文件

- `src/dalton_core/research_plan_coordinator.py`
- `contracts/research-plan-stage-output.schema.json`
- `tests/test_research_plan_coordinator.py`
- `src/dalton_core/__init__.py`

## 验证

- coordinator 专项覆盖严格逐边执行、重复重放、enqueue 后崩溃、错误 plan、越级、opaque internal success、
  v0.1/v0.2 connector receipt、runner journal、failed/retry-exhausted、attempt/formal result/ResultEnvelope/receipt/
  child WorkOrder 篡改，以及 tree-status 缺失 attempt history。
- coordinator 专项 12/12；相邻 Planner/Scheduler/research coordinator/packaging 回归 55/55；Python 全量
  519/519；OpenClaw model broker 15/15。
- `compileall`、106 份 JSON contract 解析和 `git diff --check` 通过。固定 `SOURCE_DATE_EPOCH=1700000000`
  的两份 wheel 逐位一致，SHA-256 均为
  `cbbd4feb139e764f7217fd6644001a8e2df2d6c9c5f3aae8da2da3f961052364`，大小均为 677,254 bytes；Python 3.13
  干净 venv 安装、公开导入和 packaged `research-plan-stage-output.schema.json` 读取通过。

## 未决边界

- `ResearchPlanStageOutput` 已绑定 typed ref/hash，但本切片没有从 resolver、verifier、candidate staging 各自的
  authority store 重读记录正文；真实 executor 接线时必须完成这一步，不能把 synthetic proof 当作研究产物。
- Scheduler 当前没有独立 `cancelled` 状态；本切片只对现有 failed、expired、retryable 和 plan attempts exhausted
  语义 fail closed。取消、park/resume 不在本切片范围。
- 本切片证明 admission 控制语义，不证明四步计划已经真实执行，也不证明投研产物质量。
