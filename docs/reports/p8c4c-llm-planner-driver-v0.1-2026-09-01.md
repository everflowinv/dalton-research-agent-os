# P8c-4c LLM Planner 接入驱动循环 v0.1

日期：2026-09-01  
状态：已部署 live；DeepSeek V4 Flash 已在 live 亲自撰写并驱动 probe 提案

## 结果

Tier 1 循环的提案者从确定性 planner 扩展为「LLM 优先、确定性兜底」：

1. **writer 新增 CORE ops**：`llm_planner_prepare`（disposition：硬控制态 Core 直行，否则冻结模型
   WorkOrder）与 `llm_planner_advance`（模型文本 → 严格候选解析 → Core 重验 exact ContextPack /
   WorkOrder / ResultEnvelope / ModelInvocation 溯源 → PlannerProposal 0.3）；以及
   `llm_planner_execute`——**模型调用在 writer 进程内执行**（记账写入同一 Core，driver 进程不开
   Core），一次 RPC 完成 prepare→模型→advance。writer CLI 新增 `--planner-routing-policy` 等 7 个
   flag，LaunchAgent 从 service config 的 bounded_planner 块派生（单一配置源）。
2. **driver LLM 分支**：config 成对增加 planner 模型接线（routing policy / credential slots /
   router db / broker socket+key+client id+expected agent id / 单次费用上限 0.5 USD）；每 tick 先尝试
   一次 LLM，proposal_ready 才用其提案，任何失败/不可用**回退确定性 doctrine planner**（安全网语义
   不变）。
3. **production routing policy**：live router 注册
   `model-routing-policy-version:dalton-openclaw-planner:1`（唯一 pin `profile:deepseek-v4-flash`，
   即校准首选 V4 Flash，credential slot `openclaw:deepseek`）。

## live 排障（三处真实缺口，均已修复 + 测试）

1. **同一连接假设**：coordinator/worker 构造与 provenance 链把 Scheduler 表当 Core 表读
   （`authority.connection` 查 `scheduler_work_orders`）——writer 的 Core/Scheduler 分文件部署下必然
   "provenance is missing"。修复：provenance 重验接受显式 Scheduler 连接（coordinator 传入，
   单文件历史路径不变）。
2. **expected_agent_id 缺失**：writer 内 adapter 未传 dedicated agent（chem），broker 拒绝归属
   （复用了 thesis-impact 的教训）；端到端补 `planner_expected_agent_id` 配置链。
3. live 观测：修复前 3 次模型调用成功但 bind 全败（缺口 1），修好隔离副本后全链通过再部署。

## live 验证（2026-09-01 晚）

- 隔离 live 副本全链：V4 Flash 撰写候选（选 CTSH、给出真实 rationale），Core 绑定为
  `planner:llm-research-planner:0.1` 的 PlannerProposal；
- **live loop v5（`bounded-planner-loop-version:f8fcbfd5…`）**：driver tick 经 LLM 分支完成
  probe 提案→准入→执行→outcome；提案记录中 **2 条由 `planner:llm-research-planner:0.1` 撰写**，
  确定性 planner 作为兜底继续承担其余轮次。

## 验收

driver LLM 回退测试 1（无 planner 配置的 writer 上 RPC 失败→确定性兜底端到端）+ config 校验；
专项/邻接 24/24；**全仓 961/961**（三个部署迭代，每轮全仓绿后上线）。

## 边界与下一步

- 模型只在已准入 ContextPack/coverage 内选择，不能输出模板、参数、权限、预算或 source；单次调用
  WorkOrder 费用上限 0.5 USD，每 tick 至多 1 次。
- 至此 Phase 8 P8c 的「Agenda → 视图/上下文 → planner（LLM+确定性）→ SEC probe → outcome →
  注意力 → brief」全链在 live。下一观测点仍是 **9/3 首个自动 brief**；剩余项：观察问题→lane 触发
  的 owner 裁决、P8d 评测集（等真实反馈）。
