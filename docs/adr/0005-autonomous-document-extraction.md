# ADR-0005：文档抽取与 Claim 准入由自动化完成，人只定目标、掌舵与问答

- 状态：Accepted（2026-09-07，owner 明示：「我不需要批准这些东西；我只设定研究的最终目标、必要时掌舵、随机问答；
  涉及系统变更（例如写新工具）才需要我批准」；Eve 在授权下裁决；owner 可否决）
- 日期：2026-09-07
- 适用范围：Phase 9 起所有 CoverageMission 下已获取文档（AlphaEngine、public web，后续 Guidepoint）的抽取、
  候选 staging 与定性 Claim 准入。修改 ADR-0003 B 中「transcript 语义候选只经人工审阅入库」在 mission 文档上的适用；
  不改 ADR-0001（Thesis 人工准入）与 ADR-0004 的写入词表、人类检查点和预算边界。

## 背景

到 2026-09-07 为止，web lane 已全自主：搜索→抓取→核验原文→审阅队列。但队列本身设计为「模型起草、人逐条校订引文并
staging、人逐条 accept」（P9d-3b），于是 cockpit 里堆着一整页「待抽取文档」，且 live 上连起草模型配置都没装。owner 看到后
明确否定了这个角色：人不逐条批准文档；人只做三件事——设定研究目标、必要时掌舵、随机问答；只有系统变更需要批准。

这与 08-20 的愿景校正（「默认自治，人工只处理异常；不要继续堆审批层」）和 ADR-0004（自动化按 mission `may_write` 写入）
一致。live mission v4 已授予自动化 `evidence / claim / stage_record / observation / source_discovery`。缺的不是权限，是把
「起草→staging→准入」这条链从人工动作改成 mission 内的自动化动作。

## 决定

1. **自动化身份在 mission 授权内完成整条抽取链。** 对已获取的 mission 文档，`automation_principal` 可以：
   ① 在 mission 日预算内调用模型起草定性建议（每个精确窗口一个 WorkOrder，沿用 P9d-3b 的预算准入、路由、账本）；
   ② 把建议绑定为精确引文并 staging 为 `qualitative` 候选；③ 按 policy 把候选准入为正式 Evidence / Claim。
   三步都要求 mission `may_write` 含 `claim`、`evidence`、`stage_record`，来源在 source_plan 中为 `connected`。
2. **人类检查点收敛为：发布/改版 mission（目标与掌舵）、问答、系统变更。** 系统变更指新增或修改工具、connector、
   模型/路由/预算 policy、governance 记录、universe 与预算扩张；这些仍走 owner gate。逐文档、逐候选的批准不再存在。
   cockpit 的审阅队列变为可观察、可驳回、可纠正的账本视图，而非待办清单。
3. **质量约束不放松，只是执行者换了。** 每条自动化 Claim 必须：绑定原文的精确字节级引文（span + sha256 + 渲染器身份）；
   记录 producer、model invocation、route、mission 版本 hash；`value/unit/scale=null`（数字仍只走 SEC policy lane）；
   append-only、可回滚（人可驳回并发新版本）。模型输出经闭合 schema 校验，越界即拒绝、不重试付费。
4. **ADR-0003 B 的范围收窄。** 「语义候选只经人工审阅入库」仍适用于人工发起的 transcript 修正流程；对 mission 自动
   获取的文档，准入改为 policy 自动准入。人工 accept 路径保留，作为纠错与例外通道。
5. **Thesis 层不在本 ADR 内。** ADR-0001 的 thesis 人工准入暂不变；owner 的原则（只批系统变更）意味着它也应自动化，
   但 thesis 是「重大观点变化」节点，另立 ADR 决定。
6. **预算与 mandate 仍是外层边界（ADR-0004 §7）。** 抽取模型支出计入 mission 的 `max_daily_paid_calls` 与
   `max_daily_cost_usd`，与 owner 日上限共用同一账本；预算用尽即停，等窗口滚动，不静默降级。

## 不选的方案

- **保留逐文档人工批准，只把起草自动化。** 起草物无人处理即浪费预算，队列仍堆积；与 owner 的角色定义直接冲突。
- **让人批准「抽取策略」再由机器逐条执行。** 策略本就是 mission + playbook + governance policy，已由人发布；不再加一层。
- **一步到位自动写 Thesis。** 违反 ADR-0001；先在 Evidence/Claim 层证明命中率与可回滚。

## 影响与实施

- **P9d-17a**：装上 live 的抽取模型配置（路由 policy、预算 policy、broker）；起草改为 mission 内的自动化子进程，
  每 tick 起草有限窗口数；cockpit 显示起草物。
- **P9d-17b**：自动化 staging 与 policy 准入（AlphaEngine 文档：沿用 transcript correction / citation / CandidateStaging
  链，新增自动化可用的 `verified_raw_span` 变体与 policy commit 决定）。
- **P9d-17c**：public-web 来源的 citation authority 与 staging 路径（原 P9d-16）。
- cockpit「待抽取文档」页改名为账本视图；人工 stage/accept 保留为例外通道。
