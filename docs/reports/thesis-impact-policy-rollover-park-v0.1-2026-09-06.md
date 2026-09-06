# thesis-impact 治理政策换版：从死循环失败改为显式停泊

日期：2026-09-06
状态：隔离副本实现与本地验收完成；本轮报告写作时尚未部署。
基线：`08417ed`（分支 `p9d3b-readonly-preflight`）。约束仍遵循 [ADR-0001](../adr/0001-thesis-confidence-and-coverage-admission.md) 与 [ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)。

## 现象与根因

live LaunchAgent `space.lumos.dalton.thesis-impact` 每 300 秒跑一次 `dalton_core.thesis_impact_production.main`。
日志 `~/Library/Logs/Dalton/thesis-impact.stdout.log` 从第 2930 行起连续 **1225 次**输出
`{"error_type": "RemoteError", "status": "failed"}` 并 exit 2；按 300 秒推算首次失败在 2026-09-02 06:xx UTC 前后。

在 live 只读副本上逐个 op 复现，调用链是 `thesis_impact_targets` → `thesis_impact_start` →
`thesis_impact_advance_assessment` 均成功，`thesis_impact_advance_verification` 抛 conflict。原始异常：

```
ThesisImpactIneligible: assessment verification policy is no longer active
  → ResearchPlanThesisImpactConflict: passed verification did not produce an eligible assessment
```

`eligible_assessment` 要求冻结的 verification 记录绑定的政策版本仍是当前 `governance_policy_pointer`。live 唯一一条
verification（`thesis-impact-verification:379796f9…`，verdict=pass，assessment `thesis-impact:f53bd7da…`）绑的是
`policy-3`；指针已在 **2026-09-02T07:01:12Z 换到 `policy-4`**（P9b-1 SEC company-facts 上线时发布）。这条绑定此后
永远不可能自行变成 eligible，但 runner 每 5 分钟仍重跑一遍：重复 enqueue、重放 WorkOrder、重复记录 assessment，
最后在同一处抛错。`main()` 只打印 `error_type`，所以日志里看不出真实阻塞点。

**与 2026-09-06 的 `08417ed` 部署无关**：失败早于该部署 4 天，两者不在同一条代码路径。

## 改了什么

不放宽 gate：政策换版后旧验证依旧 **不 eligible**，不自动重跑评估/验证，不改任何 authority，不发布政策绑定。
改的是"这件事如何被表达和处理"。

- `thesis_impact.py`：新增 `ThesisImpactVerificationPolicySuperseded(ThesisImpactIneligible)`，带 exact 明细
  （verification/assessment ref、验证时绑定的政策版本 ref+hash、当前生效政策 ref+hash）。指针缺失仍走原来的
  通用 ineligible 分支，语义不变。
- `thesis_impact.py`：新增只读 `superseded_verification(claim_version_ref, thesis_version_ref)`，用既有
  `idx_thesis_impact_claim` 查该绑定下最新的 pass，与当前指针比对；不发布任何记录，也不放宽任何检查。
- `thesis_impact_control.py`：新增 `POLICY_SUPERSEDED_STATUS = "verification_policy_superseded"`。
  `start_from_closed_plan` 在生成/入队 assessment WorkOrder **之前**先检查停泊状态，命中就直接返回该状态且
  `assessment_work_order` 为 `None`；`advance_verification` 捕获新异常后返回同一状态（`eligible` 与 `follow_up`
  均为 `None`），不再抛出一个调用方无法处理的 conflict。其余 ineligible 情形（无验证、verdict=reject、政策 gate
  拒绝）保持原有 conflict/异常路径。
