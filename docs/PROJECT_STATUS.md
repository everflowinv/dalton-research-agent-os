# Dalton 项目进度

更新日期：2026-08-23
- live deployed source：`3fe746e`；thesis-impact production runner：`9c295ca`；OpenClaw host patch chain：
  `6f93b9b14`
- live 已启用独立的 thesis-impact 短任务，每 300 秒运行一次；writer 持有 Core/Scheduler，worker 只能通过
  scoped RPC 提交受限操作，不直接打开 live Core SQLite
- assessment policy 固定 `profile:gpt-5-6-sol`；verifier policy 固定
  `profile:gemini-3-7-flash`，并要求 provider-controlled `thinking=low`
- production day-budget policy 为 USD 25；当前 `company_thesis_refs={}`，live Core 也没有 ThesisVersion，
  因此短任务稳定返回 idle、provider call 为 0，也不会修改 Thesis current pointer
- development candidate 已将首个覆盖切换为 US IT Services / ACN：ordinal ThesisVersion v0.2、versioned Driver Pack、
  human-only admission candidate/decision 和唯一 ACN mapping fixture 已在 in-memory Core 跑通；尚未写 live Core，
  尚未激活 production mapping，也没有产生付费模型调用
- development candidate 已增加 AlphaEngine live `search_library → get_document` bridge：每次调用绑定 exact
  CompiledConnectorPlan、credential use receipt、quota、raw Artifact 和 SourceEnvelope；真实只读 canary 已取回
  10 条 ACN 检索结果及首个 30k 字符文档分片，续页游标为 `30000`。bounded multi-page coordinator 已在本地实现：
  每页回查 immutable authority/raw JSON-RPC，只有连续终页的整文长度和 SHA-256 一致才 complete；真实完整文档
  canary 和 production ResearchPlan/Scheduler 接线尚未执行
- development candidate 已把 connector quota window 扩展到 IANA 当地日历日，并按 owner 指令冻结候选日配额：
  AlphaEngine `search_library` 50 次、`get_document` 80 份完整文档，Gemini `search_web` 1,000 次；三者均在
  `Asia/Shanghai` 00:00（UTC 16:00）重置。AlphaEngine 文档只在首个 page 消耗 1 个 document unit，续页只记录
  physical calls/bytes；内部 20 页/文档的 calls 上限只是安全阀，不是供应商计费口径。Gemini 1,000 次仍是
  development governance policy，尚未部署。分页 coordinator 的测试已确认首页记 1、续页记 0，
  并覆盖页数/总响应字节/文档字符上限和 crash replay
- development candidate 已增加 Gemini `web_search` discovery bridge 和独立 public-web fetch adapter。冻结 inventory
  已按真实 OpenClaw 合同修正为无 cursor，`freshness` 与显式日期窗互斥；search raw response 完整保存，向后只暴露
  由引用 URL 推导的 opaque authority ref，不把 Gemini synthesis、snippet 或 title 当作网页正文。系统只有从 exact
  search raw artifact 重建 URL authority，并经现有 public-only HTTPS transport 独立取回原始 bytes 后，才允许
  `fetch_get` 形成 public-web authority material；search 和 HEAD 在 source-material gate 与 candidate-evidence gate
  都会 fail closed。本地全量回归 694/694 通过；最终 citation 上限硬化后的关联超集 53/53 通过，compileall、
  inventory 确定性重建和 Python 3.13 sdist/wheel 构建也通过。具体 OpenClaw host runner、production
  Catalog/profile/grant 和真实 canary 尚未接线或执行
- development candidate 已完成 Model Input Ledger v1：actual、assumption、forecast line、scenario、model run 和
  reconciliation 都使用不可变版本与 exact authority；研究 worker 只能提交 candidate，正式 input 由认证人类准入；
  valuation 缺 price/shares/FX/rates/consensus 任一正式 authority 时 fail closed。随后新增通用
  `IndustryResearchAuthority`：Industry Evidence Pack 只能绑定正式 Evidence/Claim/Relation，Company Overlay 只能复用
  pack driver、公司 Claim 和同源的 human-admitted actual Model Input。隔离 US IT Services / ACN canary 已形成 4 个
  driver、4 家 coverage universe、6 条 Claim、5 条 Model Input、1 个 industry pack 和 1 个 ACN overlay，ThesisVersion
  与 paid model call 均为 0；尚未写 live Core。当前 pack 只有 ACN evidence，CTSH/EPAM/IBM 仍待 peer 验证
- 修正版 3×30 canary 的三个 run identity 各自独立，90/90 fresh execution，三轮均 30/30、0 FP、0 high miss，
  总成本 USD 0.12906825；随后真实 isolated shadow 固定 GPT-5.6 Sol → Gemini 3.7 Flash low，quality gate 为
  `eligible`，成本 USD 0.010518
- activation 前已创建并验证 `pre-thesis-impact-production-20260822-v1` 回滚快照；OpenClaw host 本轮没有配置
  或 patch 变化，沿用已通过 canary 的现有 gateway 进程，没有为重启而重启

本文是当前进度的权威入口。`docs/reports/` 下的实施报告记录各次交付当时的状态，后续实现不会
反向改写历史结论。这里的“完成”只表示代码、测试和当前部署已经验收，不表示已达到多租户或
hostile-code 生产安全等级。

当前 connector / 建模 / 行业研究的近期顺序见
[direction-and-source-modeling-order-v0.8-2026-08-23.md](reports/direction-and-source-modeling-order-v0.8-2026-08-23.md)。
v0.7 及更早报告保留为各切片启动时的历史基线，不再作为当前近期执行顺序。

## 当前判断

Dalton 已经完成独立 Core、Research Ledger 核心版本链与 gate、单写者、Scheduler、模型路由、模型用量/成本、
Capability Registry/Catalog、常驻控制服务、Agenda Shadow、durable outbox、人工反馈和备份恢复。
live 部署现在能自主生成并选择研究问题，也已加载 phase-pinned thesis-impact production lane；由于 live Core
尚无 ThesisVersion 和 company mapping，这条 lane 当前只做无模型调用的 idle 检查，不提交 assessment、verification
或 ThesisVersion。仓库 fixture 仍可按一次性 connector plan 执行 CNINFO、SEC、AlphaEngine 三源离线流程并从
checkpoint 恢复；当前开发候选已能重放 fixture，也能从完整 Connector authority 解析真实 SEC public
响应，并把 source/numeric verifier 通过的 CandidateEvidence/CandidateClaim 写入独立 staging。这条链已由
ResearchPlanExecutor 接通，并在隔离临时 authority 中跑完一份真实 SEC public 四步 plan。Owner 已接受该 plan
的 exact candidate；隔离 Core 已生成 1 条正式 EvidenceVersion 0.2、1 条 ClaimVersion 0.2、1 条 supports
relation 和 1 条 Backlog answer binding。当前开发候选已经增加明确人工审阅入口、无损正式 promotion 和
authority-bound ResearchPlan closure，但尚未部署。

2026-08-20 新隔离 canary 已从 `data.sec.gov` 真实跑通 policy-authorized 主链：versioned policy 自动授权 1 份
SEC public 10-Q plan，四个节点全部成功，系统自动提交 1 条 Evidence、1 条 Claim、1 条 supports relation，并生成
1 条 Backlog answer binding。`research_plan_approvals=0`、`human_review_decisions=0`；Core、candidate staging、
coordinator、capability 四个 SQLite integrity check 均为 `ok`。这证明低风险主链不需要逐 plan、逐 Claim 找 owner
审批，但仍只是 `filing_count` 机制样本，不代表已经达到自主研究分析师的最终质量。

同日第二条无凭据 canary 已把 SEC Company Concept 接入完整自动主链。系统从 Microsoft 同一份 10-Q accession
`0001193125-26-191507` 中选出 2026-01-01..2026-03-31 与同比期间收入：USD 82,886,000,000 对
USD 70,066,000,000，独立 numeric verifier 复算同比增长 18.3%。`get_company_facts → authority resolver →
source/numeric verifier → candidate staging → policy commit → formal Evidence/Claim → Backlog answer` 全部完成；正式
Claim 为 `claim-version:56531bfe0721396625896d5bef8b2584e3c0fd5388aadf7e996bc6cde6c7e179`，
`research_plan_approvals=0`、`human_review_decisions=0`，四个 SQLite integrity check 均为 `ok`。选择规则拒绝跨
filing 拼接、年度/累计期间、单位漂移、模糊 comparative context、非整数 USD 事实和 taxonomy/concept 猜测。
这证明系统不需要 owner 逐条审阅，也已把正式产物从 filing metadata 推进到研究可用的财务事实；它仍未更新
thesis/model，不能把单一收入增速自动解释成投资结论。

同日多样本校准暴露并修复了一个真实错误：Walmart 已弃用的 `SalesRevenueNet` 只有 2018 年数据，旧实现仍把它
作为“最新”事实自动提交。`get_company_facts` 现在必须冻结最长 400 天的 `filed_from..filed_to`，adapter、raw
authority replay 和 policy commit 都绑定同一窗口；旧 concept 在窗口内没有 10-Q 时会在 connector 阶段失败，
不会生成 Evidence 或 Claim。窗口化实跑中，Apple 的同季度收入同比为 16.36%，NVIDIA 的 `Revenues` 为 85.23%，
Walmart ASC 606 revenue concept 为 7.14%；三个 plan 都是零人工审批并完成正式 closure，Walmart 旧 concept 则按
预期产生 0 条 Evidence、0 条 Claim。专项测试同时覆盖 `10-Q/A` 排除、缺失同比、模糊 context 和超宽窗口。

随后开发候选已去掉 company-specific concept 输入。plan 只冻结统一的有序 allowlist：`Revenues →
RevenueFromContractWithCustomerExcludingAssessedTax → SalesRevenueNet`；adapter 从 SEC Company Facts 原始响应找出
窗口内最新 10-Q accession，只在该 accession 上按顺序选第一个能形成 same-filing 同比的 concept，并把全部可用
concept 一并写入 authority。当前代码实跑 Apple 时自动 fallback 到 ASC 606 concept，NVIDIA 自动选 `Revenues`
并得到 85.23%，Walmart 自动选更完整的 `Revenues` 并得到 7.33%；此前 7.14% 的口径不含部分会员费等收入。
三家公司均完成零人工审批的正式 closure。

开发候选现已完成 **正式财务 Claim → 既有 driver/thesis 影响判断** 的第一版 authority：producer 只能读取
exact formal ClaimVersion、当前 ThesisVersion 和两者 hash，输出 `supports / weakens / no_change / insufficient`；
另一个不同 model family 必须从 Scheduler 的 immutable ResultEnvelope 独立复核。通过后只形成 append-only
pre-commit 判断，不会直接改 thesis，也不增加逐条 owner 审批。没有既有 thesis/driver 时，系统保留 Claim 并
在 ResearchQuestionBacklog 自动生成一条可重放的 follow-up question。

2026-08-21 开发候选已把 ResearchPlan closure 的 exact formal Claim 接到上述路由。新 coordinator 重读
ResearchPlan、Backlog start/answer、正式 Claim 和当前 Thesis 的 exact ref/hash，再依次生成受预算约束的
assessment WorkOrder 与独立 verifier WorkOrder；verifier 同时读取 assessment、Claim 和 Thesis，不能只复核摘要。
隔离 recorded-result 端到端 canary 已覆盖 `supports`、没有 thesis 自动建题、`insufficient` 自动建题和 crash/replay
去重；两条模型 WorkOrder 都无 side effect，整个流程不增加逐条人工审批，也不改 thesis。截至这个 recorded 阶段，
真实付费模型尚未调用；后续 Gate 2 结果见下文。

同日后续开发已把这两个 WorkOrder 接入现有 `ModelRouter → OpenClawModelAdapter → Core/Scheduler` 执行边界。
worker 只能执行 coordinator 生成且已在 Scheduler authority 中冻结的 exact WorkOrder；assessment 失败后按 bounded
attempt 重新路由，verifier 的 producer family 从已持久化 assessment invocation 自动读取，caller 不能选择或绕过
family independence。每次 broker 调用都会先写 ModelInvocation、usage 和 cost authority，模型输出通过 closed contract
及 exact ref/hash 检查后才允许成为 Scheduler formal success。错误 JSON 会进入有界 retry，最后一次失败会形成正式
failed result，不留下 exhausted-but-unreadable 状态。

隔离 recorded broker canary 已验证 `错误 assessment JSON → retry → valid assessment → independent verifier pass → replay`：
三次调用分别形成三条 invocation、usage 和 cost，错误输出没有生成 assessment，verifier 自动切换到另一个 model
family，重放没有第四次 broker 请求，人工 review decision 仍为 0，Thesis current pointer 不变。该 canary 没有调用
真实模型，不能用于判断模型输出质量。

同日继续补齐了付费调用前的崩溃边界。如果模型已经返回并写入 invocation/usage/cost、但进程在
`Scheduler.complete` 前崩溃，lease 过期后的新 attempt 会复用原 route 和 invocation，只能向 broker 发
authenticated `replayOnly` 请求。broker durable journal 命中时返回原 completion；miss 时直接返回
`IDEMPOTENCY_MISS`，禁止调用 host 模型。只有没有被 Scheduler 接受过的 route 才进入这条恢复路径；已经形成
retryable ResultEnvelope 的 attempt 仍会按正常 retry 新建 route。隔离 E2E 注入了 accounting 后崩溃，最终 2 次
socket 请求只产生 1 次 provider call、1 条 invocation、1 条 usage 和 1 条 cost，formal success 落在 Scheduler
attempt 2，三套 SQLite integrity check 均为 `ok`。

2026-08-21 方向复审维持 Conditional Go，但改变了下一阶段顺序。Fable 的 review evidence 注入脚本失败，且
`3fd630e..b81d1cb` 的 10 个提交在独立复核完成前已累计增加 8,527 行。仓库虽已有 Python 3.11/3.13 全量测试、
build 和 broker check 的 GitHub Actions，Apple/NVIDIA/Walmart 也跑过同指标自动 closure，但最新 HEAD 的 CI 在
复审时尚未全部结束，三家公司结果也只有状态文档记录，没有可提交的 replay bundle。当前顺序改为：先完成最新
HEAD 独立 CI、hermetic replay canary 和 fail-closed review evidence collector；再在同一 commit 上复现 5 家公司并
产出 verified revenue-growth brief；之后才取得单独付费调用授权，跑一条真实 thesis-impact canary。此前不增加
thesis updater、并发 worker、自动 thesis revision 或 fleet control。真实 canary 仍不得直接修改 thesis；
`insufficient` 只生成后续问题，`supports / weakens / no_change` 只形成可供未来 thesis updater 消费的已验证判断。

