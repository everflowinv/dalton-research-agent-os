# P8c-4b Doctrine ContextPack 接入驱动循环 v0.1

日期：2026-09-01  
状态：已部署 live；loop v3 已准入，首轮验证 doctrine 绑定提案真实生效

## 结果

Tier 1 循环的提案现在可以绑定 exact PlannerContextPack（doctrine 透镜冻结），为 LLM planner
（LLMPlannerCandidate 0.1 要求 exact ContextPack）铺平前置：

1. **writer 新增两个 CORE ops**：`materialize_bounded_planner_context`（loop + doctrine pack
   exact ref/hash + as_of → 冻结问题、doctrine 透镜、outcome 历史、directive、剩余预算与
   ProbeTemplate catalog）与 `bounded_planner_propose_next_with_context`
   （exact context 重验后提案；human directive 优先，透镜只能在已准入 coverage item 内重排）。
2. **driver doctrine 模式**：config 新增 `doctrine_pack_version_ref/hash`（须成对配置）；启用时每 tick
   先 materialize 再 propose_with_context；materialize 冲突（如 pending round）如实记
   `doctrine_context_unavailable:*` 并跳过，不回退、不静默。
3. **live（2026-09-01）**：service config 绑定 doctrine pack v1
   （`doctrine-pack-version:13a018582c…`）；loop v3（`bounded-planner-loop-version:1db56b1f…`，
   prior=v2，同五家 coverage + 概念候选）准入。**首轮真实验证**：driver 经
   materialize→propose_with_context 完成 probe，提案记录携带
   `planner_context_pack_ref: planner-context-pack-version:be6eb4d3…`，outcome observed、
   observation unchanged（与 v2 同 accession，diff 正确不重复提问）。

## 边界

- 当前 lens 的 `priority_topics`（"new bookings trajectory" 等）与 coverage item ref
  （`coverage:revenue-growth:*`）不重合，重排惰性（回退 uncovered[0]）——排序要生效需要 loop 版本把
  coverage item 命名对齐透镜主题，属于后续 loop 版本的准入选择，不是代码缺陷。
- LLM planner（DeepSeek V4 Flash）仍未接入：那是下一个切片，需要把 development-only 的 planner
  routing policy 提升为 production 并接入候选校验链。

## 验收

driver doctrine 模式 e2e 测试 1（提案携带 context 绑定跑到终态）+ config 校验；专项/邻接 21/21；
**全仓 960/960**；CI 绿色。

## 下一步

1. 9/3 窗口值守（首个自动 brief）。
2. P8c-4c：LLM planner 接入（planner routing policy 提升为 production + LLMPlannerCandidate 校验链
   + driver 分支）。
3. 观察问题 → lane 触发的策略裁决（owner 决定 lane binding actor 语义）。
