# Dalton 愿景复盘与下一阶段裁决 v0.9

日期：2026-08-26
状态：当前执行顺序基线；替代 v0.8 的近期顺序，不反向改写历史报告
触发：owner 指令「review 过往所有 vision 讨论，确定 next phase，然后继续」；裁决由 Eve 在授权下作出，owner 可否决

## 结论

1. **方向没有漂移。** 从 08-13 v0.1 到 08-25 S6，愿景一直是同一件事：一个持续运行的研究 OS，长期维护
   Evidence → Claim → Thesis → Driver → Question → Outcome；在 mandate、预算和版本化 policy 内默认自治，人只在越界、
   连续失败、重大观点变化、扩权/扩预算和交易这些异常节点介入；唯一交互面是 tailnet 私有 Cockpit，方向控制只用自然语言。
   08-20 的校正（「不要把 canary 的人工 gate 变成长期流程」）和 08-24 的 Cockpit 裁决都是对同一愿景的收敛，不是转向。
2. **但 13 天以来，所有正式研究记忆都留在隔离 Core 里。** live `core.sqlite` 的正式 Evidence / Claim / Thesis 仍是 0，
   只有 08-25 用户经真实 Tailscale 会话写入的 1 条 transcript correction set 和 1 条 citation binding。v0.5 定下的
   2026-08-29 门槛（1 条真实端到端 ResearchPlan + 1 条人工接受的正式 Claim）在隔离环境早已达到（08-15 人工接受、
   08-20/21 policy 自动闭环 5 家 SEC issuer），在 live 上一条都没有。
3. **live 上唯一常驻的研究循环正在坏掉。** 万华 Agenda Shadow 自 08-25 起连续两天 `PROVIDER_BUDGET_EXCEEDED`：
   Dalton 冻结 tokenizer 把整段中文数成 1 个 token（08-26 提示词 2,719 Dalton token），DeepSeek 实际计 9,284，policy
   `max_input_tokens=8000` 是按后者事后执行的；模型已经回答、费用已经入账，结果被丢弃。
4. **下一阶段定为 Phase 7「live 研究记忆启动」（切片代号 S7）**：不再新增隔离能力，把两条已在隔离环境证明的链搬上
   live Core 并让它们持续产出——① ACN transcript 首条正式 Claim（S6 收口，人工 canary）；② US IT Services 的 SEC
   季度收入增速 policy 自动闭环（零逐条审批、日预算、失败告警）。在此之前先修 live Agenda。

## 愿景脉络

| 日期 | 文档 / 决定 | 对愿景的贡献 |
| --- | --- | --- |
| 08-13 | `vision-and-architecture-v0.1.md`、Fable 独立审阅 | 自主研究分析师；8 项自主能力；Phase 0-5；按风险分层的自主权限模型；「人面对研究组合和认知，不面对任务队列」 |
| 08-14 | `architecture-debate-and-v0.2-direction.md`、`runtime-options-and-build-vs-adopt.md` | Architecture-first, implementation-thin；Core headless、事件驱动；OpenClaw 只做人机桥；Ledger 与 Model IR 分权；Capability Growth Plane 治理链 |
| 08-15 | v0.5 / v0.6 | 「没有发动机的精密变速箱」；价值只看固定成本下经人工接受的 Claim 数量、质量和对真实投资讨论的贡献；Conditional Go；08-29 门槛；允许直接服务首条 plan 的 connector/model 增量 |
| 08-20 | 愿景校正（memory 08-20 15:34） | 默认自治，人工只处理异常；不要继续堆审批层；SEC policy 自动闭环 canary 证明低风险主链不需要逐条审批 |
| 08-21 | v0.7 | breadth 先于 depth；开发过程也受 gate（一批一个 slice、CI 绿再开下一批、验证记录引用独立 runner）；第一产品是每周 5 家公司的验证简报；Gate 0-4 |
| 08-23 | v0.8、ADR-0001、planner/intent 系列 | 数据链 → Model Input Ledger → US IT Services 行业研究；ACN 为首家公司；开放式自然语言由 LLM 翻译成 typed candidate，scope/权限/预算/admission 全在 Core 或 human gate |
| 08-24 | Cockpit 架构审阅 | 单一 tailnet Cockpit；自然语言方向控制；ad-hoc 四选一路由；ACN 第一条真实轨迹；S1-S5 顺序；canary 人工动作只用于校准 |
| 08-25 | S6 决策与部署 | 「ACN 首条正式研究记忆闭环」；Cockpit 上线并完成真实人工 transcript gate |
| 08-26 | S6b、ADR-0003 | 两道结构性门：live Core 无 connector authority；CandidateStaging 只收数值候选 |

