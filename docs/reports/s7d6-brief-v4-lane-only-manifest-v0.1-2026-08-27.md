# S7d-6：brief v4 lane-only manifest 调查与草案（2026-08-27）

## 结论

可以用四条 policy-committed SEC lane Claim 注册一个只有「reported quarterly revenue growth」driver 的 evidence pack，并为 ACN、CTSH、EPAM、IBM 各注册一个 overlay。ACN transcript qualitative Claim 不进入这个 lane-only v4。

## 合同答案

### A. 一个 KPI driver + 四家 Claim 能否注册？

可以。`register_evidence_pack` 只要求所有 driver 至少各有一个 binding，比较的是已绑定 driver 集合与 driver-pack 全集，不要求每个 metric 或每家公司都有 Claim：`src/dalton_core/industry_research.py:467-490`。本 manifest 只有一个 driver，四条 revenue Claim 都绑定到它。

每个 overlay 仍需列出 driver pack 的全部 driver，且每个 `driver_views[].claim_version_refs` 非空：`src/dalton_core/industry_research.py:266-313, 611-688`。四家公司各有自己的正式 revenue Claim，所以满足该条件；没有为缺失 KPI 造 Claim。快照会把非 observed 单元格渲染到「KPI coverage gaps」：`src/dalton_core/industry_research.py:1032-1081`。

关键绑定键是 lane 实际写入的 `quarterly_revenue_yoy_growth`，不是人为命名的 `metric:revenue-growth-usd-gaap`。executor 在 SEC Company Facts staging 中固定该 `metric_or_aspect`，period 是当前季度 `start..end`，basis 是 `official-filing-xbrl`：`src/dalton_core/research_plan_executor.py:2185-2244`。因此 manifest 把「revenue-growth-usd-gaap」作为 driver 名，把前者作为 metric spec / binding key。

### B. ACN transcript qualitative Claim 放哪里？

先不进 live v4。它的正式 `metric_or_aspect` 是 `aspect:new-bookings-direction-local-currency`，并非 lane 的 revenue key；若要纳入，必须在同一 driver 增加这个 semantic aspect，再给 ACN overlay 增加 observed cell。这样技术上可行：metric spec 可以是 `semantic`，而 qualitative Claim 可以绑定到 driver；numeric metric 只有 transcript 作为 observed authority 才会被拒绝：`src/dalton_core/coverage_admission.py:89-137`、`src/dalton_core/industry_research.py:669-688`。

但 owner 已定 lane-only / SEC-only 口径，故把它作为 future non-lane corroboration，而不是把它伪装成 revenue corroboration。v4 的三条 binding 均为 SEC lane revenue Claim。

### C. IBM 缺 Claim 会不会挡 overlay？

会。`industry_brief_snapshot` 要 coverage universe 中每家公司恰有一个 overlay：`src/dalton_core/industry_research.py:792-819`；overlay 的每个 driver view 又必须引用本公司的 Claim：`src/dalton_core/industry_research.py:266-313, 647-668`。IBM 没有 revenue Claim 时无法形成合法的非空 driver view。S7d-5 部署后 IBM 已经由 policy-2 自动提交正式 Claim，因此最终首次发布把 IBM 纳入同一 live-v1 authority；没有发布过不含 IBM 的 live prior。

## Manifest 设计

- `driver-pack-version:us-it-services:live-sec-lane-v1` 是 live Core 的首个 driver pack；先前 v2/v3/v4 都只存在于隔离 canary，不能拿来当 live prior。故 `prior_version_ref=null`。
- `industry-evidence-pack-version:us-it-services:live-sec-lane-v1` 是 live Core 的首个 evidence pack，直接钉住四条 live Claim、Claim hash 与 Evidence relation hash，不使用隔离 canary 的 8-K exhibit Claim；四个 overlay 同样从 live v1 开始。
- 报告内保留 issuer 的 fiscal/calendar period 与不同 US-GAAP concept caveat；不把 ACN、EPAM、CTSH、IBM 的期间误写成同一日历季度。
- manifest 是发布输入，不会自行写 live Core；发布器须按 `register_driver_pack` → `register_evidence_pack` → 三个 `register_company_overlay` 的顺序执行。

## 未做

- 未访问或改写 live Core、未部署、未合并 main。

## 主 session 复核

- 复核 live Core 后确认 driver pack、industry evidence pack、company overlay 三条版本链都还是空表。草案最初把隔离 canary 的 v4/v3 当 live prior，会被「first version cannot have a prior」合同拒绝；已改成独立的 `live-sec-lane-v1` refs，全部 `prior_version_ref=null`。隔离 canary 的编号不再冒充 live authority。
- 在 live Core + candidate staging 的 SQLite 一致性副本上注册 manifest：driver pack v1、evidence pack v1、三家公司 overlay v1 全部成功；快照含 1 个 driver、3 家公司、3 条 formal Claim。
- Markdown 渲染 7,448 bytes；同一组 exact refs 重放逐字节一致，render hash `1def0359…04a48`。副本中已有 IBM 演练 Claim，但 v4 草案按 as-of 边界只覆盖 ACN/CTSH/EPAM，证明 coverage universe 不会从 Core 自动扩张。
- IBM live ticket `sec-lane-run:2a6c518b28cdf11987ba1629` 已 policy-commit：Q2 2026 Revenues 17,162.0M 美元，同比 +1.09%，10-Q `0000051143-26-000078`；source/numeric verification 均 pass。manifest 已换成 IBM 的实际 live Claim/relation ref/hash，等待最终副本验收后做 live 首次发布。
- 最终四家公司 manifest 在 IBM 落库后的 fresh live 副本完成注册：driver pack v1、evidence pack v1、四个 overlay v1；4 Claim、4 source、1 个 driver、4 个 KPI cell。Markdown 9,279 bytes，重放逐字节一致，render hash `ee95c975…2d4a4`。
