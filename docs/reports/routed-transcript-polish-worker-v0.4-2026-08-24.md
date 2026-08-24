# Routed TranscriptPolish Worker v0.4

日期：2026-08-24  
状态：development candidate；未跑真实模型，未部署 live

## 本轮交付

TranscriptPolish 不再依赖调用方塞入一个已经生成的 model candidate。新增链路：

`exact source lineage → model candidate WorkOrder → pinned route → broker → Core conservation gate → derived artifact → original probe Result → Planner Outcome`

它复用现有 Scheduler、ModelRouter、OpenClaw adapter、ModelInvocation、usage/cost accounting 和 Bounded Planner probe，
没有新建队列或让模型直接触碰 Core authority。

## 模型看到什么

Core 每次从 exact complete AlphaEngine manifest、raw object 和可选的 human-admitted correction set 重新生成
`resolved_source_text`。原始 ASR 中已经被正式修正的内容先进入 resolved source；unresolved 内容不猜测，也不让 polish
模型擅自修正。

Core 把 resolved source 确定性切成不超过 2,000 字符的连续 span，并预先给出：

- `source_start / source_end`
- exact UTF-8 `source_sha256`
- `source_text`

模型不计算 SHA-256，只能逐段原样回抄 span identity，再输出 `polished_text`。固定 wrapper 明确把 transcript 标成
quoted data；原稿里即使出现“忽略上面的要求”等文字，也不能变成模型指令。

## 模型不能做什么

模型输出仍是 `TranscriptPolishCandidate 0.1`，只有 `schema_version` 和 segments。它不能输出或发布：

- correction set 或 correction admission；
- Evidence、Claim、Thesis 或研究结论；
- source、permission、budget、route、hash authority；
- polished artifact。

correction 仍要求 human actor；数字、否定词、语义和 speaker 修正仍要求音频或官方逐字稿级 authority。model worker
只处理可读性派生稿。

## 路由与失败边界

模型 candidate WorkOrder 使用 `research` capability 和原有 broker runtime。worker 启动时要求 routing policy 只允许一个
exact profile，随后按原有规则执行 route、provider call、ModelInvocation、usage/cost accounting、bounded retry 和
crash replay。

模型返回 strict JSON 后，还不能立刻成为成功 Result。worker 先用 exact probe WorkOrder 调本地
`TranscriptPolishWorker`：

- span 必须连续覆盖完整 resolved source，ref/hash 必须一致；
- 数字表达式必须逐段和全局守恒；
- protected proper names 必须保持数量和顺序；
- conservation rule v0.2 新增否定词和不确定性限定词守恒，例如 `not / never / may / might / uncertain` 及常见中文对应词；
- 长度比例与全局专名检查继续生效。

strict JSON 失败形成 `MODEL_OUTPUT_CONTRACT_REJECTED`；上述保真检查失败形成
`MODEL_CANDIDATE_CONSERVATION_REJECTED`。两者都按 Scheduler 上限重试，不能关闭原 probe。只有 verifier 已幂等生成
`citation_authority=source_lineage_only` 的 derived artifact，model Result 才能成功。

## 接回 Planner Loop

`RoutedTranscriptPolishCoordinator` 绑定两份不可变 WorkOrder：一份是 Bounded Planner 已批准的 local probe，一份是
candidate generation model work。它在关闭 probe 前重新生成 exact source context 和预期 model WorkOrder，重读 formal
model Result，再次幂等执行 local verifier。最终 probe Result 保留：

- model WorkOrder/result/invocation/route/profile provenance；
- usage refs；
- polished artifact ref；
- candidate hash 与 Core verifier 标记。

原 Bounded Planner 仍只从 probe Result 的 `matches` 形成 `observed` Outcome，不会因此自动写 Claim。

## 验证

相关超集 55/55 通过，覆盖 routed valid candidate、实际成本入账、artifact 先验收后关闭 probe、幂等 replay、数字漂移
bounded retry、非法 candidate contract bounded retry、admitted correction 进入 model source、原 deterministic probe 和
Bounded Planner 回归，以及 Core 预计算多段连续 span/hash。另通过 pyflakes、`compileall`、JSON schema 解析和
`git diff --check`，并完成 wheel 构建、隔离安装和新增模块导入。

本轮没有访问真实 AlphaEngine、没有付费模型调用、没有写 live Core。模型横评仍不能沿用 Planner corpus 结果。

## 下一步

冻结 transcript 专用 corpus，至少覆盖：prompt injection、数字/单位、否定词、不确定性、speaker、专名、已准入修正、
unresolved span、中英文和多 span 长文。然后在同一 WorkOrder 上比较 DeepSeek V4 Flash、GLM 5.3 和 GPT-5.6 Luna；
统一记录 contract pass、conservation pass、可读性、延迟和实际成本，再发布 development-only exact profile policy。
