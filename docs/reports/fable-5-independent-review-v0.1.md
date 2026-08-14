# Fable 5 对 Dalton Research Agent OS v0.1 的独立架构审阅

> 日期：2026-08-13  
> 性质：只读独立审阅；未修改 Dalton live 系统  
> 输入：愿景与架构规划 v0.1、Dalton Coverage OS 架构说明、AGENTS、coverage 配置、核心脚本与 SQLite schema  
> 复核：Eve 已在 2026-08-13 复核文中五项现状判断：thesis upsert 覆盖旧内容、investment view 可被 CLI 直接更新、DB 无 usage/cost 账本、next_check/valid_until 无消费者、当前全局并发实际为 1。均与 live 代码一致。

以下保留 Fable 5 的完整审阅原文，作为后续 v0.2 取舍依据。

已完整读取六份材料并检查了 coverage.db schema。以下是独立审阅结论。

---

# Dalton Research Agent OS v0.1 独立架构审阅

## Direct Answer（3 句结论）

1. **方向正确，但 v0.1 是一份"大教堂图纸"**：它把一个尚未验证核心假设的系统设计成了 10 层平面 + 20 余个实体 + 完整 skill 流水线，而真正的核心假设只有两个——"模型能基于 Ledger 状态选出人类认可的议程"和"verifier 能抓住真实错误"——这两个都是 prompt + 数据问题，几周的 shadow mode 就能验证，不需要先建 Research Kernel。
2. **应扩展 Coverage OS，而不是自研 runtime-agnostic Kernel**：在 3 家公司、单机、单 agent 的规模下，"Kernel" 就是 6–8 张新表、2 个新 prompt、几个 CLI 子命令；Execution Fabric 适配协议、DeepSeek Harness、Temporal/Postgres、知识图谱全部应明确推迟。
3. **在授予任何自主权之前，必须先修三个现有硬伤**：`theses` 表 upsert 会覆盖历史（直接违反自家 4.5 审计原则）、`add-decision` 一条 CLI 命令即可无门控改写 `companies.investment_view`、全系统没有任何 token/费用 usage 记录——没有成本账和不可变历史的系统谈不上"可审计、成本受控的自主"。

---

## 一、认同 / 反对 / 遗漏 / 建议

### 认同（且应坚持）

- **研究状态独立于模型和 harness**（4.1）。这是整个规划里最重要的一条，现有 SQLite Ledger 已经在正确方向上。
- **事件驱动 + 有界工作周期，反对常驻 Session**（4.3）。`coverage_run.py` 的"先查队列、有活才唤醒、fresh session"模式是对的，应保留为唯一唤醒路径。
- **容量池（portfolio constraints）防新闻吞噬**（5.4）。这是防"新闻上瘾"的正确机制，比任何打分公式都可靠。
- **不用伪精确分数**（4.7）。ordinal + 事后校准是对的。
- **五种 decision 词汇 + Deep Insight Gate** 已被实践验证，应作为不变的对外协议保留。
- **Phase 0/1 先记录、先 shadow** 的演进顺序本身正确。

### 反对（主动挑战）

