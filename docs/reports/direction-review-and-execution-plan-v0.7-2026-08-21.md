# Dalton 方向复审与执行计划 v0.7

日期：2026-08-21
状态：当前执行顺序基线；替代 v0.6 的近期顺序，不反向改写历史报告

## 裁决

Dalton 保持 **Conditional Go**。产品方向没有偏：系统仍要在 owner 设定的 mandate、预算和版本化 policy 内，
自动规划、执行、验证、返工和提交低风险研究；人工只处理越界、连续失败、重大 thesis/估值变化、权限或预算扩大、
交易等异常。

当前问题是建设顺序和证据质量。过去 10 个提交在 `3fd630e..b81d1cb` 之间增加 8,527 行、删除 298 行，
其中 thesis-impact assessment、control 和 model worker 三个模块合计超过 2,100 行。它们的 recorded tests 有价值，
但还没有一条使用真实 ThesisVersion 和真实 brokered model completion 的端到端产物。接下来停止扩建新的
thesis-impact 能力，先把独立验证、同指标多公司复现和第一份可消费产品补齐。

## 对 Fable 复审的采纳与更正

采纳三点：

- 平台深度跑在研究负载前面，thesis-impact stack 需要真实消费者和真实运行；
- 作者、测试、验证报告和状态文档长期由同一 agent 生成，独立验证必须成为下一道硬门；
- 第一产品应是人每周会读的研究产物，不是更多 contract、schema 或 worker。

更正三点：

- 仓库已经有 GitHub Actions。它在 Python 3.11/3.13 上运行全量 `unittest` 和 build，并单独运行 broker
  `npm run check`。截至本次复审，`b030093..69a2ce3` 的 9 个相关 push run 全部成功；`b81d1cb` 的 broker job
  已成功，两个 Python job 仍在运行。因此缺口不是“没有 CI”，而是最新 HEAD 尚未取得完整 green，以及 CI
  没有覆盖完整 research closure → thesis impact 的 hermetic replay canary。
- Apple、NVIDIA、Walmart 已在当前代码上完成过同一 revenue-growth 路径的零人工 closure；Walmart 还暴露并
  修复了 stale concept 问题。因此“只有一家公司、一个 closed plan”不准确。但三家公司结果目前只写在
  `PROJECT_STATUS.md`，没有可提交的结果摘要和 replay bundle，不能当作独立可复现的 breadth proof。
- Core 已有 append-only `ThesisVersion`、claim links 和 commit gate；缺的是可实际操作的 thesis
  admission/revision/retirement 工作流，以及一条真实 thesis-impact canary。不能把“没有完整工作流”写成
  “没有 thesis substrate”。

Fable 的 review evidence 注入脚本确实失败：两个应包含 vision/status 和 implementation/tests 的区块只有
`zsh: parse error near 'done'`。本次裁决已经直接重读仓库文档、源代码清单、commit diffstat、CI 配置和远端
Actions 状态；以后证据采集命令只要失败或产生空区块，review 必须 fail closed。

## 已决定的三个问题

### 1. Breadth 先于继续加深 thesis-impact

这是硬顺序。现有 thesis-impact 代码可以保留并用于后续 canary，但在多公司复现 gate 完成前，不增加新的
thesis updater、并发 worker、自动 thesis revision 或 fleet control。真实模型 canary 不与新的功能扩建并行。

### 2. 开发过程也要受 gate 约束

研究自治和开发流程不需要共用同一套业务 authority，但 main 上的开发批次必须满足：

- 一个批次只交付一个有明确验收条件的 slice；不以行数作为硬上限，避免用拆 commit 绕过 review；
- 前一批 HEAD 的独立 CI 未完成前，不启动下一批 capability code；
- contract、模块、测试和验证记录同批交付；验证记录必须引用独立 runner，不能只写本机自报结果；
- 任何 schema 变化同批提交迁移说明和旧数据 replay/upgrade 测试；
- review evidence 为空、采集命令失败或关键检查未运行时，状态只能写 blocked/pending，不能写 verified。

### 3. 第一产品是每周 5 家公司的验证简报

首个可消费产物定为：**5 家 SEC issuer 的 verified quarterly revenue growth + thesis-impact brief**。每家公司至少
包含 accession、财务期间、current/prior 数值和单位、选用 concept、同比增速、verifier 状态、对应 thesis/driver
影响或 `insufficient`、实际模型成本。首轮允许 thesis-impact 栏为空并明确写 `not yet run`；不能用模型摘要替代
正式 Claim 或来源。