Gate 0 候选现已增加显式 hermetic replay CI step 和 fail-closed review evidence collector。前者单独覆盖
recorded SEC Company Facts → formal Claim closure → recorded thesis assessment/verifier → replay；后者从 closed
manifest 收集非空文档、实现和命令证据，只接受 argv 数组，任一文件缺失/为空、命令失败/超时、路径逃逸或陈旧
输出都会停止且不发布半份 artifact。GitHub Actions 在 Python 3.11、3.13 两个 runner 都跑 canary，3.13 runner
另上传证据包。该候选本机 Python 全量 581/581、broker 16/16、canary 1/1、collector 8/8、build 与 compileall
通过。Exact commit `3d2114a05b97b2a6a5005242106ebb961df161f9` 的
[Actions run 32470808101](https://github.com/everflowinv/dalton-research-agent-os/actions/runs/32470808101)
中 Python 3.11、Python 3.13、openclaw-broker 三个 job 全部成功；两个 hermetic canary step、3.13 review collector
和 artifact upload 也全部成功。Gate 0 已完成。

Gate 1 已在 clean commit `0b0f872c0f935098f8e41af339a93d8164684992` 运行固定五家公司 batch。
Microsoft、Apple、NVIDIA、Walmart、Amazon 五条同入口 SEC Company Facts plan 全部完成 source/numeric verifier、
正式 Evidence/Claim、Backlog closure 和进程重启后的无网络 replay；重放统一返回 `duplicate`，没有新增网络请求，
每家公司保持 1 条 Evidence、1 条 Claim、0 条 Thesis、0 个人工 gate，全部 SQLite integrity check 为 `ok`。
五家公司季度收入同比依次为 18.30%、16.36%、85.23%、7.33%、19.62%；实际模型调用和成本均为 0，thesis
impact 明确记录为 `not yet run`。Walmart stale `SalesRevenueNet` 控制样本按预期失败，candidate 和正式
Evidence/Claim/Thesis 全部为 0。独立 edgartools 路径复核了 5 个 accession、10 个财务报表数值和 5 个同比计算，
没有差异；同时证明通用 Revenue 关键词路由会对 NVDA/WMT 误选 CostOfRevenue，不能替代 exact duration 和冻结
concept allowlist。结果 bundle hash 为 `7f69dc9a483d3e04cc6c8c6eeb01563ad0e5e94e28d189df34350f812a95844b`，
简报见
[gate1-sec-five-issuer-revenue-growth-2026-08-21.md](reports/gate1-sec-five-issuer-revenue-growth-2026-08-21.md)。
Gate 1 没有 schema 变化，也没有部署 live。Gate 1 完成时的下一门是取得 owner 对具体付费调用和 hard spend cap
的单独授权后运行一条真实 ThesisVersion 的 thesis-impact canary。本机相邻回归 81/81、Python 全量
587/587、broker 16/16、显式 hermetic replay 1/1、build、compileall、结果摘要 JSON 和 `git diff --check`
全部通过。

Gate 2 已在 owner 授权的 USD 1.00 hard cap 下完成真实隔离 canary。MSFT Gate 1 Claim 先由 GPT-5.6 Sol 生成
`insufficient` assessment；worker 在 model accounting 后、Scheduler completion 前退出，lease 过期后通过同一
invocation 的 `replayOnly/duplicate` 恢复，没有第二次 provider call，也没有重复 invocation/usage/cost。独立
verifier 使用 DeepSeek V4 Flash，producer/verifier family 分别是 `openai-gpt-5.6` 与 `deepseek-v4`。本次两条
调用为 3,338 tokens、USD 0.013211；全部已知成本为 USD 0.169986，另为一条无费用遥测的 Claude TIMEOUT 预留
USD 0.25，累计上界 USD 0.419986。离线 replay 使用 deny adapter 禁止 broker 访问，稳定重现正式 `rejected`，
两条模型记账数量不变，Thesis pointer 不变，Core/review/coordinator/router integrity 都是 `ok`。

Gate 2 的控制面通过，模型质量门没有通过。DeepSeek 的五条 rejection findings 中有 ref/hash 自相矛盾，并建议
使用 authority closed taxonomy 之外的 `none / contradictory`；系统仍保留正式 `reject`，assessment 未进入
eligible，也未回写 Gate 1 简报。真实运行还暴露并修复了两个控制缺口：verifier token 预算过小，以及旧 adapter
在 provider telemetry 超预算时先抛错、导致已付费拒绝响应只留在 broker journal。现在超预算内容仍会被拒绝，
但 invocation/usage/cost 会进入 Core。报告见
[gate2-real-thesis-impact-canary-2026-08-21.md](reports/gate2-real-thesis-impact-canary-2026-08-21.md)。

Verifier 校准基础随后冻结 12 个样本：5 pass、7 reject，其中 5 个 high、2 个 medium。gold label 与模型输入分离；
基础冻结时，Gate 2 的 DeepSeek 输出是唯一真实观测，在应 pass 的样本上形成 1 个 false positive。新 WorkOrder 必须返回
严格 `0.2` finding contract，authority 会按 WorkOrder 重验版本，worker 会拒绝与已验证 binding/driver 自相
矛盾的 finding。历史 `0.1` 只保留 replay 兼容。12 个样本小于 30 个放权门槛，因此不能解锁自动化。报告见
[thesis-impact-verifier-calibration-foundation-2026-08-21.md](reports/thesis-impact-verifier-calibration-foundation-2026-08-21.md)。

真实候选校准现已完成。DeepSeek V4 Flash 跑完 12/12：accuracy 75%，7 个错误样本检出 5 个，detection rate
71.4%；5 个正确样本误杀 1 个，false-positive rate 20%；high-severity miss 为 1。另有 3 条输出会被 production
consistency guard 拒绝。Claude Fable 5 第一条实际使用 6,795 input / 958 output tokens，超过 WorkOrder 的
3,000 / 400 上限，费用 USD 0.11585 也超过单条 USD 0.04 admission cap；runner 持久化失败后停止剩余 11 条。
本轮两模型新增实际费用 USD 0.118666；加上 Gate 2 既有 accounted + uncertain reserve 后总上界 USD 0.538652。
没有候选获准上线。报告见
[thesis-impact-verifier-live-calibration-2026-08-21.md](reports/thesis-impact-verifier-live-calibration-2026-08-21.md)。

2026-08-22 校准语料已扩到固定 30-case v0.2。第一轮 shortlist 中 Gemini 3.1 Pro Preview 为 30/30，Claude
Opus 5、Qwen 3.8 Max、GPT-5.6 Terra 均为 29/30；只有前两者没有 high-severity miss，但这轮仍是
`calibration-posthoc-v1`，不能冒充 production provider-control proof。随后 provider direct 测试证明 Gemini 3.7
Flash low 可稳定返回 strict JSON；旧模型回抄 `assessment_ref/hash` 的设计也被替换为 wrapper-owned binding。
模型现在只返回 closed `schema_version/verdict/findings`，trusted worker 从 immutable WorkOrder 注入 exact
assessment identity；raw ResultEnvelope 不改写，authority replay 可重建正式 `0.2` output，历史 WorkOrder 继续兼容。

同一 30-case corpus、strict prompt、temperature 0、thinking low、16k cap 的正式 semantic-only 重跑中，Gemini
3.7 Flash 和 GPT-5.6 Luna 都是 30/30、0 false positive、0 high miss；Qwen DeepSeek V4 Flash 为 27/30，
有 2 个 high miss。Gemini 平均 2.452 秒、P95 3.911 秒，Luna 平均 7.829 秒、P95 12.687 秒；Owner 已选择
exact `google/gemini-3.7-flash`、thinking low 作为主候选，Luna 保留为低成本候选。90 条 raw output 都不含
target binding，wrapper 后 90 条均绑定成功。Python 全量 616/616、broker 21/21、wheel/sdist build 通过。
报告见
[thesis-impact-verifier-wrapper-selection-2026-08-22.md](reports/thesis-impact-verifier-wrapper-selection-2026-08-22.md)。

2026-08-22 后续实现补齐 production verifier 的两项 conformance：新增不可变
`model-routing-policy-version:dalton-openclaw-verifier:1` 只允许 exact `profile:gemini-3-7-flash`，worker 的
verification 相位可改在该 policy 下路由，且未 pin 到单一 profile 的 policy 会在 claim 前 fail closed；
`thinking=low` 现在冻结进 verifier WorkOrder 与 calibration manifest，进入 broker `requiredControls`、
request hash、invocation 幂等身份和 host proof 的 closed 合同（broker 升至 0.1.0-spike.5）。host 侧
provider controls、rate card、thinkingLevel 与 patch 后来已
打通；首次真实 3×30 的 90 次 fresh 调用与质量数据有效，但因三轮 run identity 重复而撤销 production gate。
修正后的 runner 已用三个不同 run identity 重新完成 3×30，90 条均为 fresh execution，三轮均 30/30，
`production_gate.eligible=true`；随后单条 isolated shadow 也达到 `eligible`，并保持 ThesisVersion pointer 不变。
Owner 后续授权 production activation，当前 live 服务已经安装独立短任务、phase policies、USD 25 day cap、
scoped writer principal 和回滚快照。由于没有 live thesis target，activation 后 provider call 和 Thesis mutation
均为 0。部署记录见
[thesis-impact-production-activation-2026-08-22.md](reports/thesis-impact-production-activation-2026-08-22.md)。
phase-pin/thinking 批次的本机验证：Python 全量 621/621、broker
22/22、显式 hermetic research replay canary、compileall、wheel/sdist build 与 `git diff --check` 全部通过；
没有付费调用。报告见
[verifier-phase-pin-and-thinking-controls-2026-08-22.md](reports/verifier-phase-pin-and-thinking-controls-2026-08-22.md)。
同日后续新增 `dalton-thesis-impact-verifier-canary` campaign runner：把 3×30 开闸条件收敛为一条显式授权即可
执行的命令，内置 per-case/per-round/campaign 三重硬顶、逐轮验收（0 FP、0 high miss、0 schema/control
failure、thinking 与 provider-control 合同逐 record 绑定）与 `production_gate` 裁决；campaign 自身不发起
未授权付费调用，测试无网络无付费；本机验证 Python 全量 628/628、hermetic replay canary、compileall、
wheel/sdist build 与 `git diff --check` 全部通过。报告见
[verifier-canary-campaign-runner-2026-08-22.md](reports/verifier-canary-campaign-runner-2026-08-22.md)。
同日再后续补齐 v0.7 Gate 3 控制面：新增 `thesis_impact_budget` authority（独立 owner-only SQLite，第 17 份
packaged SQL schema）提供版本化 per-day 硬顶、per-(work order, attempt, phase) admission/settlement（未结算
按全额保守占额）、durable rejection（被拒身份不可复活）与 append-only 告警链（claim TTL、投递上限 5）；
worker 可选接入——超顶在任何 broker 调用前落成正式 `DAY_BUDGET_EXCEEDED` 失败与 high 告警，终态失败记录
`work_order_failed` 告警，未配置时行为不变。故障注入已证明超预算停止并留下 decision、重放不产生新事实；
本机验证 Python 全量 636/636、broker 22/22、hermetic replay canary、compileall、wheel/sdist build 与
`git diff --check` 全部通过，没有付费调用。报告见
[thesis-impact-day-budget-and-alerts-2026-08-22.md](reports/thesis-impact-day-budget-and-alerts-2026-08-22.md)。

现有 versioned governance policy 可分别只允许 closed SEC public `10-Q list_filings` 或 exact
`10-Q get_company_facts` plan 自动启动；
其中 `list_filings` 结果只有在 Core 从 exact
CallSpec、SourceEnvelope 和 Artifact 重新推导 CIK、表单、日期窗、记录数与完整 statement，并命中固定
`filing_count` rule 时才能自动写 EvidenceVersion 0.2、ClaimVersion 0.2 和
supports relation；`get_company_facts` 结果则必须从 exact Company Facts raw body 重放最新 10-Q accession、冻结
allowlist 选择、current/prior quarterly fact，并由 numeric verifier 独立复算 `growth_percentage` 后才能自动提交。
ClaimIndex status 派生现已改为读取
Core 的一致 Ledger snapshot，绑定 snapshot ref/hash，并拒绝
caller-provided status；DocumentIndex FTS5 已完成开发候选；ContextPack authority-bound materializer 已完成
claim/artifact 只读切片，并已接通 Agenda 的 mandate/perception exact reader。PerceptionSnapshot 现在进入 Core
append-only authority，Agenda 使用独立 AgendaContextBinding，模型只读取固定 instruction/output contract 与
materializer quoted JSONL；可变 snapshot 文件不再参与 replay 或 prompt。ResearchQuestionBacklog 开发候选已
完成：稳定 question 身份、冻结状态机、AgendaDecision 链接、正式 ClaimVersion answer 绑定与 Mandate 进度
投影，问题现在可以跨 cycle 存续。Planner 薄闭环也已完成开发候选：exact selected AgendaDecision/
ResearchQuestionVersion → immutable ResearchPlanVersion → WorkflowRunVersion/WorkOrderLink 任务树；首版只允许
无凭据 SEC public `list_filings`；人可以批准 plan，active versioned policy 也可以只对 closed low-risk scope 签发
独立 authorization，automation 不能伪装成人。启动只把根 connector WorkOrder 放入
Scheduler，下游 resolver/verifier/candidate staging 保持 planned，必须由 coordinator 在上游 exact result 后逐项
admission；开发候选 coordinator 已完成 exact Scheduler/connector receipt/runner journal/内部阶段输出证明核对，
每次只 admission 直接子节点，重放收敛且篡改 fail closed。ResearchPlanExecutor 现已把四个真实节点接到各自
authority，并跑完第一条真实 SEC public WorkOrder 树。首条 exact candidate 已获 explicit accept，正式
Evidence/Claim/Relation 与 Backlog answer 也已写入隔离 authority；新 closure coordinator 会重验
plan→final candidate→authorization→formal promotion 全链，并在崩溃后收敛到同一 answer binding。当前自动规则
不接受 model-written interpretation、candidate revision、凭据来源、非完整枚举、其他 source/metric/form 或越界
预算；这些情况进入 human review / revise / reject 异常通道。HumanReviewAuthority 的 revise consumer 只允许人工
改写 `normalized_statement`，source、numeric、period、evidence 和 provenance 不变，其他变化回到新的 verified
plan run。Interrupt / park / resume 与 Reflection 继续顺延，不增加没有真实消费者的内核子系统。当前没有 live
policy activation、凭据或旧 cron cutover。
万华的 10 个工作日/20 个显式人工标签门槛
只限制 Agenda 从 1 家扩到 3 家。第一条真实闭环和至少 1 条人工接受的正式 Claim 门槛已在隔离 canary 达到，
但它只证明机制闭环，不证明研究质量或生产就绪。后续增量必须回指真实审阅数据或明确解除覆盖阻塞；与质量缺口
无关的新 connector 品类、Model IR、sandbox 和其他内核扩建继续后置。任何研究执行开闸、生产部署或旧 cron
cutover 仍须单独验收。

### P2 DocumentIndex FTS5 当前进度（开发候选，未部署）

- 新增 `DocumentIndexInput`、`DocumentIndexSnapshot` closed contract，以及 owner-only SQLite FTS5
  projection。投影只读 exact `ObservabilityStore`、`ConnectorStore` 和 `RawSpool`；rebuild/clear 只改
  自己的 disposable 数据，不提供 Artifact、Ledger 或 connector authority mutation API；非内存文件强制
  `0600`；
- 内置 `utf8`/canonical `json` extractor 直接从已复核 ArtifactVersion hash+size 的 raw bytes 派生正文，
  caller 不能提交正文或自报 metadata。`content_type` 对应 ArtifactVersion `kind`，`media_type` 单独过滤；
  默认只返回 `public`，但 projection 不是同 UID 下的多租户安全边界；配置为可见的 internal/restricted
  内容仍可能物理存在于 disposable FTS 文件；
- source join 沿 SourceEnvelope → SQL execution link → Profile/CallSpec → connector ExecutionInvocation
  复核 exact ref/hash。`source_type` 从 Profile source identity 派生；只有 SEC
  `source:sec-edgar/list_filings` 的 `issuer` 能生成 `company:sec-cik:<10位CIK>` facet，unknown source 保持
  空 facet；rebuild 和查询都会检查 FTS、主表、facet 和 record hash 的一致性；
- FTS 使用 `trigram`。三字符中文（如“半导体”）可有限命中，两字符（如“存储”）可能 miss；这不是通用
  中文分词。SEC submissions JSON 只按 connector response/filing metadata 处理，不能称为 filing 正文全文；
  embedding 尚未实现；ContextPack materializer 支持 exact ClaimVersion 0.1/0.2、ArtifactVersion 0.1/0.2、
  MandateVersion 与 PerceptionSnapshot，SourceEnvelope 正文类型仍 fail closed；它从 Ledger/Observability/RawSpool/Core
  重读 authority，不能把 caller 正文、DocumentIndex FTS 正文、transcript 或 compaction summary 当事实；
  输出是短生命周期 quoted JSON-lines render 加不含正文/path/locator/credential 的 hash manifest，header/分隔符
  开销计入预算，不能超预算静默裁剪；现已接 AgendaCoordinator，仍未部署、未改 cron；
- `tests/test_document_index.py` 覆盖 raw hash/size、authority hash rebinding、source/profile/call link、
  access/filter forge、FTS `delete-all` checksum、FTS/main-table sync、query boundary、Unicode、删除重建和
  文件权限。该 slice 未部署、未接 Agenda/cron；Agenda materializer 不读取 DocumentIndex FTS body；
- broker 回归 15/15；固定 `SOURCE_DATE_EPOCH=1700000000` 独立构建的两份 wheel 逐位一致，SHA-256
  均为 `ccd4ad817cf1837ed2e99d48b1cdd1b23e543dcadafede8a72921ff70a3cd5c8`，大小均为 601,297 bytes；
  干净 Python 3.13 venv 安装、导入、打包后的 FTS schema 和两份新 contract 检查均通过。

### ContextPack materializer 当前进度（开发候选，未部署）

- 新增 `ContextMaterializer` 与 `ContextMaterialization` closed contract。materializer 要求 exact
  `DaltonStore` 与 `ObservabilityStore`；artifact 路径另要求 exact `RawSpool`。当前支持 `claim`、`artifact`、
  `mandate` 和 `perception`，source 正文仍 fail closed；可见 `access_class` 默认只有 `public`，扩大范围必须在
  materializer 实例显式配置；
- ClaimVersion 0.1/0.2 从 Core `claim_versions` exact row/record 读取，复核 id、version、prior、created_at、
  SQL column、canonical record hash 和对应 validator；render 同时携带 pack 冻结的 ClaimIndex entry，保留
  `proposed/corroborated/contested/superseded/retracted` 状态，但不把该投影冒充 ClaimVersion authority；
  ArtifactVersion 0.1/0.2 从 Observability API、跨代
  index、record row 及 RawSpool 复核 hash/size，正文只用内建 `utf8`/`application/json` extractor，storage
  locator 只用于 authority 校验，不进入 manifest；materializer 不读 DocumentIndex FTS body；
- materializer 可从 exact authority refs 构建 ContextPack 0.1 的 authority-bound input accounting。旧的
  caller-content pack 即使 ref/hash 合法，只要原文 token/byte 与 authority 正文不一致也拒绝；不重选、不截断。
  render 使用固定 quoted JSON-lines 边界，prompt-like 正文只在 `quoted_data` 中出现；ContextPack 的正文选择
  预算与 materialization 的 envelope-inclusive 总预算分开记录，header/分隔符必须计入后者；manifest 记录每项 authority/body/render 账、omission/failure 账、
  renderer/tokenizer ref/hash 与最终 render hash，不持久化正文、路径、locator 或 credential；
- `tests/test_context_materializer.py` 覆盖 23 个专项：claim/artifact、ClaimVersion 0.2 Decimal/structured period、
  caller text/hash rebinding、SQL/raw
  tamper、跨代 Artifact index、duplicate/omitted、正文/总预算、确定性、access class、unsupported kind/media、
  JSON/CJK、prompt-like quoted data、冻结 builder/selector/tokenizer/truncation、历史 pack replay、
  plan/ClaimIndex binding、敏感字段与 authority 行数不变。Agenda 接线由下节单独验收；整体仍未部署、未改 cron。
- 本地专项 23/23、materializer/coordinator/DocumentIndex/ClaimIndex 相关 57/57、Python 全量 423/423、
  broker 15/15、`compileall`、95 份 JSON schema、16 份 SQL schema 和 `git diff --check` 均通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel SHA-256 均为
  `e61d35359d52a169c8abd4df7628836715038064ff5167e917c1c3cd007ebd21`，611,413 bytes；Python 3.13
  干净安装、公开导入、新 contract 与共享 extractor/tokenizer 资源检查通过。

### Agenda context authority 当前进度（开发候选，未部署）

- 新增 Core `perception_snapshot_versions` append-only authority；authorized insert 与 no-update/no-delete trigger
  同时生效。AgendaCycle 启动时重读并核对 exact PerceptionSnapshot、MandateVersion 和 AgendaPolicyVersion；
  cycle row 冻结三者的 exact hash，active policy/mandate 读取也改走 canonical row/hash 复核；
- 新增 closed `AgendaContextBinding`，直接绑定 exact Cycle/Policy/Mandate/Perception ref/hash，不伪造
  CompiledConnectorPlan。ContextMaterializer 的受控 union 保持旧 connector plan replay，同时增加
  mandate/perception authority reader；writer 只允许 core principal 按 cycle ref 和预算读取，不接受正文、路径、
  callback、DB 或 caller timestamp；
- AgendaCoordinator 已删除手工 `MANDATE=`/`PERCEPTION=` 拼接。最终 prompt 只有固定 instruction/output contract
  与 materializer quoted JSONL；allowed source refs 和 company 只从 exact PerceptionSnapshot authority 派生；
  完整 prompt 使用冻结 tokenizer 核算 `max_input_tokens`，任一 required input 被预算丢弃即 fail closed；
- Agenda 专用 renderer 绑定 AgendaContextBinding；ContextPack 0.1 的必填 ClaimIndex 字段使用只允许 Agenda
  binding 消费的显式 no-index sentinel，不扫描 Ledger。无关 Claim/Ledger 增长不能改变 pack、manifest、prompt、
  WorkOrder 或模型调用幂等键；
- 专项 51/51、Python 全量 460/460、broker 15/15、`compileall`、96 份 JSON schema、16 份 SQL schema 与
  `git diff --check` 均通过。固定 `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel SHA-256 均为
  `b66589f8e28f6b10fd7f0c44bffe37ba6de97ce5b1c95add57dbe9da59dd0ba9`，大小均为 622,505 bytes；Python 3.13
  干净安装、AgendaContextBinding contract、Agenda schema/migration 与公开导入检查通过；
- 本切片未部署、未接 Backlog/Planner、未改变 auto-accept/timeout 权限、未改 cron。旧 live cycle 若没有已登记的
  PerceptionSnapshot 会 fail closed；部署前需单独裁决 backfill 或从新 cycle 开始，不能静默信任旧 snapshot 文件。

### ResearchQuestionBacklog 当前进度（开发候选，未部署）

- 新增 Core append-only ResearchQuestionBacklog authority：`question_ref` 由 canonical
  `{mandate_ref, company_ref, question}` 绑定确定性派生，caller 不能提供 id/hash；内容版本行不可变带链；
  冻结状态机 `open → selected → planned → in_progress → answered | blocked | retired`，每个迁移在同一
  Core 事务内校验并追加不可变 event，非法/乱序/重复迁移 fail closed，无恢复迁移；
- 同一问题跨 cycle 保持同一身份：相同绑定+相同内容幂等返回既有 head（duplicate），相同绑定+不同内容
  fail closed（conflict）；`backlog_idempotency` 沿用 agenda 幂等约定，同 key 不同 request 返回 conflict；
- `select_question` 在事务内重读 exact AgendaDecision/AgendaCycle/MandateVersion，要求 cycle mandate ==
  问题 mandate、cycle company == 问题 scope、decision 的 selected candidate 与问题内容逐字一致，
  并核对回答标准与 `source_refs`；读取 link 时再次复核 event、decision、cycle、candidate、policy 和
  backlog head，跨 mandate/跨公司/来源换绑/伪造 decision fail closed；
- `answer_question` 只接受正式 ClaimVersion：逐条从 Core `claim_versions` 重读、重算 hash、按 0.1/0.2
  重新校验闭合形状，candidate/staging/缺失/篡改 claim 拒绝，读取 answer binding 时再次核对 exact
  ClaimVersion，`candidate-claim:` 前缀显式拒绝；
  AgendaDecision 永远不会成为 answer；
- `mandate_progress` 是纯确定性、可重建的进度投影：绑定 active MandateVersion ref/hash，统计各 state
  计数与 answered claim refs；不写任何表、不改 MandateVersion authority、不成为替代 authority；
- Backlog authority 本身不创建 plan/WorkOrder/DAG；Planner 开发候选现已接管 `selected → planned` 与
  `planned → in_progress`，两次迁移都要求 exact plan/start binding 并与对应 authority 同事务写入；
  无 auto-accept 路径；
  专项 34/34，Python 全量 494/494，相关回归 82/82、broker 15/15、101 份 JSON contract 解析、
  16 份 Core SQL schema、`compileall`、`git diff --check`、SQLite integrity/FK 与 deterministic wheel
  检查全部通过。
- 仍未部署、未接 cron、未改变 auto-accept/timeout 权限；Planner 开发候选已接入 exact plan/start binding，
  Agenda Shadow 旧 `research_question_versions` 写路径保持不变，与 backlog 并存。Backlog 初始切片见
  [research-question-backlog-2026-08-15.md](reports/research-question-backlog-2026-08-15.md)。

### Planner SEC public 薄闭环当前进度（开发候选，未部署）

- 新增 append-only ResearchPlan authority。plan 身份由 exact ResearchQuestionVersion、selected
  AgendaDecision 与规范化 SEC request 确定性派生；在创建事务内重读完整 backlog/Agenda/context authority，
  候选位置、问题、回答标准、来源、company 或 mandate 任一换绑都会 fail closed；
- 首版执行范围固定为无凭据、公开只读的 SEC `list_filings`，只接受 10 位 CIK、`10-K`/`10-Q`/`8-K`
  和不超过 366 天的窗口。plan 冻结 profile、operation、verifier、runtime、capability、输出 contract、预算
  与 side effect，caller 不能扩充 host、credential、步骤或写权限；
- 每份 plan 确定性生成 connector → authority resolver → source/numeric verifier → candidate staging 四个
  WorkOrder 和三条 WorkOrderLink。启动时写 WorkflowRunVersion 与完整任务树，但只 admission 根 connector
  WorkOrder；三个子节点保持 `planned`，要由 coordinator 在 exact 上游结果后逐项 admission；
- plan 必须由 exact `human:<principal>` 写一次终态 accepted decision；model、automation、timeout、Agenda
  approval 和 auto-accept 都不能启动 plan。未批准和 rejected plan 均 fail closed；
- start 在 Scheduler、workflow/link 和 Core binding 接缝使用确定性身份；外部接缝或事务内故障后重放会收敛
  到同一个 start、同一棵任务树和一个根 WorkOrder。exact readers 会重新核对 plan/question/approval/start/
  workflow/link/Scheduler 双向绑定，后续 authority 篡改同样 fail closed；
- Planner 专项 13/13、Planner + Backlog 47/47、Python 全量 507/507、broker 15/15 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 两份 wheel 逐位一致，SHA-256
  `466935efa4684e7384b9b050002e642e648f848968442fb0a6a71850acb3ca38`，666,164 bytes；Python 3.13
  干净安装、公开导入与 packaged SQL 检查通过；
- 未部署、未访问真实 SEC、未创建 CapabilityLease/CredentialGrant、未自动写 Ledger。rejected plan 后问题仍
  停在 `planned`；replan/park/resume/retire 的产品语义继续保留为缺口，但在真实研究闭环和质量校准之后再设计。
  完整结果见
  [research-plan-thin-closure-2026-08-15.md](reports/research-plan-thin-closure-2026-08-15.md)。

### ResearchPlan coordinator 当前进度（开发候选，未部署）

- 新增唯一的下游逐项 admission 边界。caller 只提交 plan ref 和 upstream WorkOrder ref；coordinator 每次
  重读 exact plan/start/workflow/link、Scheduler WorkOrder/policy/attempt/lease/formal result/ResultEnvelope，
  不接受 caller-supplied success boolean 或 opaque payload；
- connector 根节点必须有 exact compiled request、completion receipt 与 Scheduler result 绑定。v0.2 receipt
  还要重读 Core runner journal 中的 actual request、完整事件 hash 链和终态 `responded` response；
- 三个内部节点必须返回封闭、可哈希的 `ResearchPlanStageOutput`，绑定 exact plan/step/output contract、直接
  上游 WorkOrder/result 和阶段规定的 typed ref/hash records。每次只 enqueue 直接子节点，不能越级或批量放行；
- child identity 和 idempotency key 由 immutable plan 派生。重复调用、enqueue 后崩溃重放都会收敛到同一节点；
  错误 plan/workflow/upstream/result、缺失 receipt、attempt/formal result/ResultEnvelope/receipt/child tamper，
  以及 failed/expired/plan-attempt-exhausted 都 fail closed；
- coordinator 专项 12/12、相邻 Planner/Scheduler/research coordinator/packaging 回归 55/55、Python 全量
  519/519、broker 15/15、`compileall`、106 份 JSON contract 与 `git diff --check` 均通过。固定
  `SOURCE_DATE_EPOCH=1700000000` 的两份 wheel 逐位一致，SHA-256 均为
  `cbbd4feb139e764f7217fd6644001a8e2df2d6c9c5f3aae8da2da3f961052364`，大小均为 677,254 bytes；
  Python 3.13 干净 venv 安装、公开导入和 packaged stage-output contract 检查通过；
- 本切片没有接真实四步 executor，也没有从 resolver/verifier/candidate staging 的 authority store 重读 typed
  records 正文；它证明 admission 控制语义，不证明四步计划已真实执行或投研产物有价值。完整结果见
  [research-plan-coordinator-admission-2026-08-15.md](reports/research-plan-coordinator-admission-2026-08-15.md)。

以上是 coordinator 切片交付时的历史结论。后续 executor 与真实 canary 进展如下。

### ResearchPlan executor + SEC public canary 当前进度（开发候选，未部署）

- `ResearchPlanExecutor` 已把 connector、authority resolver、source/numeric verifier、candidate staging 四个
  WorkOrder 接到各自 authority；coordinator 仍按 exact 上游结果逐边 admission，executor 不接受 caller
  伪造的 stage payload，也不会越级执行；
- 2026-08-20 在 owner-only 临时目录跑完一份人工批准的隔离 plan。真实请求只访问
  `data.sec.gov`，无凭据、不打开 live DB；四个节点均 `queued → succeeded`，最终停在
  `human-review-ready-candidate`；
- canary 枚举 Microsoft CIK `0000789019` 在 `2026-01-01..2026-08-17` 的 `10-Q`，得到 2 条官方 filing。
  candidate 的 `semantic_verification_status` 仍为 `unverified`，正式 Ledger 中 Evidence、Claim、Thesis 均为 0；
- 第一次错误窗口暴露出 transport 把 `error.retryable=false` 仍写成 Scheduler `retryable`。开发候选已改为保留
  adapter 的 retryability：不可重试 normalization failure 直接终止，429/timeout/adapter crash 仍按原策略
  重试；新增专项回归并通过；
- canary 的 4 个 SQLite authority 均通过 `PRAGMA integrity_check`。完整记录见
  [research-plan-executor-sec-canary-2026-08-20.md](reports/research-plan-executor-sec-canary-2026-08-20.md)。

### Agenda live 恢复（2026-08-20）

- 保留原 append-only 记录，用 correction 消除 1 条当前未定价成本，并为 2 条已计量但缺 cost 的 UsageEntry
  补齐价格链；当前 `missing_cost_count=0`、`current_unpriced_count=0`，Core/Scheduler integrity 均为 `ok`；
- `03ea471` 最小热修复已装入 live runtime：provider 不返回 token split 时使用 admission 时冻结的 route estimate
  记一笔 request cost；模型 profile 刷新后，新的 input/output price rate 版本会链接上一版，不再触发 immutable
  fork conflict；旧基线完整回归 196/196 通过；
- controller、writer、control、projection、backup、outbox 和 dashboard 当前健康。8 月 20 日已开始的旧 Agenda
  cycle 保持 append-only terminal `failed`，不会改写成成功；daily cycle 上限仍为 1，未擅自扩大，下一次正常
  live cycle 要等下一个日历周期。

### Connector P0-0 当前进度（未部署）

- `ExecutionInvocation` 通用超类型、Model 1:1 subtype link 和新调用原子双写已实现；
- ArtifactVersion v0.2 改用 `producer_execution_ref`，跨 v0.1/v0.2 版本索引和 dashboard projection
  已接通；
- Scheduler 已支持 `retry_at/not_before`，新 attempt event 使用 `wire_version=0.2` 声明 hash epoch；
- 203 项 Python 测试通过，含回填冲突回滚、跨代 artifact 分叉、投影和 Retry-After 幂等专项测试；
- 生产 Core/Scheduler SQLite 的临时副本已完成 startup backfill 演练：2 条 ModelInvocation 全部建立
  execution link，72 条 v0.1 artifact 全部进入跨代索引，两个副本 integrity 均为 `ok`；
- `79bca15` 本身不含 connector authority；当前 dirty P0-1 在它之上继续实现，不能把两者混成一个
  已部署基线。

此前 Connector 报告把未实际发生的 Fable 5 复核写成事实。报告已更正；随后完成的真实独立审阅结论
是“有条件 Go”。

### Connector P0-1 当前进度（authority foundation 已提交，未部署）

- 新增 `ConnectorStore`，已把 connector authority DDL 接入 trusted `DaltonStore` transaction；
- profile、call spec、logical invocation、physical attempt、Usage/Price/Cost、quota
  reservation/settlement、SourceEnvelope、incident 和 source-health 已有闭合 wire contract；
- quota admission 使用 SQLite `BEGIN IMMEDIATE` 串行化，当前只支持固定 UTC window；quota 按稳定
  scope 跨 policy version 聚合，只有 exact active policy 能 admission；每个 physical attempt 必须先
  预占 1 次调用，并同时检查并发、calls、bytes、records 和 cost_micros；
- reservation 的创建、到期和 window 边界约束 attempt 开始时间；所有当前 attempt outcome 都是终态，
  `completed_at` 不得晚于 authority 的记录时间，429 还要求 `retry_at > completed_at`；
- `Usage → Cost → Settlement` 已做逐级精确绑定；consumed 只接受 final Usage 和按 frozen price book
  计算的 actual CostEntry，
  quota policy 冻结币种；Usage/Cost correction 领先 settlement 时 admission fail closed 并开启 blocking
  `quota_drift` incident；correction 即使来自旧 quota window，也在写入同一事务立即开 incident；actual
  overage 也在 settlement 同一事务开启 incident；
- RatePolicy 冻结 exact price refs、required meters 和 price-book hash；注册时必须枚举整个 policy interval
  的完整 canonical price book，免费来源也要显式登记 zero-rate；同一 profile/meter/currency 的 rate 生效
  区间不得重叠；admission 按当前时点重新核对 price book，再按 profile 最大 bytes/records 与固定 call 数
  计算保守成本下限，调用方不能低报 `reserved.cost_micros`；Cost 还会按 physical attempt 的开始时间复核，
  漂移时不写 Cost，并持久化 blocking incident；
- SourceEnvelope 必须匹配 profile/call spec、同一 execution 生产的 ArtifactVersion v0.2、raw hash、
  source/schema/policy/provider request、明确的 result attempt 和 outcome；source/schema/content hash 都
  绑定 source identity、schema bundle 和规范化 metadata/record refs；content hash 不声称绑定 record body；
  profile 还冻结 runner environment hash；
- CallSpec 拒绝 credential-shaped 参数；
- blocking incident 会阻止新 reservation；429 physical attempt 单独记录 `retry_at`；
- `docs/CONNECTOR_PROTOCOL.md` 与 `ConnectorProposalManifest` 已定义 Dalton 自建 connector 的离线生成、
  replay、双人工 gate 和静态 resolver 边界；
- Fable 5 已做六轮只读敌对复核：前五轮持续发现并复现 authority 漏洞，第六轮冻结复核结论为
  **技术 Go**。当前可提交为 **P0-1 authority foundation**；connector 专项 23/23、Python 全量
  226/226、broker 15/15 通过。这个 Go 不包括部署、真实 connector 或 E1；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel 构建得到相同 SHA-256：
  `825f07246ed13afff39ac0c5201242ca4e756542332d6590cd40bbb6a6d7a8c5`；隔离安装后可创建 16 张 connector 表；
  live Core DB 的只读 backup 副本完成 P0-0 backfill 与 ConnectorStore 建表，2/2 model execution links、
  72 条 artifact index、16 张 connector 表，integrity 为 `ok`；
- 本机系统 Python 的 `python3 -m build` 仍因已安装的 `build` 包没有 `build.__main__` 而失败；独立 venv 的
  `pip wheel --no-build-isolation` 已通过，这不是 Connector 代码验收通过的替代条件，也不隐藏该环境问题；
- P0-1 authority foundation 已提交为 `c4e78db`；完整 Connector P0、Runner、writer RPC、真实 adapter、
  dashboard projection 和第一条真实 A 股公告 connector 尚未完成，因此不能报完整 P0 或 E1 通过。

### Connector P0-2a 当前进度（control-plane foundation 完成，未部署）

- 新增闭合 `ConnectorRunnerRequest`、`RunnerEnvironmentManifest`、`ConnectorAdapterRequest`、
  `AdapterTransportObservation` 和 `ConnectorRunnerResponse` contract/schema；
- Scheduler 新增 exact-current lease use-time gate；旧 revision、错误 hash 和过期 lease 都 fail closed；
- `StaticAdapterResolver` 只接受 operator 注入的 callable 与冻结 binding，禁止从 proposal path 动态
  import/exec；CapabilityDescriptor 的实现来源与 ConnectorProfile 的目标数据源分开建模，live descriptor
  contract 的 adapter/input/output schema refs 必须与 Profile/manifest 一致；
- Runner admission 从 Core、Scheduler、CapabilityCatalog 和 ConnectorStore 重读 authority，外部 request
  只携带 refs/hashes；静态 input validator 按冻结 schema 检查完整 parameters；内部 AdapterRequest 的
  parameters、host、policy、deadline 和 response 上限只使用最后一次 authority 重读结果；
- Runner 专用 reservation API 派生 exact active policy version、连续 attempt、Profile 最大 bytes/records、
  保守成本和 TTL；transport gate 只接受唯一 open reservation，并再次核对 hash、有效期、price book、
  blocking incident 和 circuit state；reservation 验证后再做最终 Scheduler/Capability use-time gate；
- P0-2a 只接受 `auth_mode=none`，不接受任何 credential grant；raw sink handle 由 Runner 按
  invocation/reservation/attempt 确定性派生，调用方不能传路径或 handle；
- Runner reservation 在 `BEGIN IMMEDIATE` 内核对 next authority attempt、同一 invocation 的 pending
  唯一性并写入；`max_concurrency=2` 的两个独立 SQLite connection 并发探针最终只产生 1 行 reservation；
- live Descriptor 还必须满足 `kind=connector`、`mode=typed_call`、`instruction_ref=null`；Manifest、
  AdapterRequest 和 Profile 的 auth/credential、public-only、redirect 条件已在 JSON Schema 与 Python
  validator 两边统一；
- Fable 5 前两轮复核均为 No-Go，第三轮在复跑旧探针及上述并发/Schema/Descriptor 探针后给出
  **Go**，只批准 P0-2a control-plane foundation；相关测试 58/58、Python 全量 236/236、broker 15/15；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel 构建得到相同 SHA-256：
  `0e198638dcde7b52ccc756715eef20da5ca9a1ead8f91fb72cb1c8d3f2d25881`；隔离安装可导入 Runner、创建
  16 张 connector 表，integrity 为 `ok`；
- 当前仍只是 control-plane seam：没有执行 adapter、没有 credential grant、durable runner journal、raw spool、
  writer authority port、SSRF transport 或 recorded success/429/crash replay，不能称 P0-2 完成。

### Connector P0-2b 当前进度（recorded transport thin slice 完成，未部署）

- Fable 5 先做独立架构复核，结论为“有条件 Go”；P0-2b 仍是正确下一阶段，但必须先修正非成功
  RunnerResponse 无法表达没有 raw artifact/SourceEnvelope 的契约缺陷；
- `ConnectorRunnerResponse` 发布 wire 0.2：成功必须带 raw artifact 和 SourceEnvelope，429/timeout/failed
  可以把两组 ref/hash 同时置空，禁止用空 artifact 凑契约；其余 connector contract 不原地改 epoch；
- Core DB 新增 append-only runner request/event journal，唯一 `transport_started` barrier 决定恢复只能
  released 还是必须 indeterminate；journal 不属于 Research Ledger，也不保存 Scheduler lease token；
- raw spool 使用 write-only bounded sink、SHA-256 content address、原子 finalize、同 hash 去重、全局高水位
  和 orphan partial GC；adapter 不接收路径；
- 窄 `ConnectorAuthorityPort` 只开放 attempt、Usage、Cost、Settlement、ArtifactVersion v0.2、
  SourceEnvelope 和 Scheduler completion 七类写入，不暴露 connection；
- recorded success、empty、429、timeout、adapter exception 已执行完整事实链。timeout 由 Runner hard
  watchdog/deadline 判定，observation 不能自报；非最终计量使用 reserved cost upper bound 并按
  indeterminate 结算；
- W0–W4 以及 attempt/Usage/Cost/Settlement/Artifact/Source/Scheduler 每个写入缝隙都做故障注入；恢复后
  第二次重放零新行。journal 完全缺失的 reservation 也按 indeterminate，不误放为 released；
- P0-2b 不含真实网络、SSRF、credential authority、writer RPC、metadata importer、spool lifecycle、
  ContextPack/Checkpoint/ClaimIndex、部署或真实数据源；Python 254/254、broker 15/15、专项 18/18 和
  确定性 wheel 已通过，Fable 5 最终复核及增量复核均为 **Go**；实现提交 `f0e824f`，GitHub CI 最终
  全部通过。完整结果见本轮实施报告。

### Connector P0-3 当前进度（metadata + public transport safety，未部署）

- 新增闭合 `OpenClawCapabilitySnapshot` 和 `OpenClawMetadataImporter`：skill 只导入 compact metadata、
  opaque instruction ref/hash；MCP 只导入 metadata 与闭合 input/output JSON Schema。skill 正文、prompt、
  tool output、路径、server config 和 credential 不进入 Dalton；
- imported skill/MCP 仍只是候选 metadata，不能自动进入 CapabilityCatalog。`publish` 继续要求现有 human
  promotion receipt，并强制 descriptor 的名称、摘要、source、contract、source/schema hash 与 current
  imported metadata exact match；caller 不能借 importer 改写摘要或权限；
- 完整 scope 中的 source/schema/metadata 变化或 capability 删除，会在同一事务中撤下 current descriptor
  projection 并推进 catalog epoch；旧 lease 因 epoch 变化 fail closed。重复 snapshot 不推进 epoch；
- 新增 credential-free `PublicHttpTransport`：只允许 exact-host HTTPS/443，拒绝 URL userinfo、credential-
  shaped query/body/header、环境代理语义和非幂等 redirect；每一跳都重新检查 allowlist、DNS 全量 IP 与
  redirect，任一 private/loopback/link-local/reserved 地址即拒绝，并把 socket pin 到已验证 IP、保留原
  hostname 做 TLS SNI/证书校验；response size 同时检查 Content-Length 和 streaming bytes；
- 新增 closed `CredentialGrantEnvelope` 与 `CredentialAuthorityPort` 边界。Core 只见 grant metadata 和
  logical slot ref；credential value、OAuth/MCP auth 与不可序列化 handle 留在 host-owned authority。
  Public transport 的 API 不接受 credential grant，现有 ConnectorAdapterRequest 0.1 仍强制
  `credential_grant_ref=null`；
- 当前只完成离线 control-plane/transport component。尚无 OpenClaw live exporter/sync daemon、真实 HTTP
  call、authenticated runner、A股/SEC/AlphaEngine connector、dashboard connector projection、部署或数据
  源访问。AlphaEngine 的 `mcp_managed` profile/runner wire 需要独立版本，不能把 loopback MCP 塞进 public
  HTTPS `allowed_hosts`；
- importer/public transport/credential 专项 15/15、Python 全量 269/269、broker 15/15、`compileall` 和
  `git diff --check` 全部通过。固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel SHA-256 均为
  `c9af233004f0a6bed406572f97c1802cef06ddefc20b0c17728302bf7138ac86`；隔离安装可导入三个新模块、
  创建 6 张 external metadata 表并找到 2 份新 contract，SQLite integrity 为 `ok`。P0-3 复核时 system
  Python 3.13 曾因缺少 `setuptools.build_meta` 无法走 no-build-isolation；当前项目 `.venv` 是 Python 3.13.14，
  已含 setuptools 84.0.0 与 `setuptools.build_meta`，本轮 no-build-isolation 重复构建通过，不需要全局安装；
- 实现提交 `e1ab94c`；GitHub CI 的 broker、Python 3.11 和 Python 3.13 全部通过：
  <https://github.com/everflowinv/dalton-research-agent-os/actions/runs/31828754012>。

### Connector P0-4a 当前进度（trusted metadata sync + Connector Shadow projection，未部署）

- `OpenClawCapabilitySnapshot` 发布 wire 0.2，新增 exporter source instance、exporter version、严格递增的
  catalog generation 和 exact prior snapshot ref/hash；
- Catalog 内部新增 trusted source registration 和 per-source head authority。只有 operator resolver 返回的
  active human registration 才能启用新 source instance；实例更换会撤下旧实例的 current metadata 与
  external-scope descriptor、推进 epoch，旧实例也不能自行复活；registration receipt 还绑定 reset 前的
  exact active source/hash，两个并发 reset 只能有一个成功；首次注册也会撤下 P0-3 legacy current state；
  wire 0.1 禁止有限 expiry，只能显式换实例；
- 同 source instance 只接受 exact next generation 与 exact prior head。同 generation/同 hash 是幂等重放；
  stale、gap、fork、equivocation 和未注册 source 都 fail closed，只追加脱敏 ingest event，不改 current
  metadata、descriptor projection 或 catalog epoch；
- host-owned exporter 使用 owner-only SQLite 保存一个 pending snapshot，Catalog 已接受但 exporter 尚未
  acknowledge 时，重启后会重放同一 snapshot，不会跳 generation；exporter 只接收已过滤的 compact
  skill/MCP records，不保存路径、instruction、server config 或 credential；
- skill 的 approval-bound schema hash 现在也绑定 exact upstream metadata hash。prompt-like description 可以
  原样进入隔离 staging，但不能在 human approval 前进入可搜索 Catalog；
- P0-3 既有 SQLite 不改写 snapshot 表：wire 0.2 的严格 source/generation/prior/FK/unique/check authority 放在
  1:1 sidecar chain 表。旧 snapshot 保持 immutable 历史行且不伪造 operator registration；fresh 与升级库
  使用同一份 DDL，新注册 source 从 generation 1 建新链；
- snapshot 接受事务的 snapshot、head、ingest event、schema/metadata 与 descriptor withdrawal 原子提交；故障
  注入后可以重放同一 generation；同一代并发 loser 会持久化 equivocation event，不再提前变成无事件的
  `StaleCatalog`；
- Fable 5 的四轮增量复核先后复现并关闭 incomplete reset、fresh/migrated DDL 分叉、有限 expiry、并发无事件
  和首次 registration cutover 缝隙，最终裁决为 **Go**，仅批准 P0-4a Commit A；metadata 专项 19/19、
  Python 全量 280/280、broker 15/15、`compileall` 和 `git diff --check` 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel SHA-256 均为
  `d06474d8292edcca7efcefaa1c2ee5b4adaec023b941b16aa1e34a1235b4a178`；隔离安装可创建 11 张 external
  metadata authority 表，`foreign_key_check` 无违规，SQLite integrity 为 `ok`；
- 第二笔提交新增 disposable Connector Shadow projection。Projector 以 SQLite `mode=ro` 读取 Core/Catalog，
  投出 metadata source head/freshness/reject、profile/operation、physical attempt/retry、每个 attempt 最新
  Usage/Cost/Settlement、quota window、health/circuit 与 incident；固定 API 和静态快照页面同步接入；
- 投影不含 raw body、authority `record_json`、incident detail、credential、provider request/usage ref 或
  Core/Catalog 路径，也不参与 admission。完全缺少 P0-4/connector authority 表时向旧 baseline 返回 warning
  与空集合，部分表存在时 fail closed；watermark 覆盖新增 authority；
- Fable 5 增量复核关闭同 timestamp latest-event 排序和 partial schema 漏检后给出 **Go**；dashboard/
  dashboard-projector 20/20、service/static 7/7、Python 全量 284/284、broker 15/15 通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 Python 3.13 no-build-isolation wheel SHA-256 均为
  `655f4af42fa0db54524ad5512fc29d6eea4320c64777fe3014918622a7fe7910`；隔离安装可创建 projection schema
  0.2 的 5 张新 read-model 表，HTML 含两个新 API endpoint，SQLite integrity 为 `ok`；
- 这两笔提交仍未把 exporter 接到 OpenClaw live inventory，也没有真实网络、数据源访问、部署或研究
  WorkOrder。P0-4a Commit B 的最终测试、wheel、Fable 5 复核与提交信息见本轮独立报告。

### Connector P1-0 当前进度（complete inventory + recorded reference shadows，未部署）

- P1-0a 冻结十个独立 profile：CNINFO、SEC、AlphaEngine、X/xreach、X/x_search、Reddit/last30days
  keyless、Guidepoint、Gemini web search、public web fetch 和雪球；X 的枚举/语义搜索与 web 的搜索/抓取
  不合并；雪球只允许 `get_hot_stocks` 走带 `source_ref/adapter_ref/provenance_label` 的
  `cn-hk-findata xq_hot_rank` fallback；
- 每个 profile 都有闭合 operation/input/output/pagination/completeness/auth/transport contract、逐 operation
  synthetic fixture matrix 和 proposal-only manifest。十类当前均为 `inventory_connected`，不产生 lease、
  不请求 canary，也不代表 live connector 已接通；
- Inventory loader 最终把已验证 package graph 与 deterministic build 逐对象精确比较；authority ref、时间戳、
  fixture scenario/error/raw/auth 语义和 graph hash 任一漂移都 fail closed。P1-0a 提交 `976548e` 已获
  Fable 5 **Go**：专项 12/12、Python 296/296、archive wheel 安装和 31 个 packaged JSON 逐字节检查通过；
- P1-0b 新增 CNINFO `list_announcements` 与 SEC `list_filings` 的离线 recorded reference shadow。每页独立
  reservation、physical attempt、Usage、Cost、Settlement 和 raw ArtifactVersion；多页共用一个 logical
  invocation，并用 AdapterRequest 0.2 把 parent query、上一页 request/observation/attempt hashes 与 cursor
  绑定到下一页参数；bounded window、页数上限、revision chain 和 completeness 全部显式；
- Runtime fixture 必须与 packaged deterministic fixture 逐对象相同，并冻结 parent parameters/query hash；
  plan 与 AdapterRequest 显式绑定 selected scenario。normalized output 必须通过 inventory 的 closed schema，
  runtime profile 的 hosts/network/operation/schema/fixture/package graph 任一漂移都在 adapter 前拒绝；
- 成功/empty/partial 才生成 SourceEnvelope；schema drift、429、timeout、malformed 不生成 raw artifact 或
  SourceEnvelope。每个成功页在 page commit 内独立注册 ArtifactVersion；page recovery 覆盖 reserved、
  transport_started、observed、artifact/responded barrier 和第二页 capacity failure；
- response journal 先持久化 closed result/response/page receipts/commit context，再通过窄 AuthorityPort 做
  Scheduler reconciliation。Scheduler 从构造时绑定的 exact RunnerJournal 与同一 Core store 的
  ConnectorCompletionReceiptReader 读取全部事实，不接收 caller 声明的 event hash/time；只有 parent
  `recorded_at` 决定 lease 内完成。page observed/transport_started 不足以在过期后完成旧 attempt；lease 内已
  持久化的 parent completion 可在过期后收敛，later attempt 已重新 claim 时 fail closed；
- 父级 ResultEnvelope/RunnerResponse 由 deterministic builder 从 request/context、全部 page receipts 和
  Connector/Artifact/Source authority 唯一生成，并整份精确比较；额外 output/metadata/side effect、空 authority
  或分页串页即使重算 hash 也不能形成 formal result；
- Fable 5 对 `9599ea8` 至 `bf7c169` 的多轮复核发现并推动关闭了 fixture/runtime graph、page recovery、输出
  schema、query/scenario、lease proof、caller 时间、inner fact chain、分页串页和开放 parent completion 等
  问题。最终 committed-tree 审阅对 `2cb671e` 给出 connector 代码 **Go**：专项 21/21、组合超集 92/92、
  broker 15/15、敌对 completion probes、compileall、diff-check、clean install 和 SQLite integrity 全部通过；
- 独立审阅在 UTC 跨日后把全量基线重跑为 316/317，唯一失败是既有 Agenda `decide_cycle` 重新按真实时间
  查 active policy，而没有读取 cycle 冻结的 policy version。后续修复改为 exact frozen policy binding，并加
  active policy 中途换版回归；当前 Python 全量为 319/319；
- `2cb671e` 的两次 archive wheel 独立复现 SHA-256 均为
  `8775adbeaa6c901801e84dfe3652cdaa312912d65301cb13173c068edae23f58`。旧记录
  `ca0e6a9e...` 无法从任何现存提交复现，不能继续作为验收事实。工具链为 Python 3.13.14、setuptools
  84.0.0、pip 26.1.2、`SOURCE_DATE_EPOCH=1700000000`；Agenda 修复后的工作树 wheel 两次逐位一致，
  SHA-256 为 `0df85b85981fd14ebc095bf9ecd5ff86377d11a4ac8822ffc1166dea01bbbd04`；
- P1-0c 在十件 frozen inventory loader 之外新增声明式 proposal package loader。每个 package 只能包含
  `profile.json`、`fixture.json`、`proposal.json`，第 11 条及后续 proposal-only connector 不改中央
  `PROFILE_DEFINITIONS` 即可完成 offline graph/schema/fixture 验证。loader 递归闭合验证 operation schema，
  保留冻结十件的 slug/connector identity，固定 adapter version 与 transport/auth required gate，并拒绝 symlink、
  超大文件、重复 JSON key、非 synthetic fixture、执行权限升级、敏感配置和跨对象 graph 漂移；冻结十件 loader
  保持不变。Fable 5 对 `8b13e26` 给出无条件 **Go**；inventory 专项为 17/17、Python 全量为 322/322、
  broker 为 15/15，两次 committed archive wheel SHA-256 均为
  `23ce64bdfb5b74cad4344fac314da43d2b88f459f022aabdd7cc2e35df27a51b`；
- connector 语义选择不在每个 physical call 重做。Planner 在 WorkOrder/协调器边界一次选定 source、operation、
  completeness 和 fallback；Runner 每次调用只做确定性的 lease、quota、host/auth、schema 与 provenance gate。
  `CompiledConnectorPlan` 等到 P2 coordinator 有真实消费者时再加入，不提前建立独立 Router；
- 本轮没有部署、没有访问真实数据源、没有使用 credential，也没有写 Evidence/Claim/Thesis。真实 public
  network 仍被 killable total-deadline transport gate 阻塞；AlphaEngine/Guidepoint/雪球等 host/MCP 路径仍被
  runner wire 0.2、credential revoke/max_calls use-time authority 阻塞。

### Connector P1-0d 已完成（AlphaEngine offline `mcp_managed` shadow，未部署）

- 新增独立 `mcp_managed` RunnerRequest/AdapterRequest/TransportObservation wire 0.2；该路径没有 URL、host、
  public network policy 或可序列化 credential value，不能复用 public HTTPS transport；
- Credential authority 只保存 grant、revoke 和逐次 use receipt 的闭合 metadata。每次使用精确绑定 profile、
  capability lease、adapter、principal、credential slot、operation、reservation 和 physical attempt；在返回
  host-owned opaque handle 前再次检查 revoke、expiry 与 `max_calls`；
- AlphaEngine 当前只实现 `search_library` 的离线 recorded shadow。fixture 精确绑定 frozen inventory、parent query、
  selected scenario、input/output schema 和 transport target；success、empty、partial、pagination、schema drift、
  rate limit、timeout、malformed、permission denied、revoked 均不访问真实 MCP；
- 成功调用继续走既有 reservation → attempt → Usage → Cost → Settlement → ArtifactVersion → SourceEnvelope →
  Scheduler completion；失败不得伪造 raw artifact 或 SourceEnvelope。Runner-owned deadline 会中止超时 fixture 的
  sink；credential 在 reservation 后形成 use receipt，并在 adapter 前做最后一次 use-time 验证；
- connector 的语义路由没有进入每个 physical call。Planner/协调器一次选定 source、operation、parameters、
  completeness 与 fallback；Runner 的分页和重试只执行本地确定性 authority gate。一个稳定 source 可以暴露
  多个 operation，跨 source 的 findata 体验由上层 research recipe 组合；
- 新 connector 继续走声明式 proposal package。P1-0d 只增加 AlphaEngine 的运行时参考链，不把中央语义路由
  做成新服务，也不提前加入尚无消费者的 `CompiledConnectorPlan`；
- MCP/credential/Runner/transport/packaging 组合 41/41、Python 全量 341/341、broker 15/15、`compileall`、
  `git diff --check` 和 deterministic fixture regeneration 均通过。两次 committed-tree Python 3.13
  no-build-isolation wheel 逐位一致，SHA-256 为
  `1d18058a2f00ecee014da41c0c1dd4a360067df1b700c20febd5899272b1349f`；干净安装、packaged fixture、四张
  credential authority 表与 SQLite integrity 均通过；
- Claude Fable 5 对 `e48d76b` 的 committed tree 给出 scope-limited **Go**，没有 P0/P1。下一阶段选 P2
  coordinator foundation：先做 ContextPack、RunState、Checkpoint、ClaimIndex，再由首个 fixture-only consumer
  引入一次性 `CompiledConnectorPlan`；Guidepoint shadow 延后到真实 research recipe 需要时；
- 本轮没有读取 AlphaEngine token、没有调用本地 MCP、没有访问真实数据、没有部署，也没有写
  Evidence/Claim/Thesis。`get_document` 仍停留在 inventory；Guidepoint、雪球和 live MCP 仍为 No-Go。

### Connector P1-0e 开发候选（AlphaEngine live bridge，未部署）

- 新增 live MCP transport plan 0.1 和 AdapterRequest 0.3。两份闭合 contract 把 exact
  `CompiledConnectorPlan/step`、frozen AlphaEngine inventory/schema、operation、参数、bridge 和 transport target
  绑在一起；endpoint、token、cookie、server config 和任意 tool name 都不能进入可序列化对象；
- host-owned bridge 当前只允许带显式端口的 loopback `/mcp`，关闭 proxy 和 redirect，并限制 tool allowlist、
  deadline、response bytes、strict UTF-8/JSON/SSE 和 JSON-RPC request id。Core 只拿 opaque handle；
- `LiveMcpRunnerAdmissionGate` 在 quota 前后重检 Runner、Catalog、profile、resolver、call、invocation、lease、compiled
  plan 和 transport plan；每个 physical call 形成独立 credential use receipt。credential use 的幂等 hash 不再受
  authority 当前时钟影响，scalar/collection 不能冒充 opaque credential handle；
- `search_library` 固定使用 relevance，并把 frozen company/date/document type/geography/industry 参数映射到真实
  AlphaEngine schema；`get_document` 只接受 `alphaengine-doc:<doc_id>` 和数字 offset cursor。成功结果先写 exact raw
  JSON-RPC Artifact，再生成 hash-bound SourceEnvelope；不生成 Evidence、Claim 或 Thesis；
- fake source 端到端测试已覆盖 complete/partial normalization、exact raw artifact、credential/quota、compiled-plan
  tamper、duplicate replay 和 `after_observed` crash recovery。恢复只重放 authority 写入，不再次调用上游；
- 真实 AlphaEngine 只读 canary 已完成 `search_library → get_document`：搜索得到 10 条结果，文档调用得到首个
  30,000 字符分片、内容 SHA-256 和 `next_cursor=30000`。这次 canary 直接验证 bridge/adapter，没有接入 live Core
  authority，也没有证明完整文档；
- bounded multi-page coordinator 已新增 plan/page/manifest 三份闭合 contract。每页作为独立 physical call 记录
  calls/bytes，只有首页消耗 1 个 document unit；逐页回查 Invocation、Usage、Settlement、raw Artifact 和
  SourceEnvelope，触及限制只形成 partial，终页整文 hash/长度一致才形成 complete manifest；
- 尚未部署，没有 production Catalog/profile/grant/mapping，没有模型调用，没有 Evidence/Claim/Thesis mutation。
  production ResearchPlan/Scheduler 接线与真实完整文档 canary 仍须单独验收；下一开发切片是 Gemini web search
  discovery 和独立 web fetch。

### P2 coordinator foundation 当前进度（fixture-only，未部署）

- 新增闭合 `CompiledConnectorPlan`、`ContextPack`、`ClaimIndex`、`ConnectorCompletionReceipt`、
  `ResearchCheckpoint` 和 `ResearchRunState` 0.1 contract/schema；所有对象都拒绝未知字段并校验 canonical hash；
- Planner 在 WorkOrder 边界只生成一次三步计划。每个 RunnerRequest 精确绑定 plan/step ref/hash；Runner 的
  physical call 不重复语义路由，只继续执行既有本地 authority gate；
- ContextPack 只保存 ref/hash 和冻结后的 token/byte 选择账，不保存正文；ClaimIndex 只从已有 ClaimVersion、
  EvidenceRelation 和 ledger snapshot ref/hash 构建可重建搜索投影，不提供 Ledger 写 API；
- `FixtureResearchCoordinator` 只持 `ConnectorExecutionPort.execute`。参考 port 只读取 packaged CNINFO、SEC、
  AlphaEngine fixtures，不导入 network/MCP client，不接受 credential；coordinator 自身不持 Connector、Scheduler、
  Credential、Core 或 Research Ledger DB handle；
- owner-only scratch SQLite 只保存 immutable plan/context/index/checkpoint/run-state projection。checkpoint chain
  精确绑定 attempt、plan、context、step、连续 connector attempt 和 completion receipt；数据库可删除重建，
  不属于 Research Ledger 或 connector completion authority；
- fault injection 覆盖 execute 后、checkpoint 后和 state 后恢复。execute 后未写 checkpoint 时复用同一个
  idempotency key；checkpoint 已写后不重复调用。429/retryable 立即返回，不 busy wait；每 step 最多两次，
  耗尽后 run 终结为 failed；
- Fable 5 对首个 committed tree 给出 scope-limited **Go**。随后关闭 public runner 把调用方 plan binding 当
  authority、RunState 首 checkpoint 前可换绑、恢复不重验 prior checkpoint chain、ContextPack 不重算选择结果和
  fixture port 幂等键分叉等债务；
- Fable 最终增量复核仍为 **Go**，没有 P0；复核发现的 MCP 0.2 gate plan-binding 不对称也已关闭，两个 gate
  现在共用同一拒绝函数。敏感键分隔符归一化和 checkpoint authority/RunnerRequest/idempotency 恢复重验一并完成；
- 新增专项 14/14，相关 runner/MCP/coordinator 组合 38/38，Python 全量 357/357、broker 15/15、`compileall` 与
  `git diff --check` 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 两次 wheel 逐位一致，SHA-256 均为
  `0077ca167f0b7626910edb10aac719b11e7a08bbea3062f61be0d33eeb5cade6`，每份 507,712 bytes；干净 venv
  安装、`pip check`、三步 plan build、packaged SQL 和 SQLite integrity 均通过；GitHub CI `31869944201` 的
  Python 3.11、Python 3.13 和 broker 三个 job 全部通过；
- 本轮没有部署、没有访问 live source/MCP、没有读取 credential、没有写 Evidence/Claim/Thesis，也没有切换
  旧 cron。其后的 offline source/numeric verifier 与 candidate staging candidate 见下一节。

### P2 offline verifier + candidate staging 当前进度（fixture-only，未部署）

- 新增 closed `SourceVerificationMaterial`、`NumericVerificationSpec`、`VerificationBundle`、
  `CandidateEvidence` 和 `CandidateClaim` 0.1 schema。verification bundle 固定 verifier ref/hash；候选对象使用
  candidate-only identity，不能当作正式 Ledger version；claim 的叙述语义在人工 review 前固定为 `unverified`；
- source verifier 重新核对 plan/context/step/request/receipt/checkpoint/authority binding，并从 packaged fixture
  重算 raw payload、synthetic source summary、artifact、schema、record refs、lineage、completeness 和时间顺序；
- numeric input 必须用 exact material ref/hash + JSON Pointer 从 verified raw payload 重新抽取。数值使用 canonical
  Decimal string，只开放 `identity / sum / difference / ratio`，并核对 unit、currency、scale、period 和 rounding；
- `CandidateStagingStore` 使用独立 owner-only SQLite 和 append-only trigger，不导入 `DaltonStore`，也不创建
  Evidence/Claim/Thesis 表。staging 会重新执行两个 verifier 并要求结果 canonical equality，调用方自报 pass 无效；
- stage request 在 `BEGIN IMMEDIATE` 事务内保存 material/spec/verification/candidate/idempotency。事务内崩溃全部
  回滚；commit 后返回前崩溃用同一 idempotency key 返回 duplicate；同 key 不同 request fail closed；
- 专项 7/7，coordinator/verifier/packaging 组合 22/22，Python 全量、broker、build 和安装结果见本次实施报告；
- 当前 SourceEnvelope/Artifact 仍是 P2 synthetic summary ref/hash，不是 live connector authority record。真实只读
  WorkOrder 前必须增加 authority resolver 并复核完整 checkpoint chain；本阶段不部署、不接 Agenda、不读取凭据、
  不写正式 Research Ledger。

### P2 authority resolver + SEC public canary 当前进度（隔离验收，未部署）

- 新增 closed `AuthorityResolution`、`AuthoritySourceVerificationMaterial` 0.2 和
  `ConnectorCompletionReceipt` 0.2。旧 0.1 receipt 保持兼容，但真实成功链必须同时绑定 coordinator 的
  plan request 与 Connector Runner 实际执行 request；
- `ConnectorAuthorityResolver` 只读连接 Core、Connector、Observability、Scheduler、RunnerJournal 和
  coordinator scratch。它重算 raw Artifact、SourceEnvelope、Profile/CallSpec/Execution/WorkOrder、
  ResultEnvelope/formal Scheduler event、physical attempt、Reservation、最新 Usage/Cost/Settlement、
  AdapterRequest/observation/response 和完整 checkpoint chain；任何换绑、缺失、部分结果或篡改都 fail closed；
- SEC adapter 只允许 `https://data.sec.gov/submissions/CIK{cik}.json`，不接受 credential handle，沿用
  public transport 的 DNS/IP pinning、redirect 和 response-size gate。normalizer 严格拒绝重复 accession、
  非法日期、不明 amendment revision、超窗口和静默 limit 截断；
- isolated canary 使用临时 SQLite/raw spool 和 synthetic canary approval，不打开 live DB。真实 Microsoft
  `CIK0000789019` 2025 10-Q 请求返回 3 条 filing，最终进入独立 candidate staging；状态为
  `human-review-ready-candidate`，`semantic_verification_status=unverified`；
- authority/SEC/Agenda 专项 8/8、相关 connector/coordinator/verifier 回归 41/41、Python 全量 370/370、
  broker 15/15、`compileall`、schema 解析和 `git diff --check` 通过；固定
  `SOURCE_DATE_EPOCH=1700000000` 的两次 Python 3.13 wheel SHA-256 均为
  `d85ad4ecb466a18f3447549a3765f6561eba025a6b8bbed33baee3469dec22ae`，557,781 bytes；干净安装、
  `pip check`、新增模块、14 份 packaged SQL 和 88 份 packaged contract schema 检查通过；实现提交
  `002ebda`，GitHub CI `31878953063` 的 Python 3.11、Python 3.13 和 broker 三个 job 全部通过；
- 本轮不部署、不接 Agenda、不读凭据、不写 Evidence/Claim/Thesis，也不切换旧 cron。人工 review authority/
  入口、正式 commit 和生产 connector promotion 仍是独立 gate。

## 蓝图阶段

### Phase 0：记录和可观察性——主体完成

已完成：

- 94 份 JSON Schema、16 份 SQL schema；
- immutable DomainEvent、WorkOrder、ResultEnvelope、ModelInvocation；
- Evidence → Claim → Thesis 版本链、verification 和 commit gate；
- Workflow、Artifact metadata、模型 Usage/Cost、只读 projection 和静态看板；
- legacy workspace/database/cron 的 shadow import；
- owner-only writer、每日 SQLite backup、已完成的 restore 演练与数据库完整性检查。

部分完成：connector authority foundation 已有 16 张 append-only 表、trusted store、wire contract 和只读
Connector Shadow projection，
并通过六轮独立复核；Runner 控制面、recorded adapter execution、durable journal/raw spool、W0–W4 recovery
和窄 AuthorityPort 已完成 P0-2b；metadata importer、credential-free SSRF-safe public transport 和
credential authority metadata boundary 已完成 P0-3 离线切片。真实 connector 调用、authenticated runner、
writer RPC、完整 source-health ledger、生产对象存储生命周期和跨机灾难恢复仍未完成。

### Phase 1：Agenda Engine Shadow——单公司运行中

已完成：

- Mandate、PriorityOverride、ResearchQuestion、AgendaCycle、AgendaDecision；
- PerceptionSnapshot legacy adapter；
- Scheduler → Model Router → OpenClaw broker → provider usage/cost → 确定性选题；
- append-only outbox、marker reconciliation、receipt、补投和 stale-attempt 拒绝；
- Tailscale HTML agree/disagree、24 小时 timeout 默认接受和独立统计；
- 全局 pause、一次性 human governance CLI、Agenda 监督 projection。

当前 live 范围只有万华。Agenda 只选题，不执行研究，不写 Evidence、Claim 或 Thesis。旧
`dalton-coverage-*` 10 条 cron 继续运行。Connector Shadow 可以并行建设，但其输出不得接入当前
Agenda Perception；否则会在 10 日评估窗口中途改变输入分布，污染现有 shadow 指标。

### Phase 2：低风险自主闭环——部分底座完成，研究闭环未接通

已完成：Scheduler lease/retry/idempotency、ProcessRuntimeAdapter、六模型 exact route、OpenClaw 模型
broker、预算和 pause gate。

部分完成：connector runner、recorded transport、fixture-only coordinator、offline/authority source-numeric
verifier、只读 authority resolver、candidate-only staging、HumanReviewAuthority 和无损 Ledger promotion 0.2
已完成；真实 SEC public source 与 review/commit 只在隔离测试运行，尚未接 Agenda 或生产 authority。
未完成：原生事件 connector、从 AgendaDecision 到 research DAG 的 production planner、
`ready → connector/worker → verifier → revise/commit` 完整 coordinator，以及生产化只读研究 WorkOrder。

### Phase 3：Verifier 与 Thesis Commit——权威机制完成，运行层未开始

已完成：独立性 predicate、VerificationRecord、Evidence/Claim/Thesis gate、adjudication、不可变
版本、原子 commit、幂等和事务失败回滚约束。这里没有 thesis 业务版本回滚；当前可激活历史版本的
rollback 只存在于 Capability Registry。

部分完成：source/numeric verifier 已有 synthetic fixture replay、真实 Connector authority replay、
换绑/数值错误探针，以及 explicit human review 后的 Evidence/Claim 0.2 原子 commit。
未完成：生产 authority verifier、completeness/investment-link verifier、seeded-error 校准、局部返工和任何
live thesis commit；review 入口仍未部署。

### Phase 4：能力自主改进——治理半边完成

已完成：CapabilityProposal、Evaluation、Decision、Registry、Catalog、lease、attestation contract、
human promotion 和 rollback；builder 不能自评或自批。

已完成：Connector Protocol 0.1 文档、闭合 `ConnectorProposalManifest` schema 和 executable validator。

未完成：重复任务/gap detector、代码生成器、真实 sandbox service、历史回放 runner、可信 canary、
上线监控和自动 rollback trigger。当前 attestation 验证器不执行代码，
且明确禁止网络、凭据和 Core DB。

### Phase 5：多 runtime 与规模化——替换边界完成

已完成：Dalton-native process runtime、Pi/DeepSeek Harness spike、OpenClaw model/bridge adapter seam。

未完成：production worker manager、多 runtime coordinator、独立 OS/container identity、跨机服务身份、
Postgres/Temporal 规模化门槛和迁移。

### 仍未完成的横切蓝图

- 完整 coverage requirement/mandate policy 与 HTML steering；当前 HTML 只处理 Agenda feedback，全局
  agenda pause 仍走 CLI，通用 cancel/approve/emergency-stop command/event bridge 未完成；
- native event inbox，以及 expiry、catalyst、falsifier、source failure 触发；Agenda portfolio pools 和
  跨公司容量校准未完成；
- production planner DAG、stop/cancel 和 worker manager；fixture coordinator 已有 checkpoint/resume，尚未接
  Scheduler/Agenda/live connector；
- P2 已有 typed ContextPack、per-attempt RunState/Checkpoint 和结构化 ClaimIndex；尚缺版本化 retention policy
  与 authority DB 之外的滚动 OpsTelemetry；session transcript 和 compaction summary 不作为研究 memory；
- operational verifier 已有 fixture 与隔离 SEC authority source/numeric thin slice；尚缺生产 authority reader、
  completeness/investment-link verifier、revise/replanning/reflection 和 seeded-error 校准；
- first-class falsifier/catalyst/driver/model/valuation authority、Model IR、Tier 1/2/3 evaluator 和
  Excel exporter；
- generic research review/delivery outbox；现有 outbox 只服务 Agenda Discord 通知；incident ledger、
  production object lifecycle 和 offsite disaster recovery；
- hostile-code OS/container identity 与 sandbox，以及 production multi-runtime/scale。

首个 live thesis commit 前的 confidence contract debt 已由 ADR-0001 裁决：新 `ThesisVersion` v0.2 只接受
`low / medium / high`，旧 v0.1 float 只读兼容。US IT Services / ACN 初始 coverage thesis 只允许 human admission；
candidate 建立后，旧 model-verification commit 不能创建或修改同一 thesis。

## 已上线的运行面

- `space.lumos.dalton.writer`：独占 Core authority DB；
- `space.lumos.dalton.controller`：lease sweep、Agenda、projection、backup、outbox 和 health；
- `space.lumos.dalton.control`：Tailscale 内的 Agenda HTML 控制面；
- OpenClaw model broker：复用 host-owned model authentication，Core 不读取凭据；
- 公开只读看板：<https://eve.lumos.space/dalton/>；
- 私有 Agenda 控制面：`https://everflowdemac-mini.taild2c767.ts.net:8793/`。

已知限制：Mac mini 本机的 Tailscale CLI 与 daemon 版本不一致，本机验收使用显式地址映射完成；
尚未从第二台 tailnet 设备实测私有控制页。

已部署验证基线：Python 195/195，OpenClaw broker 15/15，Python 3.11/3.13 wheel build 与 GitHub CI
通过；Core、Scheduler、Model Router SQLite integrity 均为 `ok`。当前工作树的专项测试、构建和 CI
状态不能与 live 基线混写。US IT Services / ACN 开发候选已完成 Python 658/658 全量回归和 Python 3.13
sdist/wheel 构建；尚未提交远端 CI，也尚未部署。

## Connector Fabric Shadow

### 边界

现有 OpenClaw skill/MCP 不整体迁入 Dalton。每项能力拆为：

1. connector：取数、分页、限流、原始响应留痕、结构化返回；
2. normalizer：转成带 provenance 的 typed record；
3. research recipe：决定查什么、如何交叉验证；
4. delivery：继续走 durable outbox。

CapabilityCatalog 已支持 `kind=connector`、`skill/mcp/tool/plugin` 来源、typed call/process、权限、
credential slot、lease 和 human approval。下一阶段复用这些边界，不另建第二套能力注册表。

### 必须补齐的契约与权威状态

- `ExecutionInvocation` 通用超类型，以及 Model/Connector 1:1 子类型；历史 ModelInvocation 采用
  additive backfill，新调用由 writer 在一个事务中同时登记通用与模型行；
- immutable `ConnectorCallSpec`：source-specific 参数和 schema hash 由 WorkOrder `input_refs` 引用；
  connector RPC frame 必须绑定 admitted WorkOrder、WorkOrder hash、CapabilityLease、lease hash、
  ConnectorCallSpec/hash、descriptor revision 和 idempotency key；
- transport-only `ConnectorRunnerResponse`：内含 ConnectorInvocation、`ResultEnvelope`、
  `SourceEnvelope` refs 和 quota settlement；`ResultEnvelope` 仍是唯一执行结果权威，adapter 强制
  work/invocation/artifact/usage/status canonical 一致；
- `ConnectorProfileVersion`：exact source/adapter version、allowed operations/hosts、auth mode、
  credential slots、input/output schema refs、pagination/completeness、max response，以及
  access/retention/terms refs；profile 还冻结 redirect、DNS/IP 和 private-network policy；
  CapabilityDescriptor 只负责发现和治理摘要，不承载全部 source runtime 参数；
- `SourceEnvelope`：source、operation、source record/document ref、published/updated/as_of/retrieved
  四类时间、cursor、provider request id、raw response/artifact hash、schema/content hash、
  completeness（enumerated/ranked/partial/unknown）、access/retention/terms policy ref 和 error；
- `ConnectorUsageEntry`：physical calls/pages/records/bytes/duration/billable quantities、metering source
  和 correction chain；
- `ConnectorPriceRateVersion`：meter、unit quantity/price、effective interval、currency 和 source；
- `ConnectorCostEntry`：绑定 exact usage 与 exact rate，保留 actual/estimated/unpriced/waived 和
  correction chain；
- `ConnectorRatePolicyVersion`：quota scope（connector/operation/credential slot/provider shared）、
  burst、并发、window/reset timezone、calls/bytes/records/cost、billable unit 和 Retry-After；执行 timeout
  由 ConnectorProfile/RunnerEnvironment 冻结，不属于 quota policy；
  RatePolicy 只做 admission/quota/retry，可以引用 rate card，不能兼任费率或网络权限权威；
- durable quota reservation/settlement：logical invocation 与 physical provider attempt 分开记；每次
  retry、429、timeout 前都先预占 physical call，结果不确定时保守占额；
- query hash/cursor/time-window 去重、429 reschedule，以及由 append-only event 投影得到的
  source-health/circuit state。外部调用不承诺 exactly-once。

现有 `UsageEntry` 是模型专用契约，强制记录 model/profile/token 字段。它继续作为模型子类型的
用量账；connector 新建独立 Usage/Cost authority，不急于合并成万能 usage 表。现有 ArtifactVersion
的 producer 也通过外键强绑 `ModelInvocation`；P0 发布 ArtifactVersion v0.2，改为引用
`producer_execution_ref`。connector 不能通过伪造 ModelInvocation 取得 raw artifact provenance。

### 通用 connector 路线图

所有 connector 面向数据源和 operation，不面向单家公司：

- **A 股公告**：巨潮/CNINFO 公告检索、下载、正文读取和修订链；万华只是首个 shadow fixture；
- **SEC**：edgartools/findata-analyst 的 filing list、official attachment、item/text/facts；
- **X/xreach**：固定账号完整枚举、单帖和 thread，completeness 可声明 enumerated；
- **X/x_search**：主题发现和媒体理解，输出只能声明 ranked/partial，不能与 xreach 共用 descriptor；
- **Reddit public fetch**：只抽取 last30days 当前可用的公开 Reddit adapter，不把多源 last30days
  聚合器当作 connector identity，也不复用本机已知 403 的 Reddit JSON curl；
- **AlphaEngine**：`search_library → get_document`，凭据只由本地 MCP/credential slot 持有；
- **Guidepoint**：`search_library → transcript`，保留文档 ID、原始 attribution 和许可边界；
- **公开 web search**：Gemini grounded search，只负责 ranked discovery；
- **公开 web fetch**：单独的 last-mile fetch connector，必须拒绝 private IP、非公开 URL 和
  redirect 逃逸，不能与 search 共用权限；
- 后续：港交所公告、A/H/美股行情与财务、雪球、公司 wiki 和其他内部文档源。

聚合 skill（例如 last30days、公司深研）只作为 research recipe 或多个 source connector 的编排层，
不能成为不透明的单一 connector。

### Dalton 自建 connector

第一阶段允许 Dalton 自动发现重复任务或能力缺口、生成现有 `CapabilityProposal(kind=connector)`、
代码、schema、fixtures 和修订版本，但不允许自行激活生产 connector。proposal 的开放
`contract/permissions` object 只能引用闭合、带 hash 的 connector manifest/profile，不能在开放 object
中藏 source runtime 权限：

`gap → proposal → protocol template → offline replay/eval → human canary authorization → trusted canary → canary eval → human production promotion → Catalog`

offline sandbox 不给网络、凭据或 Core DB。在独立 OS/container identity 落地前，自生成代码只能做
offline replay；networked canary 只能运行 operator-reviewed immutable adapter，或者进入独立身份/
container。可信 Connector Runner 不得动态 import 或执行 proposal code。真实 canary 只获得固定
host/operation、短期 credential slot 和独立 quota。运行时还需 exact version/source hash 的 adapter
resolver；MCP tool name、operation、schema epoch 和 skill entrypoint/hash 任一变化，都必须让旧 lease
失效。当前 CapabilityAttestation 明确禁止网络和凭据，真实 canary 必须使用 v0.2 或独立的 closed
canary attestation，不能冒充 offline attestation。未来若要让低风险 connector 自动晋级，必须新增
明确治理 policy，不能绕过当前 human promotion gate。

## 下一阶段顺序

### P0：Connector Protocol 与计量边界

0. 已完成 P0-0：seam 敌对测试、生产数据库副本 startup backfill 演练、复核出处修正、Artifact v0.2
   projection 和 Scheduler attempt event wire/hash epoch；
1. 已完成：`ExecutionInvocation` 超类型、Model/Connector 子类型与 ArtifactVersion v0.2；采用新增表、
   回填 link 和新写入原子双写，不重写历史 model/artifact hash；
2. 已完成 authority contract：ConnectorCallSpec、ConnectorProfileVersion、Runner frames、SourceEnvelope、
   usage、physical attempt 和 rate-policy schema；
3. 已完成 P0-1：trusted store、quota reservation/settlement、幂等、append-only source-health event 和
   最小 `ConnectorIncident` authority（quota drift、schema drift、credential/auth、source outage）；
4. 已完成 P0-2a/P0-2b：Connector Runner 控制面、CapabilityLease use-time gate、exact static adapter
   resolver、authority-derived AdapterRequest、journal/spool/AuthorityPort/recorded transport；
5. 已完成 P0-3 importer thin slice：OpenClaw skill/MCP 只导入 metadata/schema/ref/hash，不导入 prompt、
   凭据或整份 skill；complete scope 内的 MCP/skill 漂移会撤下旧 descriptor 并推动 catalog epoch；
6. 已完成 P0-4a 两笔提交：trusted exporter state、source registration、单调 generation/prior chain、ingest
   event，以及 connector logical/physical usage、quota、health、incident 和 metadata source 的派生只读
   看板。OpenClaw live inventory attach 仍未开放；
7. 已完成 credential-free public HTTPS transport component 的 DNS/IP/pinned socket/TLS/redirect/size
   复核；待接 web fetch adapter/Runner 后才算真实链路；
8. 已冻结 public transport 与 credential authority metadata/API 分界；offline attestation 与 networked
   canary attestation、两次 human gate 仍待完成。同 UID runner 不执行自生成代码，独立身份/container
   上线前只允许 operator-reviewed adapter 进行 canary。

### P1：参考 connector 与 shadow

先实现 A 股公告、SEC、AlphaEngine，再实现 X、Reddit、Guidepoint、web search 和 web fetch。顺序按协议覆盖面，
不是按长期重要性排序：前 3 条分别覆盖公开文件、官方 filing 和 authenticated MCP。每条 connector
都先 shadow，对照现有 skill/MCP 输出，不写 Research Ledger。

### P2：第一条只读研究闭环

offline/authority source-numeric verifier、只读 authority resolver、candidate staging、一条隔离 SEC public
WorkOrder、独立 HumanReviewAuthority、HTML 入口、正式 Evidence/Claim 0.2 promotion 与 ResearchPlan closure
已完成开发候选。
ClaimIndex status 派生、DocumentIndex FTS5、claim/artifact ContextPack materializer、Agenda context authority、
ResearchQuestionBacklog、Planner SEC public 薄闭环、下游逐项 coordinator admission 和真实四步 executor 均已完成
开发候选；隔离 authority 中的一份人批 SEC public 四步任务树已经跑通。Owner 已接受 exact candidate，正式
Evidence/Claim 0.2 promotion 与 Backlog answer binding 已完成；closure coordinator 对全链重验并支持崩溃重放。
真实 policy-authorized 隔离 canary 已完成：closed SEC public plan 自动授权、执行、验证、promotion 并回答原
question，越界 statement 也已在专项测试中 fail closed。Apple、NVIDIA、Walmart 的多样本运行已经用于修复
company-facts filing window 和 latest-accession-bound concept 选择，但结果未形成可独立 replay 的提交证据；
当前已增加第一版
Claim → driver/thesis impact authority，以及 ResearchPlan closure → bounded assessment/verifier WorkOrder 接线。
两个 WorkOrder 现已接入 ModelRouter/OpenClaw model worker，并以无外部调用 recorded broker 验证 contract retry、
usage/cost 入账、model-family independence、lease-expiry crash recovery 和 replay。Gate 0/1 breadth proof 与
Gate 2 真实模型 canary、30-case corpus、候选校准和 wrapper-owned output contract 均已完成。旧的“模型回抄
assessment ref/hash”已从 semantic decision 中移除；trusted worker 从 immutable WorkOrder 绑定 target，仍保留
provider strict Schema、输入/输出/总 token、费用硬控制、raw ResultEnvelope 和历史 replay。Gemini 3.7 Flash low
与 Luna low 在最新 30-case direct calibration 中均为 30/30，Owner 已选择 Gemini 作为主候选；Qwen 和 Ox-alpha
仍有 high miss。仓库内的三项 production conformance 缺口已关闭两项：phase-pinned immutable verifier policy
（`dalton-openclaw-verifier:1`，只允许 exact `profile:gemini-3-7-flash`，未 pin 的 policy fail closed）与
thinking-level 控制合同（WorkOrder/manifest 冻结 `low`，进入 broker request hash、invocation 身份与 host
proof；broker 0.1.0-spike.5）。host 配置和 patch 已经打通；首次 3×30 的 90 次 fresh 调用保留为质量证据，但
旧 runner 复用了 run identity，production gate 已撤销；修正 runner 后续用三个不同 identity 重跑 3×30 并正式
通过。phase-pinned isolated shadow 也通过，production runner 现已部署到 live，但在没有 company→thesis mapping
时保持 idle。ThesisVersion 自动 mutation、旧 cron cutover、Interrupt / park / resume 和 Reflection 仍后置并保持
独立人工 gate。直接解除真实质量缺口或按明确标准改善下一轮产物的 connector/model
增量可以推进；与真实消费者无关的扩建后置。当前没有 live staging/review/plan authority。

operational verifier 与 fixture-only research coordinator 只继续第一条真实闭环需要的部分。formula census/
Model IR ADR 和 offline capability sandbox 只有在首条 plan 明确需要且有验收标准时才恢复，否则等真实闭环与
首轮质量数据完成后再决定。

2026-08-23 owner 明确要求在行业研究前先补建模地基，因此已实现范围受限的 Model Input Ledger v1。它只保存
human-gated actual/scenario/assumption/forecast version、冻结的 model run 和 reconciliation，不实现任意单元格、
VBA、循环引用、通用估值引擎或自动 thesis mutation。研究 worker 只能写 candidate；正式 input 仍由认证人类
准入。Valuation output 在 price/shares/FX/rates/consensus 五类正式 authority 齐备前 fail closed。这里不等于
恢复完整 Model IR 扩建，formula census 和 Tier 1/2/3 evaluator 仍按后置门槛处理。

## 继续建设与开闸的不同门槛

可以立即继续：完成最新 HEAD 的独立 CI；把无网络、无付费调用的完整 replay canary 接入 CI；修复会静默生成
空 evidence block 的 review harness；用同一 revenue-growth plan 复现 5 家 SEC issuer，并生成第一份 verified
brief。真实运行暴露的 verifier/connector 缺口可以修，但必须进入同一个 brief 验收，不能顺手扩建平台。

继续暂停：与真实质量缺口无关的新 connector 品类、Interrupt / Reflection、无明确质量验收的 Model IR、
sandbox、embedding、多 runtime，以及没有真实消费者的 contract、projection 或 dashboard 扩建。

仍需观察或人工批准：扩大 Agenda 公司数、生产 connector 权限、重大或非规则化 Evidence/Claim/Thesis
commit、外发、付费调用、凭据扩权、旧 cron cutover。低风险确定性 Claim 可在 owner 激活的 versioned policy
内自动提交。第一条闭环开发和 Agenda Shadow 数据积累可以并行，其他架构扩建按
v0.7 的价值门槛后置。

## Connector Fabric 完成门槛

### E1：authority 与真实链路

- 旧 ModelInvocation 回填通用 execution link，新 model 写入原子双写且现有模型路径无漂移；
- 至少一条公开 connector 真实跑通 WorkOrder → lease → runner → SourceEnvelope/Artifact →
  ResultEnvelope → Usage/Cost；
- 429 进入 Scheduler retry time，不 busy wait，每次 physical provider attempt 都计量；
- 并发 quota 不超卖；reserve 后本地崩溃可释放，上游是否已调用不确定时标 indeterminate 并保守扣额；
- raw artifact 写入后崩溃可按 content hash reconciliation，不重复产生事实；
- stale lease/source/schema/policy，以及越权 host/operation/credential 全部 fail closed；
- connector shadow 不写 Evidence、Claim、Thesis，也不接入现有 Agenda Perception。

### E2：契约与敌对测试

- closed schema、未知字段拒绝、ExecutionInvocation subtype/equality；
- RunnerResponse 与 ResultEnvelope 的 work/invocation/artifact/usage/status canonical equality；
- SourceEnvelope 四类时间、completeness、access/retention/terms 和 provider request identity；
- SourceEnvelope 的 result attempt、structured content hash 和 raw artifact producer/hash equality；
- logical request 与 physical attempts、quota window/reset/timezone/rounding；
- cursor/page/partial/schema drift、error taxonomy、Retry-After；
- DNS/IP/redirect SSRF、source/adapter/MCP schema hash 变化使旧 lease 失效；
- offline attestation 不能冒充 networked canary evidence。
- trusted runner 对 proposal code 的 dynamic import/exec 必须被源码和运行测试阻止；自生成 adapter 的
  networked canary 必须使用独立 OS/container identity。

### P1：每条 connector 的 shadow gate

- closed profile 和 operation schema；recorded fixtures 覆盖正常、空结果、分页、partial、schema drift
  和 429；
- 每个结果都有 raw artifact、SourceEnvelope 和 exact physical usage；
- enumerated source 在 bounded window 内对 document IDs/revision chain 与旧路径对账；ranked source 不得
  冒充完整枚举；
- authenticated connector 完成 credential revoke 和 permission failure 演练；
- 全程不写 Research Ledger，也不接入当前 Agenda input。

### P2：第一条只读研究 gate

- 一条真实只读 WorkOrder 完成 connector → source/numeric verifier → candidate staging → policy gate；
- closed low-risk plan 和确定性 candidate 可由同一 active versioned policy 授权；人审保留为越界、重大变化和
  verifier 无法自行解决时的升级通道；
- policy accept 或 explicit human accept 都在一个事务内无损写 Evidence/Claim/Relation；reject/revise 不产生
  formal commit；
- retry/revise 有界，失败后不留下 ready/leased 僵尸任务；
- production 未部署前只在隔离 authority 验收自动 Ledger commit，不关闭旧 cron。

硬指标：100% physical attempts 入账；0 fake ModelInvocation；0 未 reservation 的本地 admission；0 超过
本地 hard quota 的 admission；provider-reported overage 必须写 incident 并阻断后续调用；0 secret/Core
path 泄漏；authority idempotency 与数据库 integrity 全部通过。外部计费和 provider quota 可能与本地
状态漂移，不能承诺绝不 overage。

## 当前主要风险

- connector 复用同一 macOS user 时，credential slot 不是 hostile-process sandbox；
- 供应商 quota 与本地计数可能漂移，必须 reservation 后结算并保留 provider-reported 状态；
- 聚合 skill 容易把检索、判断和格式化重新耦合；
- self-generated connector 若缺 recorded fixture、schema drift、429、分页和 partial-result 测试，会在
  正常路径通过、在真实源上失控；
- shadow 通过不等于允许写 Ledger，也不等于可以关闭旧 cron。
- 同一 agent 同时写代码、测试、验证报告和状态文档会形成 self-attestation；最新 HEAD 必须由独立 CI 验证，
  review evidence 为空或采集命令失败时必须 fail closed；
- thesis-impact stack 已有真实 ThesisVersion、30-case no-leakage corpus、wrapper-owned output contract 和多模型
  真实观测；Gemini 3.7 Flash low 已在 direct calibration 30/30，但 production broker 尚不能证明 exact low thinking、
  Google provider controls 和 phase-pinned route，因此仍未达到 live 门槛；
- schema 持续演化但缺少统一迁移纪律；后续任何 schema 改动必须同批提交迁移说明和旧数据 replay/upgrade 测试。

## 相关入口

- 架构蓝图：`docs/reports/vision-and-architecture-v0.1.md`
- 架构裁决：`docs/reports/architecture-debate-and-v0.2-direction.md`
- Core 规格：`SPEC.md`
- Agenda Shadow：`docs/reports/phase-1-agenda-shadow-implementation-2026-08-14.md`
- Agenda 运营与反馈：`docs/reports/phase-1-agenda-control-2026-08-14.md`
- P2 authority resolver 与 SEC canary：`docs/reports/p2-authority-resolver-sec-canary-2026-08-15.md`
- DocumentIndex FTS5：`docs/reports/document-index-fts5-2026-08-15.md`
- ResearchQuestionBacklog：`docs/reports/research-question-backlog-2026-08-15.md`
- Planner SEC public 薄闭环：`docs/reports/research-plan-thin-closure-2026-08-15.md`
- ResearchPlan coordinator：`docs/reports/research-plan-coordinator-admission-2026-08-15.md`
- 当前方向复审与执行计划：`docs/reports/direction-review-and-execution-plan-v0.7-2026-08-21.md`
- 上一版愿景与执行优先级：`docs/reports/vision-and-execution-priority-v0.6-2026-08-15.md`
- Claim → thesis 影响判断：`docs/reports/thesis-impact-verifier-2026-08-20.md`
- ResearchPlan → thesis impact 控制面：`docs/reports/research-plan-thesis-impact-control-2026-08-21.md`
- Thesis impact 模型执行器：`docs/reports/thesis-impact-model-worker-2026-08-21.md`
- Thesis impact 付费边界崩溃恢复：`docs/reports/thesis-impact-model-crash-recovery-2026-08-21.md`
- Gate 2 真实 thesis-impact canary：`docs/reports/gate2-real-thesis-impact-canary-2026-08-21.md`
- Thesis-impact verifier 校准基础：`docs/reports/thesis-impact-verifier-calibration-foundation-2026-08-21.md`
- Thesis-impact verifier 真实校准：`docs/reports/thesis-impact-verifier-live-calibration-2026-08-21.md`
- Thesis-impact 30-case shortlist：`docs/reports/thesis-impact-calibration-v0.2-shortlist-2026-08-22.md`
- Google Generative AI provider controls：`docs/reports/google-generative-ai-provider-controls-2026-08-22.md`
- Wrapper binding 与候选选择：`docs/reports/thesis-impact-verifier-wrapper-selection-2026-08-22.md`
- Phase-pinned verifier policy 与 thinking 控制合同：`docs/reports/verifier-phase-pin-and-thinking-controls-2026-08-22.md`
- 3×30 verifier canary campaign runner：`docs/reports/verifier-canary-campaign-runner-2026-08-22.md`
- 3×30 provider-controlled canary 通过：`docs/reports/verifier-canary-3x30-passed-2026-08-22.md`
- 3×30 canary 独立复核与更正：`docs/reports/verifier-canary-independent-audit-2026-08-22.md`
- GPT-5.6 Sol assessment producer phase pin：`docs/reports/assessment-producer-phase-pin-2026-08-22.md`
- Thesis-impact per-day 预算硬顶与失败告警：`docs/reports/thesis-impact-day-budget-and-alerts-2026-08-22.md`
- Thesis confidence 与 coverage admission ADR：`docs/adr/0001-thesis-confidence-and-coverage-admission.md`
- US IT Services / ACN 初始覆盖准入：`docs/reports/us-it-services-acn-admission-v1-2026-08-23.md`
- Model Input Ledger v1：`docs/reports/model-input-ledger-v1-2026-08-23.md`
- US IT Services Industry Evidence Pack / ACN Overlay v1：`docs/reports/us-it-services-industry-evidence-pack-v1-2026-08-23.md`
- OpenAI Responses provider controls：`docs/reports/openai-responses-provider-controls-2026-08-22.md`
- Connector Fabric 独立复核与更正：`docs/reports/connector-fabric-next-phase-2026-08-14.md`
- Connector P0-1 authority foundation：`docs/reports/connector-p0-1-authority-foundation-2026-08-14.md`
- Context、Memory 与 Log 裁决：`docs/reports/context-memory-log-subsystem-2026-08-14.md`
- Connector Protocol 与自生成模板：`docs/CONNECTOR_PROTOCOL.md`