1. **反对"自研 runtime-agnostic Research Kernel"作为近期工程目标**（9.1）。这是过早抽象。当前只有一个 runtime（OpenClaw）真正在生产运行，为一个不存在的第二 runtime 设计统一执行协议（start/checkpoint/heartbeat/cancel/resume/collect + side-effect ledger + durable child id）是在解决没有的问题。正确做法：把这些概念作为 **tasks 表的新列和纪律**落进现有 Coverage OS，等第二个 runtime 真的接入时再抽协议——抽象应从两个具体实例中提炼，而不是预先设计。
2. **反对 Ledger 一步到位建 20+ 实体**（5.3）。`claims` 与 `evidence` 分开、`unknowns` 与 `research_questions` 分开、`side_effects` 独立成表……在样本量为个位数时，每张空表都是维护负债。MVP 只需新增 6–8 张表（见下文数据模型）。
3. **反对 Phase 4 的 skill shadow/canary 全套流水线**。shadow/canary 是为高流量生产系统设计的统计工具；Dalton 的 skill 调用量（每天个位数）根本不足以让 canary 产生统计意义。单人审批成本极低，第一年 skill 晋级全部人工批准即可，整套自动晋级机器应删到只剩"隔离目录 + fixture 测试 + 人批"。
4. **反对把"信息增益 × 决策影响 × 不确定性"当作排序公式**，哪怕声明是 ordinal。乘法结构会诱导 planner 系统性偏好投机性大题目（三个维度都自评为高）。应改为**字典序规则**：硬约束（截止日、falsifier 被触发、human override）→ 池配额 → 池内按单一 ordinal 排序。规则可审计、可对抗博弈，公式不行。
5. **部分反对"每项候选生成完整 agenda card"**（含预期信息增益、成本、风险、执行策略等十余字段）。为每个候选做这么重的元推理，本身就是成本黑洞和自我合理化的温床。候选卡应极薄（问题 + 触发源 + 决策关联 + 截止），只有**入选后**才由 Planner 展开成 work order。

### 遗漏（v0.1 没写或写轻了）

1. **成本账本完全缺失**。现有 DB 无任何 usage/cost 表，v0.1 虽提到 `budgets/usage` 但排在 Phase 5 才认真对待。这个顺序反了：**成本计量必须先于自主权**，否则"成本受控"无法证伪。每次模型唤醒必须落 tokens、美元、时长、触发源。
2. **`theses` 无历史**。`command_upsert_thesis` 用 `ON CONFLICT DO UPDATE` 直接覆盖旧 statement/confidence/falsifier，version 号加一但旧内容永久丢失。这不是小 bug，是与"每项认知变化可回答旧观点是什么"直接矛盾的架构缺陷，必须最先修。
3. **`investment_view` 无 commit gate**。`add-decision --new-view` 直接 UPDATE companies 表，worker 在一次任务里就能改正式投资观点。v0.1 设想的 Commit Gate 在现有代码里完全没有对应物，且没被列为 Phase 0 工作。
4. **`next_check` 与 evidence `valid_until` 没有消费者**。schema 里有 `valid_until` 和 `next_check`，但审阅的脚本中没有任何 job 由"证据过期/检查条件到期"生成任务。**这恰恰是自主议程最便宜、最确定性的来源**：到期驱动的维护任务不需要任何模型判断就能填满议程底座，v0.1 却把它埋在候选来源第 6 条。
5. **verifier 的"验证者校准"缺失**。v0.1 详细设计了 verifier 检查什么，但没设计**如何知道 verifier 本身在工作**。必须周期性注入已知错误（seeded errors：改一个数字、删一个来源）测 verifier 检出率；verifier pass rate 长期接近 100% 应触发告警而非庆祝。
6. **模型文件不可 diff**。Excel 是 driver 和估值的权威，但系统无法机器读取"model delta"。只要 driver 还锁在 xlsx 里，"事件 → driver → 模型 → 估值"的联动就只能停留在文本描述。缺一个 `drivers` 表或每版模型的结构化导出（named ranges → CSV/JSON）。
7. **全局并发度 = 1 是隐式设计**。`ready_tasks()` 在任何任务 leased 时返回 0，等于单飞行度串行。这在当前是合理的成本闸门，但 v0.1 通篇谈并行多 agent 却没意识到现有系统的这个隐式约束，应把它显式化为可配置参数。

### 建议（框架级重述）

v0.1 把智能放在"Agenda Engine 大脑"里；我建议反过来：**boring kernel, smart edges**。调度核心尽量确定性——一个 question backlog + 到期触发 + 池配额 + 字典序选择，这部分不需要模型；模型的智能只用在三个刀刃上：(a) 把新信号提炼成好的 research question（进 backlog），(b) 执行研究本身，(c) 验证与反方挑战。这样"自主形成议程"退化为一个可审计的排队问题，而不是一个需要被信任的黑盒 planner。九天闲置事故的教训不是"需要更聪明的 planner"，而是"需要一个永不枯竭的确定性任务底座（到期检查 + 证据过期 + 催化剂日历）"。