- `thesis_impact_production.py`：`_run_target` 透传该状态；`run_once` 用 `SETTLED_STATUSES` 与
  `BLOCKED_STATUSES` 分类，全部目标要么已了结、要么停泊时整趟返回 `blocked_pending_human`，只要有真实故障仍是
  `incomplete`。`main()` 退出码：`idle`/`completed` = 0，`blocked_pending_human` = **3**，其余 = 2；失败输出
  改为带 writer 映射的 `error_code` 与固定错误文本（截断 200 字符），不输出 prompt、原文、token 或凭据。

退出码 3 的含义：这一趟没有崩溃，但也**不是成功**，它在等人决定。launchd 仍会记录非零退出，不会被伪装成正常完成。

## 验收

| 检查 | 结果 | 日志 |
|---|---|---|
| 全仓 unittest | **1117/1117**，169.512 秒，EXIT=0（较上轮 +7） | `temp/p9d3b-preflight/logs/rollover-full.log` |
| 专项与邻接 unittest | **91/91**，29.515 秒 | `logs/rollover-targeted.log` |
| live 只读副本端到端 runner | 返回 `blocked_pending_human`，目标状态 `verification_policy_superseded`，明细为 policy-3 → policy-4；assessment/verification/WorkOrder/attempt 计数前后完全不变；`OpenClawModelAdapter.execute` 与 `replay` 均被打桩为抛错且从未触发 | `logs/rollover-live-copy-canary.log` |
| wheel/sdist | 构建成功，wheel SHA-256 `2b812045a5b986821fde3912e54ca8e5630ef4c365025b3a99abd2ea1fbd159b` | `logs/rollover-build.log` |
| 干净安装 | 新建 venv，`--no-index --no-deps` 安装后 import 来自 site-packages，17 项相关测试通过 | `logs/rollover-install.log`、`logs/rollover-installed-tests.log` |

新增测试：`tests/test_thesis_impact_policy_rollover.py`（授权层：换版前 eligible、换版后抛新异常且明细准确、
只读查询与异常明细一致、reject 与"无验证"不被误判为停泊、无关绑定不停泊、计数与 integrity 不变；runner 层：
状态分类、退出码分档、失败输出形状与截断）；`tests/test_thesis_impact_control.py` 增加控制面完整一轮
（评估 → 验证 → eligible → 换版 → start/advance_verification 双双停泊，Core/Scheduler 计数与 thesis 指针不变）。

未跑：`integrations/openclaw-model-broker` 的 `npm run check`。本轮未改 broker 及其任何调用协议，沿用上一轮
25/25 的结论，不把它算作本轮证据。

## 这次没做的事（属 owner 决策）

政策换版后要让这条绑定重新有结论，只有两条路，都需要 owner 明确批准，本轮都没有实施：

1. **在 `policy-4` 下重跑一次评估与验证**。会产生 2 次付费模型调用（assessment ≤ 0.25 USD、verifier ≤ 0.25 USD，
   按现有 `ASSESSMENT_BUDGET`/`VERIFIER_BUDGET`）与新的 assessment/verification authority 记录。还需要一个
   明确的重跑身份：现有 WorkOrder id 由 exact claim/thesis 绑定派生，与政策版本无关，所以要把政策版本纳入
   identity（类似现有 `_work_id_with_redrive` 的做法），否则新一轮会撞上已成功的旧 WorkOrder。
2. **接受"换版即作废"并显式关闭这条绑定**，例如由人裁决记录一条结论后不再进入 targets。

在 owner 拍板前，系统保持现在的行为：停泊、可见、不花钱、不自动晋级。另外建议 owner 顺带确认一件事：
`policy-3 → policy-4` 是 P9b-1 上线的连带结果，当时没有人预期它会让已通过的 thesis-impact 结论失效；
如果这类"政策换版使既有结论作废"应当在发布政策时就被提示，那是发布流程的改动，不在本片范围。

## 边界

未验证真实模型质量与真实账单；本轮零付费调用。所有 live 数据只以只读副本参与验证，未写 live Core/Scheduler，
未改 live 配置、预算、权限或 mission，未接受任何 Claim/Thesis。
