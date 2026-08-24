# TranscriptPolish Terra policy 与 corpus v0.2

日期：2026-08-24  
状态：Terra development policy 与 corpus v0.2 已完成；真实 AlphaEngine canary 被上游登录态变化阻塞；未启用 production

## 结论

Owner 决定 TranscriptPolish 优先使用 GPT-5.6 Terra。Core 已新增 immutable development policy v2，精确固定
`profile:gpt-5-6-terra`；原 Gemini 3.7 Flash policy v1 保留为历史 lineage，没有被改写。Planner 继续使用
Qwen DeepSeek V4 Flash，未受本次决定影响。

Terra 在新的 transcript corpus v0.2 上取得 12/12、safety 11/11，contract、conservation 和 quality 三道 gate
全部通过。中位延迟 4.142 秒，正式记账成本 USD 0.037506。

## Owner override 与 policy lineage

上一轮全模型横评按速度、成本和 speaker 结构保真选择了 Gemini 3.7 Flash。Terra 当时也连续两轮 10/10、safety
9/9，只是延迟和成本更高。此次变更是 owner 对任务模型的明确选择，不应改写成“Terra 在原 benchmark 击败 Gemini”。

- v1：`model-routing-policy-version:dalton-openclaw-transcript-polish-development:1`，固定 Gemini 3.7 Flash；
- v2：`model-routing-policy-version:dalton-openclaw-transcript-polish-development:2`，prior ref 精确指向 v1，固定
  GPT-5.6 Terra；
- 两个 policy 都只属于 development catalog，不启动 live worker，不修改 production pointer。

OpenClaw broker 的 Terra profile 已在 disk config 显式固定 `thinkingLevel=xhigh`，不再依赖全局 thinking 默认值；
config validation 通过。该宿主配置需要 safe gateway restart 后才进入新进程。

## Corpus v0.2

v0.1 的 10 case 和 hash 保持可重放。v0.2 以带 hash 的 overlay 追加约束，resolved corpus hash 为
`daf20ea81e405567c42f940fdb7f469b08b936a92086c867012430863cd983eb`，共 12 case、11 个 safety-critical：

- 把 unresolved ASR case 中的通用 `Speaker` 标签列为 protected term 和 required content；
- 新增疑似专名错转 case：`Guide Point` 在没有核准 correction 时必须原样保留，禁止模型擅改为 `Guidepoint`；
- 新增疑似数字错转 case：`40 million dollars` 在没有音频或官方逐字稿时必须原样保留，禁止模型猜成
  `14 million dollars`；
- unresolved correction 由纯字符串升级为 `{term, correction_kind}`，可区分 proper name、numeric、negation、
  semantic 和 speaker label 等风险；
- v0.1 的 unresolved term 在重放时仍按 proper-name 语义解释，旧 corpus 和旧 run manifest 不变。

Terra 对 v0.2 的第一笔 run 返回 11/12；失败 case 实际没有数字或语义漂移，只把句首 `no` 正常大写成 `No`。
生产 conservation gate 已通过，问题来自 quality rule 使用大小写敏感的 `no verified audio span`。规则改成仍能表达
原义、但不把正常句首大写当错误的 `verified audio span` 后，corpus 获得新 hash，并在 clean commit 上完整重跑。
最终 12/12 结果只绑定新 hash；旧 run 不混入最终成绩。两笔 Terra v0.2 run 的实际模型成本合计 USD 0.075012。

## AlphaEngine canary 状态

本机 AlphaEngine MCP health 显示 Desktop 在线且 `authenticated=true`，但实际 `search_library` 和已知 document id 的
`get_document` 都在取得任何正文前返回：

`E_UPSTREAM_API: 用户状态发生变更，刷新 token`

因此本轮没有拿到完整 source manifest，没有把本地旧文件或 recorded fixture 冒充 live canary，也没有继续调用 Terra、
生成 polished artifact 或写 Claim citation binding。恢复条件是 AlphaEngine Desktop 刷新登录态后，再按原计划执行：

`完整文档 acquisition → human correction review → Terra → Core conservation/source-lineage gate → Claim binding dry run`

即使模型与保真 gate 通过，没有 human correction review 时，canary 也只能停在 Claim admission 前，不能把 raw ASR 当作
天然正确的事实来源。

## 验证与边界

- policy v2 和 v1 lineage 专项通过；
- corpus v0.1 可重放，v0.2 本地 gold 12/12、safety 11/11；
- Terra paid run 12/12、safety 11/11，完整 usage/cost 有正式 provider telemetry；
- 模型原文和逐 case record 只保存在 owner-only calibration 目录，仓库仅提交汇总证据；
- 本轮未启用 production routing，未写 live Evidence、Claim、correction 或 Thesis。