---

## 二、对 12 个关键问题的直接回答

**1. 最小且正确的核心抽象？**
四个：`Question`（带决策关联和"何为足够答案"的一等公民未知项）、`WorkOrder`（已有 tasks，补预算/幂等声明）、`BeliefVersion`（thesis/driver 的 append-only 版本链）、`AgendaDecision`（每轮选了什么、为何不选其余的持久记录）。过早的抽象：Execution Fabric 协议、知识图谱、skill canary、`claims` 与 evidence 分表。缺失的抽象：`UsageRecord`（成本）、`CalibrationRecord`（预测 vs 实际）、机器可判定的 `next_check` 条件。

**2. 控制循环如何避免自动造任务、局部最优、新闻上瘾、无休止研究？**
四条纪律：(a) 每个 question 必须溯源到有限根集合——mandate 覆盖要求、falsifier、催化剂日历、证据过期、human override；reflection 生成的新问题带 TTL，若 N 轮未入选自动休眠。(b) 每轮 agenda cycle 有硬预算（每日每池最多 K 个选择、$X 上限），预算用尽即休眠，不存在"再看一眼"。(c) 新闻在成为候选前先过 materiality 阈值（现有 price/volume threshold 机制已是雏形），且 event-response 池配额固定。(d) 停止条件在问题创建时定义："足够答案"标准 + 边际收益规则——同一 question 连续两个 work cycle 未产生任何 thesis/model delta，自动降级休眠并写明唤醒条件。

**3. Agenda 选择、资源分配、停止条件如何形式化和校准？**
选择 = 字典序：硬约束 → 池配额（event/首次覆盖/深研/维护四池，比例可配，未用容量可借）→ 池内单一 ordinal（H/M/L 决策影响）。每轮持久化选择与未选原因。校准只用二元事实："这项工作最终是否改变了一个 decision-relevant belief（有无 thesis/model delta）"+ 人类周度对 agenda 的 agree/disagree 标注。预测的 H/M/L 与实际二元结果做混淆矩阵，季度复盘调规则——不调公式，因为没有公式。

**4. 各存储保存什么、权威边界在哪？**
- **Ledger（SQLite）**：工作状态、belief 版本、决策、证据索引、议程记录、成本——"我们相信什么、在做什么"的唯一权威。
- **Event log**：同库一张 append-only `transitions` 表即可（谁、何时、什么状态变化、为何），不需要独立事件系统——"发生过什么"的权威。
- **Artifact store（文件 + git）**：报告、wiki、Excel 全文，Ledger 只存 path + hash——"内容"的权威。
- **知识图谱**：不建。wiki 双向链接 + evidence 外键就是图谱，图数据库是负资产。
- **模型文件**：数字的权威在 Excel，但喂给 thesis 的 driver 值必须镜像到 `drivers` 表才可 diff；冲突时以 Excel 为准并告警。

**5. Planner/Worker/Verifier/Commit 拆 actor？**
拆**记录和上下文**，不必拆模型。同源相关性错误的解法按性价比排序：(a) verifier 第一层是**确定性脚本**——来源 URL 可达、数字与引用来源一致、算术复算、单位口径检查，这层与模型无关，抓住大部分事实错误；(b) 模型层 verifier 拿原始来源而非 worker 叙述、用对抗性 prompt；(c) 仅对重大 commit 用第二模型家族复核；(d) 人类抽样审计 + seeded-error 检测 verifier 本身。防 grader gaming：worker 只见 verifier contract 不见评分细节。防无限返工：revise 最多 2 轮，之后强制 `blocked` 升级人类，且 revise 必须引用具体 finding id 做局部返工。

