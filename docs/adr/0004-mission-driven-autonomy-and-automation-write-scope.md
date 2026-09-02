# ADR-0004：任务驱动的自治与自动化写入范围

- 状态：Accepted（2026-09-02，owner 明确要求「下达任务后 7×24 自主研究」，Eve 在授权下裁决；owner 可否决）
- 日期：2026-09-02
- 适用范围：Phase 9 起所有 CoverageMission 下的自动化研究；替代 v0.1 愿景第 7 节「自主权限模型」中「更新正式模型和 thesis 属于条件自主」的表述；不推翻 ADR-0001（Thesis 人工准入）与 ADR-0003 B（transcript 语义候选只经人工审阅入库）。

## 背景

到 2026-09-01 为止，Dalton 的驱动方式是「机器提议、人批准、每一步签字」。live 上真正能写正式 Claim 的只有 SEC policy lane；Bounded Planner 的自动化身份只被授权观察（`record_observation_followup`），看到新 accession 后只能记一笔「等人处理」。8/27 之后 live Core 没有新增过一条 Claim，80 条 planner 提案、31 条 outcome 全部停在 `evidence_observed_for_review`。

owner 2026-09-02 的指令改变了驱动方向：人只下达任务（例如「建立对 US IT services 行业的首次覆盖」），系统 7×24 自主从 SEC、AlphaEngine、Guidepoint、web search 找资料，建立行业认知、公司财务模型和预测；人在关键节点看结果。这需要回答此前悬而未决的「actor 语义」问题：自动化身份能写什么、不能写什么、边界由谁定义。

## 决定

1. **任务层对象是 CoverageMission。** 人类以 `human:` principal 发布 mission（行业、公司 universe 与 tier、研究问题、交付物、来源计划、预算），mission 以 exact ref+hash 绑定一个 ResearchPlaybook、一个 ResearchConstitution 和一个 active Mandate。Planner、lane、模型引擎和 brief 从 mission 取方向，而不是从 ticker 列表取。
2. **自动化身份的写入范围写在 mission 里，且有硬上限。** `autonomy.may_write` 只能从冻结词表中选：`evidence / claim / forecast_line / model_run / research_question / observation / stage_record / forecast_reconciliation / source_discovery`。`forecast_reconciliation` 由 P9c 追加，用于预测 vs 实际的派生 Outcome；`source_discovery` 由 P9d-1 追加，用于把受控搜索及其文档清单绑定到 mission。已发布的 mission 不自动获得新词，必须由 owner 发布新版本授予。Thesis、Constitution、Playbook、Mission、Mandate、Governance policy 永远不在词表里，由合同拒绝，不靠 policy 自觉。自动化 actor 必须等于 mission 声明的 `automation_principal`；它写下的每条记录都带来源、版本 hash 和 actor，可回滚（append-only，新版本覆盖 current pointer）。
3. **人类检查点同样写在 mission 里，且不能删。** 最小集合是 playbook 标为 human_checkpoint 的阶段（Deep Insight Gate、Investment Memo）加 `thesis_admission / thesis_revision / scope_expansion / budget_expansion`。mission 只能追加检查点（如 `forecast_overturn`），不能删除。
4. **研究方法本身是版本化 authority。** 团队分析师手册转写为 ResearchPlaybook（六个冻结阶段与出口门、12 个 memo key questions、五词决定词汇、analyst level 验收标尺、tracker 类别、模型纪律、证据纪律）。playbook 是行业无关的方法；行业因果链继续留在 Driver Pack 与 Constitution。playbook 不能删除 Deep Insight Gate 和 Investment Memo 的人类检查点，不能改五词决定词汇，不能弱化「时效数字必须回指工具结果或一手 filing」。
5. **阶段推进是 append-only 账本。** 公司只能按 playbook 顺序逐阶段推进：进入第 k 阶段要求第 k−1 阶段 `gate_passed`；`gate_passed` 必须带 evidence refs；人类检查点阶段的 `gate_passed` 只接受 `human:` actor。自动化可以记录 `entered`、`gate_failed` 和非检查点阶段的 `gate_passed`。
6. **ADR-0003 B 保持不变，但范围收窄到「入库」。** transcript 语义候选仍只经人工审阅成为正式 Claim。自动化可以在 mission 内发现、获取、staging 语义候选并把「待人工 accept」记为 observation；数值 Claim 继续走 policy lane 自动提交。P9c 会评估是否把「已 accept 过同源文档的后续候选」纳入 policy 自动路径，届时另立 ADR。
7. **预算和 mandate 仍是外层边界。** mission 的 `budget`（日付费调用、日成本、AlphaEngine 24h 调用）只能收紧 mandate 与 governance policy 的上限，不能放宽；扩预算和扩 universe 是人类检查点。

## 不选的方案

- **给 `automation:bounded-planner` 直接开 lane 触发权，不建 mission。** 这只解决「观察→Claim」一条缝，仍没有对象承载「建立首次覆盖」这种指令，planner 还是只会盯 ticker。
- **让 LLM planner 读手册 prose 自行遵守。** 方法只在 prompt 里就无法验证、无法版本化、无法在 gate 上 fail closed；P8a 的 Constitution 已经证明合同化才可审计。
- **一步到位让自动化写 Thesis。** 违反 ADR-0001，也让错误自动传播到投资观点；先在 Evidence/Claim/预测层证明命中率，再谈 thesis revision 自动化。

## 影响

- P9a 落地 ResearchPlaybook 与 CoverageMission authority、writer ops、隔离 canary；不激活任何自动化写入（live 仍只有 SEC policy lane 在写 Claim）。
- P9b 起，`automation:coverage-mission` 按 mission `may_write` 范围获得 lane 触发、forecast reconciliation 和 observation 写权限；每一项 live 激活各自保留 owner gate。
- v0.1 愿景第 7 节的三层权限模型仍成立，只是「条件自主」一层的边界从 policy 描述变成 mission 合同字段。