外部文章 `docs/external/thoughts-on-ai-for-hedge-fund-2026-08-25.md` 只作参考。它的两点与 Dalton 相关：组合 PnL =
idea 数量 × 命中率 × sizing（提示 Ledger 里的 idea 登记和事后评分要可度量）；文档系统作为 agent 的长期记忆（与
ContextPack / ClaimIndex 同向）。本裁决不据此改变任何顺序。

## 兑现与缺口

按 v0.1 的 8 项自主能力逐项对照：

- **形成研究议程**：Phase 1 Agenda Shadow 自 08-14 在 live 运行，仍只有万华一家，未 cutover；人工标签很少；08-25 起失败。
- **按优先级安排工作 / 拆解计划**：Bounded Planner Loop v1、Doctrine/ContextPack、LLM Planner（Qwen/Terra 15/15）
  都是 development candidate，只在隔离环境跑过；live 没有 Planner worker。
- **持续积累证据、模型和认知**：Ledger 0.2、HumanReviewAuthority、policy commit、Model Input Ledger v1 都有；
  live Core 正式记录为 0。隔离环境里 SEC 五家 issuer 的收入增速 Claim、ACN/CTSH/EPAM/IBM 的行业 evidence pack v2
  都已经存在，但没有一条进入任何人每周会读的东西。
- **检验 thesis**：thesis-impact verifier 已校准（3×30 canary），production lane 在 live 空转——live Core 没有
  ThesisVersion、没有 ACN mandate。
- **发现能力缺口并自写 skill**（Phase 4）：只有治理半边；gap detector / builder / sandbox 按 v0.5 主动冻结，正确。
- **独立检查研究质量**：verifier 校准与 wrapper selection 完成；真实研究负载上的接受率、成本数据仍为 0。
- **完成后重新规划 / 休眠**：Scheduler、outbox、durable replay 都有；没有 live 研究 WorkOrder 让它调度。
- **多 runtime**（Phase 5）：未开始，也不需要。

一句话诊断：**隔离 canary 与 live 之间有一道没人跨的墙。** live Core 没有 connector authority（S6b 已补 schema，未部署）、
没有 US IT Services mandate / driver pack / ThesisVersion、没有 SEC lane。过去 13 天每个 slice 都以「隔离验收通过、未部署」
结束，这在架构期是对的，现在已经变成主要风险。

## Phase 7 切片（按依赖顺序）

### S7a live Agenda 预算修复（本轮，development candidate）

- 新增 `provider_token_estimate.py`：按 3 条真实 DeepSeek 观测（2.38-2.42 字符/token）冻结
  `estimator:provider-input-chars-per-token:2.2`，留约 8% 余量。
- `LegacyCoveragePerceptionAdapter.build/write` 接受 `max_estimated_tokens`：超预算时按「最旧 evidence → 最旧
  catalyst → 最旧 filing」确定性丢弃，并把 fetched / dropped / estimator 写进 snapshot 的 `bounding` 记录；
  `validate_snapshot` 校验该记录与正文一致，篡改 fail closed。
- coordinator 用 policy 预算减去固定 wrapper、mandate wire 的估计和 1,000 token 的 materialization framing reserve
  作为 perception 预算（估计覆盖将要注册的完整 snapshot wire，含 bounding 记录本身）；
  materialize 后再用同一估计器做第二道预检，超预算以 `prompt_input_budget_exceeded` 失败，不再付费后丢弃；
  WorkOrder metadata 记录 `prompt_tokens`、`estimated_provider_input_tokens` 和 estimator ref。
- 不改 policy、不改冻结 tokenizer、不改 ContextPack 合同。
- 部署：CI 绿后重装 wheel 并重启 controller/writer。writer 重启会同时带上 S6b 的 connector schema（additive）。

