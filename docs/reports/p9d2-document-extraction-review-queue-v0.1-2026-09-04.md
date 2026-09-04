# P9d-2：获取文档的人工语义抽取队列 v0.1

日期：2026-09-04
状态：已部署 live；coordinator 自动登记已生效；1035/1035
上游：[P9d-1](p9d1-alphaengine-search-driven-source-discovery-v0.1-2026-09-02.md)、
[激活与首次发现](p9d1-live-activation-and-first-discovery-2026-09-04.md)、[ADR-0003](../adr/0003-transcript-candidate-admission.md)、
[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)

## 这一片解决什么

P9d-1 之后，discovery 循环自主「搜→取」：9/4 当天 126+ 文档发现、7 个完整获取入 authority。但获取完成
之后没有任何对象告诉人「这份原文等你抽取语义候选」——ADR-0003 B 要求 transcript 语义 Claim 只能经
人工 correction/citation/stage/accept 链入库，这一链的入口完全靠人自己想起去看。P9d-2 把队列补上：

**每个获取完成的文档自动登记一条人工抽取 review；只有人能把它关掉。**

- automation（`source_discovery` scope 既有授权，无需新词表或 mission v3）在 settle `acquired` 时
  注册 `coverage_mission_document_reviews`，状态 `awaiting_human_extraction`，绑定 mission 版本、
  公司、文档 ref、discovery 账本行；每 mission 版本 + 文档恰好一条（幂等）。
- 人工裁决两种：`extraction_staged`（绑定经既有 S7c-2 路径 stage 的 `candidate-claim-version:…`，
  writer op 先在 CandidateStaging authority 核对该 ref 处于 staged/committed 状态）或
  `dismissed`（必带 rationale）。automation actor 被合同拒绝；已裁决的 review 幂等重放、永不重开。
- mission 版本在此期间被替换时，注册会因 grant 重推导失败而如实拒绝（tick 结果记
  `not_registered:<Error>`），不产生孤儿队列。
- `mission_document_reviews` 读 op 对 human 与 mission automation principal 开放；
  `resolve_mission_document_review` 仅 human-governance。进度投影新增每公司
  `awaiting_extraction_review_count`。

## 冻结边界（不变的东西）

- ADR-0003 B 完整保留：automation 不 stage 候选、不写 correction set、不绑 citation、不 accept；
  本片只把「等人」变成显式排队。
- 表 append-only（无删除）；状态迁移只在 authority 事务内；直写被 SQL trigger 拒绝。
- 不新增 may_write 词表词；`forecast_overturn` 等人类检查点语义不受影响。

## 验收

- 专项：coordinator 流（acquired → review fresh → replay duplicate → automation 拒绝裁决 →
  人 dismissed → 队列清空 → 再裁决 duplicate）+ writer ops（读/权限/未配置 staging 的拒绝）+
  既有 discovery/mission/writer 回归全绿；全仓 **1035/1035**（+2）。
- live Core 只读副本 canary：用真实已获取文档 `alphaengine-doc:320000610044534` 走 register→replay→
  人 dismissed→automation 拒绝；integrity ok；Claim/Evidence 数量不变（6/6）、0 网络、0 付费、0 live 写入。
- 部署后首个 tick 即在 live 生效（writer 自建新表，additive schema）。

## 同日 live 记录（非本片代码）

- **万华 Agenda Shadow 退役**：owner 指令「这个不都是过去时了吗」。发布
  `agenda-control-version:wanhua-shadow-retired:1`（paused，version 3，human:lumos）；心跳 agenda 块
  由 error（同日幂等 conflict）变为干净的 `paused`。不再有每日模型调用；昨天为修截断发布的
  agenda policy v5（output 4000）随退役一并失去服务对象，保留为不可变记录。
- 预算软上限边界：9/4 AE 共享计数收在 31/30——最后一篇文档多页获取（每页一次调用）可越过整数
  上限，因为 launch 检查发生在调用记录之前。如实记账、无供应商超额（文档单位口径仍受 30/24h
  probe 上限约束）；如需硬上限可在后续切片把页数计入预留。
- discovery 循环 9/4 全天：126+ discovered、7 acquired（含部署孤儿 2 个，明日起 1 天间隔自动重试）。

## 未做与下一步

- Cockpit 审阅页尚未展示该队列（读 op 已就位）；下一片可与 P9d-3（LLM 起草抽取建议）合并设计。
- web search 源（先于 Guidepoint）、M2 市场数据（等 owner 解冻）不变。
