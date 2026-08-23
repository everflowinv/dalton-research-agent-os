# Thesis-impact production activation

日期：2026-08-22  
状态：live worker 与 production controls 已启用；没有 live thesis target，因此保持 idle

## 激活结果

Owner 批准 production activation 后，live Dalton 安装了 `3fe746e` 对应的 wheel，并启用独立的
`space.lumos.dalton.thesis-impact` 短任务。该任务 `RunAtLoad`，每 300 秒运行一次，不是常驻模型进程：

- writer 持有 Core 和 Scheduler；thesis-impact worker 只能通过 scoped Writer RPC 读取 target 并提交受限操作，
  不直接打开 live Core SQLite；
- assessment 固定使用 `model-routing-policy-version:dalton-openclaw-assessment:1`，只允许
  `profile:gpt-5-6-sol`；
- verifier 固定使用 `model-routing-policy-version:dalton-openclaw-verifier:1`，只允许
  `profile:gemini-3-7-flash`，并要求 provider-controlled `thinking=low`；
- production day-budget policy 为 `thesis-impact-day-budget-policy:production:1`，每天 USD 25；
- 当前 live Core 的 ThesisVersion 数量为 0，配置中的 `company_thesis_refs` 也是空对象。worker 两次 LaunchAgent
  运行和一次手动运行均返回 `status=idle`、`provider_call_count=0`、`target_count=0`；
- activation 后 assessment、verification、budget admission、rejection 和 alert 均为 0，Thesis current pointer
  没有变化。本次部署没有新增模型费用。

live 模型目录使用当前 OpenClaw 公开价格生成 immutable profile version：GPT-5.6 Sol 为 USD 4/20 每百万
input/output token，Gemini 3.7 Flash 为 USD 0.75/3.75。目录升级现在可重复执行；同一 policy ref 只有语义完全
相同才返回 duplicate，语义漂移会 fail closed。

## 部署与回滚

部署前创建 `pre-thesis-impact-production-20260822-v1` 快照，包含 Core、Scheduler、ModelRouter、dashboard
projection，以及原 service config、writer token config 和三个原 LaunchAgent plist。`dalton-backup verify-restore`
在新的临时目录重新校验四个 SQLite 文件，全部 hash 匹配。

部署后：

- writer、controller、control 三个常驻 LaunchAgent 均为 running；
- thesis-impact LaunchAgent 不是常驻进程，最近两次 exit code 均为 0；
- `dalton-health` 返回 `ok=true`，writer/control socket、controller、projection、backup 和插件检查均通过；
- live Core、Scheduler、ModelRouter、thesis-impact budget 四个数据库的 `PRAGMA integrity_check` 均为 `ok`；
- OpenClaw patch chain `apply_all.sh --check` 返回 `CHECK OK`。

本轮没有修改 OpenClaw host 配置或 patch，当前 gateway 已是修正版 3×30 canary 和 isolated shadow 使用并验收过的
进程，因此没有做无必要的 gateway restart。

## 相邻 Agenda 状态

health heartbeat 中 Agenda 子状态仍显示当日 failed。对应事件创建于 2026-08-23 00:33 UTC，早于本次
thesis-impact 部署；原因为 `model_output_contract_rejected`。重启后的 Agenda 按同一日 idempotency key 返回原失败
cycle，没有新建模型调用。Dalton 总体 health 仍为 `ok=true`。该事件不属于 thesis-impact activation 回归，但应在
下一次 Agenda contract 维护中单独处理。

## 边界

本次 activation 只打开受控执行 lane，不伪造 live target。系统尚未激活 ThesisVersion 自动 mutation，也没有把
isolated MSFT shadow 写入 live Core。只有 live ResearchPlan 形成正式 Claim、live Core 已有 current ThesisVersion，
并显式配置 company→thesis mapping 后，worker 才会进入付费 assessment/verifier 流程；届时仍受 phase pin、
provider controls、USD 25 day cap、append-only accounting、replay 和 fail-closed gate 约束。
