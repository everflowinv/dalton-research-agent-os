# ADR-0019：按研究环节显式声明模型思考强度

日期：2026-10-10。状态：已实现并部署；实际产物完成情况与长期观察单独验收。

## 背景

`research-program-remaining-batches-2026-10-09.md` 批次 aw 更正了早期判断：
Dalton 普通 broker 调用此前未声明思考强度，但观测实际大多已是 low/minimal，
不能把 OpenClaw 交互 agent 的 `agents.defaults.thinkingDefault=xhigh` 当作 broker 默认。
本变更明确调用合同，不修改 owner 的网关全局设置。

## 决定

每个 Dalton broker 请求都有 `controls.thinkingLevel`。允许 off/minimal/low/medium/high；
没有用途或用途没有特别规则时为 low。`initial_screen`、`investment_memo`、
`deep_insight_gate`、`industry_framework` 的起草默认 medium。
独立核验始终 low；通用控件、核验控件、冻结的 WorkOrder 或 route 声明冲突时拒绝。
`requiredControls` 保留供应商侧结构化输出、输入与成本限制，不能通过通用控件绕开。

medium 是相对历史实际 low/minimal 的提高，理由是这四类步骤负责综合材料、形成观点。
真实质量对照尚不能证明 medium 普遍更好；两档严格合同与质量评分分别记录。

Owner 可在 AI 服务页按用途调整，必须给出理由。变更作为路由政策的新版本追加，
`thinking_level_overrides` 冻结 level/reason/actor_ref/created_at；旧政策与其 hash 不变。
恢复旧强度也追加新版本。原模型链保留。所有注册配置重新指向新政策，
常驻规划器刷新配置；Cockpit 把同一 owner 操作发给其余 active 环境的 writer。
失败或未运行的环境逐个出现在同步回执中，不伪报完成。

Route、invocation usage/result metadata 和每类实际费用准入记录保存 `thinking_level`。
历史费用 wire 不回填；旧用途可从 immutable chain link 找回，旧强度仍明确为未记录。
预算与费用页按最近一次 owner 变更时间对比各环节前后的次数、平均费用、平均耗时。
没有变更记录时区分历史未声明与新显式声明；耗时是准入到结算，包含排队。
未结算费用继续按预留额统计。

## Broker 与模型级参数优先级

本仓库 broker 原协议只有 `requiredControls`，不能用它给普通起草请求传 medium：
它要求 provider-controlled profile 且和 profile 的核验设置一致。
新增通用 `controls` 是独立、闭合、经过 HMAC 与幂等 hash 的字段。
Broker 将请求 level 放在 `runtime.llm.complete`，优先于 profile.thinkingLevel；
合成测试覆盖五种值、配置 high 时请求 low/medium 的优先级、冲突与不同强度的幂等冲突。
Host 没有声明支持该 level 时，在发送前拒绝，不依赖未知隐式行为。

宿主离线最终 payload 检查确认请求级控件优先于模型固定参数，另发现 DeepSeek descriptor 的能力声明缺口。专属 broker runtime 补齐能力，不修改交互 agent 的全局默认。请求控件、宿主验证通过和原始供应商 payload 是不同层的证明；正式结果目前不持久化 providerControlProof，不能把请求 metadata 冒充原始最后一跳证据。

muse 的 low 与 minimal 是不同的声明值。本计划已有观测显示 OpenClaw 会将 muse 的
off 映射为 minimal；这不证明 low 等于 minimal。Dalton 明确请求 low，不把它改成 off，
费用记录保存请求值。验收应同时核对请求 low、provider 最终 effort 和推理 token；
如果模型兼容映射将 low 转换为 minimal，应同时保留这两层证据，不能把请求值改写为实际值。

## 验收与兼容边界

Adapter 与新 broker 插件必须一起发布；旧 broker 的闭合协议会拒绝 controls。
两者已协调发布；来源和运行观察收据私有保留。旧政策 hash 保留，新 route 新增可选 thinking 字段，
adapter 仍读取旧 route，并为新 broker 请求补显式声明。

离线测试通过默认 low、四个 medium 默认、owner low 覆盖、新 policy 追加与旧 hash、
跨环境 writer fan-out、费用前后划分、真实 Node UDS/HMAC 合成协议和现有黄金样本合同。
黄金样本的既有评分范围仍是 calibration-only，不是模型实测。

真实 H 对照保留冻结输入、输出身份、请求强度、费用和严格/独立核验结果。两档质量评分仍有未通过项，不能用离线校准或参考盲评分替代研究质量结论。运行中的各用途控件仍按正式 Work/admission 精确观察，缺原始供应商证明时显式标缺。