**6. Thesis 数据结构与更新协议？**
拆三张表：`theses`（不变身份：company、side、机制一句话）、`thesis_versions`（append-only：statement、机制与关键假设、ordinal confidence、driver 引用、隐含预期/市场定价、催化剂+日期、evidence 引用集、change_reason、prior_version_id）、`falsifiers`（独立行 + 状态机 `untested → holding → pressured → triggered`，各自带监测指标和阈值)。更新协议：worker 写 staging delta → verifier 结果挂载 → commit 事件生成新 version 行 → 旧版本永不修改。`confidence` 保持四档 ordinal，禁止小数概率，版本间只比较方向（升/降/持平）。

**7. Skill 自写的落地？**
可全自动：在隔离 proposals 目录写代码、跑 fixture 测试、静态检查、生成 eval 报告。永不自动：晋级到 live 目录、任何权限/网络/凭据/外发能力授予、批准自己写的 skill。第一年不建 shadow/canary——调用量撑不起统计，人工批准一个 skill 花 10 分钟。回滚 = git revert + skill 版本号记录在每个 work run 里（这个"run 记录 skill 版本"是必须的，否则无法归因）。

**8. Human steering 如何不污染系统？**
两条唯一通道：(a) `priority_overrides` 表——带 provenance、TTL、影响范围，只作用于议程排序，到期自动失效；(b) 人类陈述的事实进 `evidence` 表且 `source=human:<who>`，与其他证据同等接受 verifier 检验，人类的观点分歧不改 confidence 而是生成一个 challenge question。绝对禁止：直接编辑 thesis/decision 历史。这样"人说先做聚氨酯"改变未来，不改写过去。

**9. 扩展 Coverage OS 还是重写 Kernel？**
**扩展**。分层：SQLite Ledger（加表）= 状态权威；`coverage_os.py` / `coverage_run.py`（加子命令和 mode）= kernel 逻辑；OpenClaw = cron、频道、模型路由、权限、常规执行——它是执行器和界面，不是状态宿主（现状已满足）；DeepSeek Harness = 整体推迟，等出现一个明确需要动态并行的任务再作为一个 worker 类型接入；Temporal/Postgres = 触发条件明确（多机 worker、跨天 DAG、多行业 agent 共享 kernel），当前三条件均不满足，SQLite WAL 单机绰绰有余。v0.1 第 9.4 节自己的"不要过早引入分布式"判断是对的，应同样适用于它自己的 Kernel 设想。

**10–12** 见下文路线图、失败模式和待拍板问题。

---

## 三、优先级排序的架构决策（8 条）

| # | 决策 | 一句话理由 |
|---|------|-----------|
| 1 | 就地扩展 Coverage OS；不建独立 Research Kernel 代码库 | 抽象从实例中提炼，不为假想的第二 runtime 设计协议 |
| 2 | 先修审计硬伤：`thesis_versions` append-only、`investment_view` 加 commit gate、全库 `transitions` 事件日志 | 不可变历史是自主权的前提，不是 Phase 3 的奢侈品 |
| 3 | 自主执行开闸前先落 `usage` 成本表 + 全局 kill switch（Ledger 里一个 pause 标志，`coverage_run.py` 每次先查） | "成本受控"必须可测量、可一键停 |
| 4 | 议程核心确定性化：question backlog + 到期/过期/催化剂触发 + 四池配额 + 字典序选择；模型只做信号提炼和池内排序 | 防自动造任务和新闻上瘾靠规则，不靠 planner 自觉 |
| 5 | Verifier 三层：确定性脚本 → 模型对抗检查 → 人类抽样 + seeded errors；revise 上限 2 轮 | 同源相关性用非模型手段破，verifier 自身必须被校准 |
| 6 | Thesis/falsifier/driver 结构化拆表，driver 值从 Excel 镜像出来变得可 diff | 没有可 diff 的 driver，"事件→模型→估值联动"是空话 |
| 7 | Human steering 仅走 override（带 TTL）和 human-attributed evidence 两条通道 | 改未来不改历史 |
| 8 | 明确推迟：DeepSeek Harness、Temporal/Postgres、知识图谱、skill 自动晋级、Execution Fabric 协议 | 每一项都有清晰的"何时再启动"触发条件，现在都不满足 |

