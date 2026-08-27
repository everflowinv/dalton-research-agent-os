# S7d-6：brief v4 lane-only manifest 调查与草案（2026-08-27）

## 结论

可以用三条 policy-committed SEC lane Claim 注册一个只有「reported quarterly revenue growth」driver 的 evidence pack，并为 ACN、CTSH、EPAM 各注册一个 overlay。IBM 目前不能放在 coverage universe；ACN transcript qualitative Claim 也不应进入这个 lane-only v4。

## 合同答案

### A. 一个 KPI driver + 三家 Claim 能否注册？

可以。`register_evidence_pack` 只要求所有 driver 至少各有一个 binding，比较的是已绑定 driver 集合与 driver-pack 全集，不要求每个 metric 或每家公司都有 Claim：`src/dalton_core/industry_research.py:467-490`。本草案只有一个 driver，三条 revenue Claim 都绑定到它。

每个 overlay 仍需列出 driver pack 的全部 driver，且每个 `driver_views[].claim_version_refs` 非空：`src/dalton_core/industry_research.py:266-313, 611-688`。本草案的三家公司各有自己的正式 revenue Claim，所以满足该条件；没有为缺失 KPI 造 Claim。快照会把非 observed 单元格渲染到「KPI coverage gaps」：`src/dalton_core/industry_research.py:1032-1081`。

关键绑定键是 lane 实际写入的 `quarterly_revenue_yoy_growth`，不是人为命名的 `metric:revenue-growth-usd-gaap`。executor 在 SEC Company Facts staging 中固定该 `metric_or_aspect`，period 是当前季度 `start..end`，basis 是 `official-filing-xbrl`：`src/dalton_core/research_plan_executor.py:2185-2244`。因此 manifest 把「revenue-growth-usd-gaap」作为 driver 名，把前者作为 metric spec / binding key。

### B. ACN transcript qualitative Claim 放哪里？

先不进 live v4。它的正式 `metric_or_aspect` 是 `aspect:new-bookings-direction-local-currency`，并非 lane 的 revenue key；若要纳入，必须在同一 driver 增加这个 semantic aspect，再给 ACN overlay 增加 observed cell。这样技术上可行：metric spec 可以是 `semantic`，而 qualitative Claim 可以绑定到 driver；numeric metric 只有 transcript 作为 observed authority 才会被拒绝：`src/dalton_core/coverage_admission.py:89-137`、`src/dalton_core/industry_research.py:669-688`。

但 owner 已定 lane-only / SEC-only 口径，故把它作为 future non-lane corroboration，而不是把它伪装成 revenue corroboration。v4 的三条 binding 均为 SEC lane revenue Claim。

### C. IBM 缺 Claim 会不会挡 overlay？

会。`industry_brief_snapshot` 要 coverage universe 中每家公司恰有一个 overlay：`src/dalton_core/industry_research.py:792-819`；overlay 的每个 driver view 又必须引用本公司的 Claim：`src/dalton_core/industry_research.py:266-313, 647-668`。IBM 没有 revenue Claim 时无法形成合法的非空 driver view。因此 v4 暂时排除 IBM；8 MiB 放开并形成 IBM formal Claim 后，再以新的 immutable evidence-pack / overlay 版本加入，不能原地修改 v4。

## Manifest 设计

- `driver-pack-version:us-it-services:live-sec-lane-v1` 是 live Core 的首个 driver pack；先前 v2/v3/v4 都只存在于隔离 canary，不能拿来当 live prior。故 `prior_version_ref=null`。
- `industry-evidence-pack-version:us-it-services:live-sec-lane-v1` 是 live Core 的首个 evidence pack，直接钉住三条 live Claim、Claim hash 与 Evidence relation hash，不使用隔离 canary 的 8-K exhibit Claim；三个 overlay 同样从 live v1 开始。
- 报告内保留 issuer 的 fiscal/calendar period 与不同 US-GAAP concept caveat；不把 ACN、EPAM、CTSH 的期间误写成同一日历季度。
- manifest 是发布输入，不会自行写 live Core；发布器须按 `register_driver_pack` → `register_evidence_pack` → 三个 `register_company_overlay` 的顺序执行。

## 未做

- 未写 v4 isolated canary / 测试：现有 v3 canary 假设其 21 条 exhibit Claim 加 transcript，而 v4 要独立构造三条 formal SEC Claim 并支持 replacement driver pack；应在主 session 另开切片。
- 未访问或改写 live Core、未部署、未合并 main。