## 执行顺序

### Gate 0：还清 runtime evidence 债

范围：只补验证和 review hygiene，不加新能力。

- 等待并记录 `b81d1cb` 的 Python 3.11、Python 3.13、broker 三个 CI job 最终结果；
- 把一条无网络、无付费模型的 hermetic replay canary 接入 CI，覆盖
  `recorded SEC input → formal Claim closure → recorded thesis assessment/verifier → replay`；
- 提供可重复的 review evidence collector；任何命令非零退出、目标文件不存在或 evidence block 为空时，
  collector 非零退出；
- 记录本机全量 Python、broker、build 和 CI run URL，不能用专项测试代替全量。

完成门槛：最新 HEAD 的三个 CI job 全绿；hermetic replay 在独立 runner 通过；review harness 能嵌入非空文档和
实现证据，并能在故障注入时 fail closed。

### Gate 1：同指标 5 公司复现并产出 brief v0

范围：只参数化已有 revenue-growth plan，不加 connector、不加指标、不改 thesis-impact stack。

- 选择 5 家能覆盖不同 SEC concept/fact pattern 的 issuer；
- 在同一 commit 上完成 5 条正式 closure，保存脱敏结果摘要、authority hash、CI/replay 命令和失败分类；
- 至少保留 1 个 stale/missing/ambiguous concept 的 fail-closed 样本；
- 生成第一份 5 公司简报。没有可用 ThesisVersion 的公司明确写 `insufficient`，不临时编造 thesis。

完成门槛：5/5 plan 可从相同入口运行；成功样本的数字、期间、accession 和 concept 可回指；失败样本不生成
Evidence/Claim；零 schema 变化，或把确实无法绕开的 gap 单列为下一 slice，而不是顺手扩 schema。

### Gate 2：一条真实 thesis-impact canary

范围：只让现有 stack 服务一条真实 ThesisVersion。

- 优先选现有 governed ThesisVersion；只有确实没有合格对象时，才补最小 human-gated thesis admission 和 revision
  history，不建设通用 fleet lifecycle；
- 取得 owner 对具体付费调用和金额上限的单独授权后，设置本次 run 的 hard total spend cap；
- producer 和 verifier 使用不同 model family，保存真实 invocation、usage、cost 和正式 verification；
- 在 provider 已返回并入账、Scheduler 尚未 complete 的位置杀掉 worker 一次，验证 `b81d1cb` 的 durable replay；
- canary 不修改 Thesis current pointer。

完成门槛：1 条 persisted verified assessment；成本不超过授权 cap；恢复后 provider call、usage 和 cost 不重复；
输出能进入 5 公司 brief，或以明确错误说明为什么不能进入。

### Gate 3：预算、告警与定时运行

在任何自动定时运行前增加 per-plan/per-day hard budget 和失败告警。故障注入必须证明超预算会停止并留下 decision，
canary 失败会通知 owner。dashboard 和路由优化继续后置。

### Gate 4：第二 connector

首选只读 earnings-call transcript。第一条验收只做一份 cross-source ContextPack，把 SEC 数字与 transcript claim
放在同一正式研究链；不接付费源、不开放写能力、不同时扩第三个 connector。

## 继续冻结

- 自动 thesis revision、multi-thesis fleet、worker parallelism；
- 新 connector 品类、Model IR、embedding、Interrupt/Reflection、第二 runtime；
- live policy activation、旧 cron cutover、生产 credential、外发和交易动作；
- 只增加架构完整度、没有进入 5 公司 brief 的 contract、schema、projection 或 dashboard。

## 当前验证状态

- 前一开发 HEAD `b81d1cb` 的
  [Actions run 32458335552](https://github.com/everflowinv/dalton-research-agent-os/actions/runs/32458335552)
  已结束，Python 3.11、Python 3.13、openclaw-broker 三个 job 全部成功；
- Gate 0 本机候选：Python 全量 581/581、broker 16/16、显式 hermetic replay 1/1、review collector
  故障注入 8/8、build、compileall 和 `git diff --check` 全部通过；实际 review manifest 已生成非空证据包；
- Gate 0 只有在 exact commit 的 Python 3.11、Python 3.13、openclaw-broker 三个远端 job 和独立 canary step
  全部成功后才完成。本段不在 push 前预写远端成功；exact run URL 由 GitHub checks 和本轮交付记录；
- 未调用真实或付费模型，未访问新的真实 source，未部署 live，未修改 cron。