---

## 四、最小可实施数据模型与状态机

在现有 11 张表上**新增 7 张**（不动现有表结构，除 theses 迁移）：

```
questions        (id, question_key, company_slug, chain, text,
                  origin ∈ {mandate, falsifier, catalyst, expiry,
                            reflection, human, event},
                  origin_ref, decision_use TEXT,          -- 回答后影响什么判断
                  sufficient_answer TEXT,                 -- 停止条件，创建时必填
                  impact ∈ {H,M,L}, pool ∈ {event, coverage, deep, maintain},
                  status, ttl_at, wake_condition, created_at)

agenda_cycles    (id, ran_at, trigger, budget_json,
                  selected_json, rejected_json,           -- 含未选原因
                  next_review_at, usage_ref)

thesis_versions  (id, thesis_id, version, statement, mechanism,
                  confidence, implied_expectation, catalysts_json,
                  evidence_refs_json, change_reason,
                  verifier_ref, committed_by, prior_version_id, created_at)
                  -- append-only；现有 theses 表退化为身份+当前版本指针

falsifiers       (id, thesis_id, text, metric, threshold,
                  state ∈ {untested, holding, pressured, triggered},
                  last_checked_at, next_check_condition)

verifications    (id, task_id, verdict ∈ {pass, conditional_pass,
                  revise, blocked, reject},
                  findings_json, deterministic_checks_json,
                  verifier_kind ∈ {script, model, human},
                  revise_round INT, created_at)

usage            (id, ref_kind, ref_id, model, tokens_in, tokens_out,
                  cost_usd, wall_seconds, created_at)

transitions      (id, entity_kind, entity_id, from_state, to_state,
                  actor, reason, created_at)              -- append-only 全局事件日志
```

外加 `config/coverage.json` 新增：`pools`（四池配额）、`budgets`（日/月 token 与美元硬上限）、`paused` 标志。

**状态机（比 v0.1 简化）：**

```
Question:  open → selected → in_work → answered
                ↘ deferred(ttl)  ↘ dormant(wake_condition) → open
Task:      沿用现有 ready → leased → done/failed，新增
           done → verifying → {committed, revise(≤2) → ready, blocked}
Thesis:    只有 version 链上的 append；对外仍用五种 decision 词汇
Falsifier: untested → holding ⇄ pressured → triggered → (触发 zero-base question)
```

---

## 五、30 / 60 / 90 天路线

**Day 0–30：审计与影子（不改变任何执行行为）**
- 建 7 张新表；`theses` 迁移到版本链；`add-decision` 改经 commit gate（暂时=人工批准所有 view 变化）。
- 每次模型唤醒落 `usage`；实现全局 pause 标志。
- 把到期触发接上：evidence `valid_until` 过期、decision `next_check` 到期、催化剂日历 → 自动生成 question（确定性脚本，零模型成本）。
- Agenda shadow：每日一个 cron 让 Dalton 基于 Ledger 出"今日议程卡"（选什么/不选什么/为什么），只发给人看，不执行。人每周花 15 分钟标 agree/disagree。
- **验证的关键假设：议程认可率。** 若 4 周后认可率 <60%，问题在 question 质量或排序规则，回到规则层修，不必写任何新基础设施。

**Day 31–60：单公司只读闭环（万华）**
- 允许 agenda 每日自主选择并执行 ≤N 个只读、无副作用、预算内的 work order（限万华 + 聚氨酯链）。
- Verifier v1 上线：确定性脚本层（来源可达、数字对账、算术复算）+ 模型对抗层；revise ≤2 轮；开始注入 seeded errors 测检出率。
- 所有 thesis/model/view 变化仍人工 commit。
- **验证的关键假设：verifier 真实检出率与自主执行的单位结论成本。**