### S7b ADR-0003 选 B（进行中，独立 worktree）

transcript 候选以 `claim_kind = qualitative` 进入 CandidateStaging 0.2 / ClaimVersion 0.2：value / unit / scale /
currency 与 numeric spec 全为 null；只接受带 exact citation binding 的 transcript evidence；只能经 explicit human review
入库，policy 路径一律拒绝。不选 A 的原因：仓库里 ACN 的 SEC 数字（USD 19.32bn、-3%）是 `human:coverage-owner`
手工登记的 evidence pack，不是 connector 数值验证产物；SEC exhibit 是 HTML，现有 numeric extractor 只有 `number` /
`count` JSON pointer，A 要先造 exhibit 解析器，本质上又回到文本抽数。08-24 已定「semantic metric 可以用
authenticated transcript 作 authority」，B 是这条原则的合同化。

### S7c writer 内获取 + 真实 ACN 落库 + brief v3

- writer 增加独立 acquisition 线程和第二组 Core 连接（`ConnectorTransportExecutor` 的 `SIGALRM` watchdog 只能在
  主线程，store executor 单线程 30s 超时），暴露 human-governance op `acquire_alphaengine_document` 与
  `stage_transcript_candidate`；Cockpit 待审页显示语义候选。
- 部署后由 owner 通过真实 Tailscale 会话触发一次 ACN 获取（digest 必须仍为 `a8a9fbff…bd96bd`，否则 08-25 的
  correction set / citation 重审），候选进 staging，owner accept，Core 写正式 Evidence / Claim。
- brief v3 用正式 Claim 确定性生成并投递，记录至少一条内容性反馈。
- **需要 owner**：把 `deploy/connector-governance/alphaengine-get-document-v1.json` 改为 `approved` 并重算 hash
  （S7c 会提供 CLI，不接受内存伪造）。

### S7d US IT Services SEC lane 上 live

- 把 Gate 1 已证明的 `get_company_facts → resolver → verifier → staging → policy commit` 参数化为 ACN、CTSH、EPAM、
  IBM（+1 家备选）的季度收入增速 lane，走 Core-hosted connector 路径进 live Core；governance policy 版本化、
  日预算、失败告警到 Discord；Cockpit 轨迹页能看到每条正式 Claim 的 accession、期间、数值和 verifier 状态。
- 这是 v0.7 定义的「发动机」：零逐条审批、可重放、每季自动更新。
- **需要 owner**：live governance policy 激活前的一次明确 go。

### S7e 第一份每周简报

由 live Core 正式 Claim 确定性生成 US IT Services 简报，投递到 Discord / 飞书；记录阅读与反馈。没有 ThesisVersion 的
公司写 `insufficient`，不补造。这是 v0.7「第一产品」的兑现，也是后续所有扩权决定的分母。

## 继续冻结

ad-hoc research（S5 剩余部分）、自动 thesis revision、Capability Growth Plane 的 builder/sandbox、第二 runtime、
Model IR 超出 v1 的部分、Agenda cutover、Gemini / Guidepoint 新 bridge、更多 Cockpit 页面——直到 S7d 在 live 连续
产出至少 4 周的正式 Claim，并有真实阅读反馈。

## Phase 7 退出门槛

- live Core：≥ 1 条经人工 gate 的 transcript 正式 Claim；≥ 5 条经 policy 自动提交的 SEC 正式 Claim；全部可重放、
  来源 / 期间 / 数字可回指；逐条人工审批数为 0（transcript 那条除外）。
- 万华 Agenda Shadow 连续 14 天没有预算类失败。
- 一份简报被真实阅读并收到 ≥ 1 条内容性反馈。
- 每条正式 Claim 的模型成本和 connector 用量有记录，日预算内。
- **止损**：到 2026-09-09 live 仍没有任何正式 Claim，则停止一切新能力开发，只保留修复这条链的工作，并重新审视架构。

## 本轮实际做了什么

- 复盘上表全部文档与 08-13 至 08-26 的 memory 记录；核对 live Core、Scheduler、model-router 三个数据库的真实数字。
- S7a 代码与测试（见上）；S7b 已派出并行开发；本文档。
- 未部署、未调真实 AlphaEngine、未写 live Core。
