# TranscriptPolish 全模型横评 v0.7

日期：2026-08-24  
状态：development model 已选定；未启用 production routing

## 结论

TranscriptPolish 的 development-only route 固定为 `profile:gemini-3-7-flash`，thinking 固定为 `low`。
Planner 仍使用 `profile:qwen-deepseek-v4-flash-0731`；逐字稿任务没有沿用 Planner 的模型结论。

Gemini 3.7 Flash 在两轮完整测试中均为 10/10、safety 9/9，中位延迟分别为 2.027 秒和
1.885 秒，正式记账成本分别为 USD 0.01465575 和 USD 0.01416450。人工复核还确认它保留了
unresolved ASR case 中的通用 `Speaker:` 标签，没有把未核准的错词改成自认为正确的内容。

## 运行口径

- 范围：Dalton broker 当前准入的 25 个 exact profile，覆盖 24 条 distinct model route；不是只挑已知强模型。
- Corpus：`transcript-polish-calibration-corpus:0.1`，hash
  `1fc70068eca00bbb980ffa6c81790f5fd5ef7b0b00ba48dd9bf4f30f08298346`。
- 题目：10 个 exact case，其中 9 个 safety-critical；覆盖 prompt injection、数字与单位、否定与不确定性、
  专名、speaker、中文、未解决 ASR 错词、human-admitted correction 和多 span 长文本。
- 约束：12,000 input-token cap、4,000 output-token cap、180 秒单 case wall-clock cap。
- 主矩阵和复测均绑定 commit `7e6e543a1b14c013f33cd10bcbecb615721f07fc`。
- Scorer 依次检查 strict JSON contract、生产 conservation verifier 和最小可读性规则。模型输出即使读起来通顺，
  少一个数字、否定词、专名或 source span 也会失败。
- 金额只在 provider telemetry 可用时算正式记账成本。失败调用显示的 hard-cap reserve 不是实际花费。

## 首轮结果

首轮完整 10/10、safety 9/9 的模型共有 8 个：

- Gemini 3.7 Flash：中位延迟 2.027 秒，USD 0.01465575；
- GLM 5.2：2.294 秒，USD 0.011629434；
- Qwen 3.8 Max：2.645 秒，USD 0.02225190；
- GPT-5.6 Terra：3.901 秒，USD 0.032062；
- GPT-5.5：4.246 秒，USD 0.080155；
- Gemini 3.1 Pro Preview：6.114 秒，USD 0.087440；
- Grok 4.3：6.106 秒，USD 0.03200475；
- Grok 4.6：13.247 秒，USD 0.093058。

其余值得记录的结果：

- GLM 5.3 `high`：8/10、safety 7/9，中位延迟 7.968 秒。它漏掉 prompt-injection case 的必保内容，
  长 case 又触发 conservation failure，不能入选。
- GPT-5.6 Luna：本轮 9/10、safety 8/9；结合之前的 9/10 和 10/10，仍有长文本稳定性问题。
- Qwen DeepSeek V4 Flash `xhigh`：9/10；长 case 产出恰好耗尽 4,000 tokens 后正文为空。
  按规则唯一一次 `low` recovery 仍为 0/1，因此不能用于 TranscriptPolish。
- Direct DeepSeek V4 Flash：8/10；Direct DeepSeek V4 Pro：5/10。两者都没有通过安全门。
- GPT-5.6 Sol：9/10；Gemini 3.5 Flash Lite：9/10；均有 safety-critical failure。
- Ox Alpha：8/10，且两次 host/contract failure 使用 reserve；stealth route 的身份与价格也不稳定。
- Claude Fable 5 前 7 题全过，但第 8 题超过 180 秒，coverage 不完整；Claude Opus 5 第一题即超时；
  Claude Sonnet 5 为 0/10 contract pass。
- Gemini Flash Latest 为 0/10，全部 contract failure；Grok 4.20 两个 beta route 和 Grok Build 也未过安全门。

完整逐 profile 汇总见
`docs/review-evidence/transcript-polish-model-matrix-summary-2026-08-24.json`。模型原文、逐 case usage 和 runner
manifest 保存在 owner-only calibration 目录，不提交仓库。

## 稳定性复测与选择

对低延迟且首轮全过的四个候选独立再跑一轮：

- Gemini 3.7 Flash：10/10、safety 9/9，1.885 秒，USD 0.01416450；
- GLM 5.2：10/10、safety 9/9，2.143 秒，USD 0.011621732；
- Qwen 3.8 Max：10/10、safety 9/9，2.461 秒，USD 0.02077350；
- GPT-5.6 Terra：10/10、safety 9/9，4.269 秒，USD 0.032062。

四个候选都满足两轮全过。GLM 5.2 虽然略便宜，但人工复核发现它在 unresolved ASR case 中删除了通用
`Speaker:` 标签；v0.1 corpus 尚未把该标签列为 protected term，因此二元分数没有抓到这个差异。Gemini 3.7 Flash
保留标签，同时比 GLM 5.2、Qwen 3.8 Max 和 Terra 更快，成本也低于 Qwen 与 Terra。因此选 Gemini 3.7 Flash，
不因 GLM 5.2 的小幅价格优势牺牲 speaker 结构保真。

仓库新增 immutable development policy
`model-routing-policy-version:dalton-openclaw-transcript-polish-development:1`，只允许
`profile:gemini-3-7-flash`。该 policy 只登记开发期选择，不启动 worker，也不修改 live production pointer。

## 下一步

1. 把 corpus 升到 v0.2，将通用 speaker 标签和更贴近 AlphaEngine 的 ASR 错词列为显式保护项；
2. 运行一份完整 AlphaEngine transcript canary：完整原稿 → 可选 human-admitted correction → Gemini 3.7 Flash
   `low` → Core conservation/source-lineage gate → Claim citation binding dry run；
3. canary 通过后再单独提交 production policy 与启用决定，不把开发期 policy 自动切成 live。

## 边界

本轮没有写 live Core、Evidence、Claim、correction 或 Thesis，没有访问真实 AlphaEngine 文档或音频，也没有启用
production TranscriptPolish worker。原始 ASR 仍是不改写的捕获记录，不是事实真值；正式 Claim 只能引用 raw coordinates
加 exact admitted correction lineage，polished 文本不构成第二份证据。