**Day 61–90：受限自动 commit 与校准**
- `NO_CHANGE` 和例行数据更新类 decision 按政策自动 commit；`THESIS_*` 仍人工。
- 出第一份校准报告：预测 impact(H/M/L) vs 实际有无 belief delta 的混淆矩阵、四池利用率、成本/结论、verifier 检出率、议程认可率趋势。
- 扩到三家公司；driver 镜像表接入万华模型。
- 期末决策点：数据说话——认可率和检出率达标则扩大自动 commit 范围并启动 skill proposal 流程（人批版）；不达标则继续 shadow。

**明确推迟（无日期）**：DeepSeek Harness 接入、Temporal/Postgres 迁移、知识图谱、skill 自动晋级、多行业 agent、任何"信息增益"数值化。

---

## 六、最危险失败模式、可观测指标与 kill switch

| 失败模式 | 指标 | Kill switch / 缓解 |
|---|---|---|
| 成本失控（自我唤醒循环） | usage 表日 spend、唤醒次数/日 | 日预算硬上限，超限 `paused=true`，coverage_run 拒绝唤醒 |
| 任务自我繁殖 | reflection 来源 question 占比、backlog 净增速 | 每轮新建 question 上限；reflection 问题带 TTL 自动休眠 |
| 信念静默漂移 | 无 evidence_refs 的 thesis version 数（应为 0）、无 verifier_ref 的 commit 数 | commit gate 拒绝写入；transitions 表周度审计 |
| Verifier 橡皮图章 | pass rate（长期 >95% 告警）、seeded error 检出率 | 检出率跌破阈值 → 冻结自动 commit，全量转人工 |
| 新闻上瘾 | 四池实际用量 vs 配额、深研池连续饥饿天数 | 池配额硬约束；event 池超额直接丢弃到 backlog |
| 闲置死锁（九天事故复发） | 连续无任何 cycle 执行天数 | watchdog：>48h 无活动即告警人类；到期触发底座保证议程永不枯竭 |
| Revise 死循环 | 单 task revise 轮数 | 硬上限 2 轮 → blocked → 人 |
| 幻觉证据污染 Ledger | evidence 来源 URL 不可达比例（脚本可测） | 确定性 verifier 层拦截；抽样人工核对 |
| Steering 污染历史 | thesis/decision 表的非 commit-gate 写入（应为 0） | 所有写入强制走 CLI，CLI 内嵌 gate 与 pause 检查 |

总开关就一个：Ledger 里的 `paused` 标志 + 停掉 OpenClaw cron。因为所有唤醒都走 `coverage_run.py`、所有写入都走 `coverage_os.py`，两个咽喉点各查一次标志即可，这是现有架构最值钱的性质，**任何演进都不得引入绕过这两个咽喉的路径**。

---

## 七、需要你拍板的高杠杆问题（5 个）

1. **自动 commit 红线**：`NO_CHANGE` 和例行更新自动提交没有争议；`THESIS_STRENGTHENED/WEAKENED` 是否永远人工？这决定 commit gate 的最终形态，建议现在承诺"`THESIS_*` 与估值变化至少 12 个月内全部人工"。
2. **预算数字**：日/月的美元和 token 硬上限是多少？四池配额比例（建议起点：event 30% / 首次覆盖 30% / 深研 25% / 维护 15%）？没有这两个数，自主执行无法开闸。
3. **Verifier 独立性成本**：重大 commit 是否愿意付第二个模型家族的复核成本（约增加 10–20% 模型开销）？还是接受"脚本层 + 同族对抗 prompt + 人类抽样"？
4. **模型权威**：driver 数字的权威留在 Excel（人可编辑、系统镜像）还是迁到结构化表（系统权威、导出 Excel）？这决定"模型联动"能做到多深，建议先镜像、一年后再议迁移。
5. **Shadow 期成功标准预先承诺**：议程认可率达到多少（建议 ≥75% 连续 4 周）、seeded error 检出率达到多少（建议 ≥90%）才授予下一级自主权？**现在写下数字**，避免届时凭感觉放权——这本身就是对系统"grader gaming"的第一道防线，也是对我们自己的。

