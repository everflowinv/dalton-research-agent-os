# LLM Research Planner 与模型选择 v0.1

日期：2026-08-23  
状态：development candidate；未部署 live production

## 裁决

开放式研究意图需要 LLM 做语义解释，但 LLM 只提交弱候选，不能直接改变研究权限或事实层。
Dalton 用版本化 authority 和 Core gate 跨越自然语言与写死架构之间的差距：

1. 人类把长期方法、行业重点、公司假设和研究中途修正分别写入 Doctrine、Driver Pack、Thesis 和
   ResearchDirective；每个对象都有 exact ref/hash 和生效范围。
2. 每轮 Planner 只读取冻结的 PlannerContextPack，从 Core 已准入的 probe catalog 中选择一个下一步，或提交一个
   closed-enum 终止候选。
3. Core 重新验证 ContextPack、catalog binding、coverage、预算、轮次和 terminal prerequisites，随后才生成正式
   PlannerProposalVersion。模型不能输出模板、参数、权限、预算、source、authority hash 或 Claim。
4. Worker 只执行 WorkOrder，Verifier 只形成来源级 ResearchOutcome。一次 `not_found` 或 source unavailable 都不能
   变成“不存在”。
5. 人类中途修正只从下一轮开始生效；不修改已经运行的 PlanRound，因而保留 replay。

这保留了原架构中的硬边界，同时让“多关注供需”“看股东回报”“检查并购 track record”这类开放式要求真正影响
下一步研究路线。Doctrine 是研究 lens，不是证据；Evidence/Claim 仍必须走独立准入和验证。

## 本次实现

- 新增 `LLMPlannerCandidate 0.1`：只允许一个 `probe` 或一个 closed terminal action。
- 新增 `PlannerProposalVersion 0.3`：在 0.2 的 deterministic provenance 之外支持 exact model invocation/result
  provenance，旧版本保持可重放。
- 新增 LLM Planner worker：固定 prompt wrapper、quoted ContextPack、strict JSON、exact profile route、replay-first
  crash recovery、用量和成本核算、有限重试。
- Core 对模型候选执行第二次校验：stale ContextPack、catalog/参数/权限/预算漂移、重复 terminal coverage item、
  不满足 coverage 的负面终止都会 fail closed。
- `request_replan`、`deprioritize`、coverage complete 和全局预算耗尽由 Core 确定性处理，不依赖模型服从。
- 保留 deterministic checklist planner，既是 fallback，也是离线 reference implementation。
- 新增 frozen calibration corpus 0.1：15 个 case，其中 10 个 safety-critical；覆盖供需、股东回报、并购记录、
  人类 correction、单一来源 miss、source unavailable、prompt injection、lens 越 catalog 等失败模式。
- 新增 development-only phase policy，固定 `profile:qwen3-8-max`；没有创建或启用 production Planner policy。

## 模型横评

同一 corpus、同一 prompt、同一 candidate contract、同一 Core scorer 顺序执行。硬门槛是：15 个输出全部符合 schema，
且 10 个 safety-critical case 全部命中；成本和延迟只在通过硬门槛后比较。本结果只针对 Dalton 的短 Planner 任务，
不是通用模型能力排名。

| 模型 | Action | Safety | Hard gate | 成本（USD） | 单 case 中位延迟 | 裁决 |
|---|---:|---:|---|---:|---:|---|
| Qwen 3.8 Max | 15/15 | 10/10 | pass | 0.05034645 | 2.286s | 入选 |
| GPT-5.6 Terra | 15/15 | 10/10 | pass | 0.057400 | 3.087s | 首选替代候选 |
| GPT-5.6 Sol | 15/15 | 10/10 | pass | 0.111292 | 3.349s | 通过，不入选 |
| Claude Opus 5 | 15/15 | 10/10 | pass | 0.789925 | 9.529s | 通过，不入选 |
| Claude Fable 5 | 15/15 | 10/10 | pass | 1.58120 | 9.257s | 通过，不入选 |
| Gemini 3.1 Pro Preview | 13/15 | 9/10 | fail | 0.109386 | 3.832s | 淘汰 |

Gemini 两次都选择了正确 action，但把 JSON 包在 Markdown fence 中；strict parser 按合同拒绝。其中一次是
safety-critical 的 source-unavailable case，因此不能用宽松解析掩盖失败。

Qwen 与 Terra 随后各自独立再跑完整 15-case corpus，两者均再次 15/15、safety 10/10。Qwen 第二轮成本为
USD 0.02860110，Terra 为 USD 0.057412。Qwen 因两轮 30/30、低延迟和低成本入选 development Planner。

## 校准异常与实际花费

- 第一版 prompt 把 probe/terminate 示例放在 `action` 数组里，GPT-5.6 Sol 前 5 次复制了数组。5 次调用共
  USD 0.035804，已停止、修 prompt 并排除出横评。
- Claude tokenizer 对同一 ContextPack 上报约 9.2k input tokens；初始 8k WorkOrder 上限让 Core 正确拒绝 4 次
  Fable 结果，共 USD 0.42456。正式 Claude run 显式使用 12k input 上限，研究轮次和 cost gate 没有放宽。
- 包含两次 smoke、上述诊断调用、6 个正式首轮和 Qwen/Terra 复测，本轮模型调用合计
  USD 3.26133855。

## 验证

- LLM Planner 首次实现后：全仓 `746/746` 通过。
- prompt schema 修复：专项 `32/32` 通过。
- subset scorer 修复及 development policy：新增专项均通过。
- `compileall`、`git diff --check` 通过。
- `.venv/bin/python -m build` 已生成 sdist/wheel，并确认 wheel 包含新 modules 与 frozen corpus。
- 系统 `python3 -m build` 因本机 `build.__main__` 环境问题失败；改用仓库 `.venv` 后构建成功。这不是源码测试失败。

## 边界与下一步

本次没有部署 live worker、没有修改 production routing、没有对外写入 Claim，也没有让模型自行创建 probe。
下一笔开发应做 StatementSnapshot v1：一个 accession 的原始 statement authority 只取一次，按版本化 concept set
生成 Decimal fact rows 与勾稽结果，再接入同一 Planner loop。TranscriptPolishWorker 排在其后。
