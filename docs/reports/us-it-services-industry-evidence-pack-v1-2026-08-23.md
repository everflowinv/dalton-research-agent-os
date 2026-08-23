# US IT Services 行业 Evidence Pack / ACN Overlay v1

## 结论

开发候选已经把“行业先行”落成 Core 合同：行业 evidence pack 只能绑定正式 EvidenceVersion、ClaimVersion 和
EvidenceRelation；company overlay 只能复用 driver pack，并且只能引用 pack 内的公司 Claim 与同源、已准入的 actual
Model Input。它不会创建或修改 ThesisVersion。

本轮 ACN 只是纵向样本。覆盖池已经固定 ACN、CTSH、EPAM、IBM 的角色，但目前正式 evidence bindings 全部来自 ACN，
因此还不能称为跨公司行业结论。下一 gate 是补 CTSH、EPAM、IBM 的 peer evidence，确认同一 schema 和 driver 是否能复用。

## 一手证据

本轮先按 SEC filing 流程核对 ACN 最新披露：

- 最新 10-Q：2026-06-18 提交，报告期截至 2026-05-31，accession
  `0001467373-26-000032`；
- Q3 FY2026 业绩 8-K：2026-06-18，accession `0001467373-26-000031`；
- exact exhibit：`q3fy26earnings8-kexhibit.htm`，SEC 原文：
  https://www.sec.gov/Archives/edgar/data/1467373/000146737326000031/q3fy26earnings8-kexhibit.htm

隔离 canary 从该 exhibit 固化 6 条 Claim：新签订单 USD 19.32 billion、本地货币新签订单同比 -3%、本地货币收入同比
3%、大型 AI transformation program 的定性需求信号、GAAP operating margin 17.0%、free cash flow USD 3.60
billion。其中 5 条定量 Claim 同时形成 human-admitted actual Model Input。AI 需求信号只保留为 qualitative，不把管理层
评论改写成 AI bookings、AI revenue 或市场规模。

## Core 合同

新增 `IndustryResearchAuthority`，管理两条不可变版本链：

- `IndustryEvidencePackVersion`：绑定 exact Driver Pack、行业边界、覆盖池、每个 driver 的 Claim/Relation、行业 debate、
  source plan 和报告字段；每个 driver 至少要有一条正式 evidence binding；
- `CompanyOverlayVersion`：逐个 driver 记录公司 stance、Claim、actual Model Input、差异与 watchpoint；公司必须已在 pack
  覆盖池中，Claim 必须属于该公司和该 driver，Model Input 必须由同一组 pack Evidence 提供 source authority。

两条版本链都有 latest pointer、exact idempotency、append-only trigger、hash replay 和 integrity report。注册和更新只能由
认证 `human:` principal 完成；research worker 不能直接发布 pack 或 overlay。新增公司只增加数据版本，不改 Core schema。

## Driver Pack v2

v1 保持不可变；v2 在原有四个 driver 上补了三个口径：

- `metric:new-bookings-growth-local-currency`；
- `metric:ai-demand-signal`，明确是 qualitative management commentary；
- `metric:operating-margin-gaap`，与 adjusted operating margin 分开。

这三项解决了 v1 的口径缺口：不能用订单金额代替订单增速，不能用 AI 评论冒充 AI bookings，也不能把 GAAP margin
默认写成 adjusted margin。

## 隔离 canary

`scripts/run_isolated_us_it_services_evidence_canary.py` 只使用 in-memory Core：

- Driver Pack：4 个 driver，v1 → v2；
- coverage universe：4 家；
- formal Evidence：1 条；formal Claim / Relation：各 6 条；
- human-admitted actual Model Input：5 条；
- Industry Evidence Pack / ACN Overlay：各 1 条；
- ThesisVersion：0；paid model call：0；
- IndustryResearch 与 ModelInput integrity report 均通过。

## 边界与下一步

- 未写 live Core，未激活 production mapping，未创建或准入 ACN thesis；
- 未调用 AlphaEngine、Gemini、Guidepoint 或付费模型；
- ACN 的业绩 exhibit 属于 issuer source，6 条 Claim 不能算 6 个独立信源；
- 下一切片补 CTSH、EPAM、IBM 最新 filing / earnings evidence，先验证 KPI 口径和 driver 复用，再生成真正的行业 brief 和
  公司差异矩阵。
