# AlphaEngine TranscriptPolish 真实 canary v0.9

日期：2026-08-24  
状态：真实 canary 已通过；继续 shadow，暂不启用 production policy

## 结论

AlphaEngine 登录恢复后，隔离 canary 完成了真实文档读取、人工 targeted correction review、GPT-5.6 Terra 生成、
Core conservation/source-lineage gate 和 Claim citation binding dry run。最终产物通过硬门，未写正式 Evidence、Claim
或 Thesis，也没有修改 production pointer。

本轮允许进入受控 shadow，但不启用 production。原因是目前只有一份真实逐字稿通过，而且前两次 canary 分别暴露了
Scheduler lease 恢复缺口和句点粘连词的假阳性保护。修复后需要再用一份结构不同的真实逐字稿验证，才能排除规则只适配
当前样本。

## 真实输入与结果

- 来源：AlphaEngine `Flowers Foods Q2 2026 (Q&A)`，document id `130000108112113`；
- 原文：17,703 字，单次 `get_document` 完整取得，manifest 与 assembled object 均有 SHA-256 绑定；
- correction review：人工标记 1 个疑似 ASR 术语，但没有音频或官方逐字稿支持，因此保持 unresolved；
- Terra：development policy v2、`profile:gpt-5-6-terra`、宿主 `thinking=xhigh`；
- 实际模型调用：1 次；Scheduler attempt 2 次，其中第 2 次只读取 broker durable replay；
- provider usage：输入 5,344 tokens，输出 8,594 tokens，实际成本 USD 0.113816；
- polished artifact：17,885 字，source/polished 比例 1.010281；数字、限定词、专名顺序和 unresolved 术语均通过 Core；
- Claim binding dry run：clean raw span 为 `claim_eligible=true`，引用模式为 `raw_span`；
- 正式 authority 写入：Evidence 0、Claim 0、Thesis 0。

仓库只保存 hash、authority ref、usage 和 gate 结果。原始逐字稿与模型输出仍留在 owner-only 临时目录，没有提交。

## canary 暴露并修复的问题

第一次真实模型返回耗时约 119 秒，超过 Scheduler 默认 30 秒 lease。Core 正确拒绝 late completion，但 worker 抛出异常，
没有进入已有的 broker replay 恢复路径。现在 worker 在模型返回后、写 polished artifact 前重新校验 lease；过期时只推进 bounded
retry，下一 attempt 使用 `replayOnly` 读取同一 invocation，不再次调用模型。回归测试同时确认 late result 不会留下孤儿 artifact。

第二个问题来自 protection gate。旧自动专名规则把 `CFO.Ryals`、`Q4.It` 这类 ASR 句点粘连误判为不可拆分专名；模型只加
空格也会被拒绝。规则 v0.3 不再把句点吸进 mixed-case/alphanumeric token，但仍分别保护 acronym、姓名、数字、显式专名和
人工追加词。离线复核显示 v3 候选在排除这类假阳性后，其余所有 conservation checks 均通过；v4 随后完成新的端到端验证。

canary summary 原先按两次 Scheduler attempt 累加同一 cost entry，导致 replay 成本重复。汇总现按 immutable cost entry ref 去重，
数据库只有 1 条 actual cost record，金额为 USD 0.113816。

## 失败记录与成本

- v2：USD 0.085264；模型结果返回后 lease 已过期，未形成 artifact；
- v3：USD 0.138558；Core 拒绝句点粘连假专名变化，未形成 artifact；
- v4：USD 0.113816；完整硬门通过；
- 三次真实模型调用合计 USD 0.337638。

失败 run 没有混入最终结果，也没有被改写成通过。

## Production 决定

当前决定是 `no-go pending second independent shadow canary`：

- development policy v2 继续固定 Terra；
- production pointer 不启用；
- 下一次 shadow canary 应选另一家公司、不同 speaker/标点结构的完整逐字稿；
- 第二份样本必须继续经过人工 correction review，并保持正式 Evidence/Claim/Thesis 写入为 0；
- 通过后再单独审核 production worker 的 lease policy、预算和部署范围。

机器可读证据：`docs/review-evidence/alphaengine-transcript-polish-live-canary-summary-2026-08-24.json`。
