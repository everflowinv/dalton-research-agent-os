# 持有期投资方法增量审查（2026-09-10）

## 结论与边界

用户新增的方法论是观点形成、估值表达和后续跟踪的增量要求，不改变 Dalton 既有六阶段研究流程，也不削弱 Initial Screen、行业框架、公司模型、辩论、验证和 Memo 的既有标准。行业认知仍然先于公司观点；这套方法只回答一个更靠后的问题：在最长十二个月的具体持有期内，预期回报来自盈利增长、盈利预期变化、估值变化中的哪一项，或怎样共振。

它不要求每项研究都具有短期催化剂。盈利按既定路径兑现本身可以构成投资依据；估值逐步正常化也可以有较长且不精确的实现窗口。催化剂、事件和跟踪信号用于说明路径和更新观点，缺少一个短期事件不应自动否定已经有充分证据的收益来源。

单一收益来源也可以形成高确信度观点。确信度应由现有证据质量、模型完整性、反证、风险收益标准和验证结果校准，不能由代理新增“必须两项共振”之类的政策门槛。共振是可能提高吸引力的事实，不是高确信度的必要条件。

## 一致的 forward 回报算术

所有分解必须使用同一估值口径、同一每股或企业价值口径，以及一致的 forward pricing period。定义：

- `P0`：观点形成时点的价格或企业价值。
- `F0`：当前定价采用的 forward 基准期。
- `FT`：持有期终点采用的 forward 基准期。
- `E0(F0)`：时点 0 对 F0 的市场基准盈利预期。
- `E0(FT)`：时点 0 对 FT 的市场基准盈利预期。
- `ET(FT)`：我们的终点情景对 FT 的盈利判断。它必须在观点形成时以当前内部模型冻结，不能事后用实际结果改写。
- `M0 = P0 / E0(F0)`：当前 forward multiple。
- `MT`：终点情景对同一盈利定义的 multiple。

于是：

`PT / P0 = [E0(FT) / E0(F0)] × [ET(FT) / E0(FT)] × [MT / M0]`

三个因子分别代表：

1. **盈利增长或自然滚动**：`E0(FT) / E0(F0)`。这是当前市场预期曲线自身从 F0 走到 FT 的变化。
2. **盈利预期上修空间**：`ET(FT) / E0(FT)`。这是内部判断与观点形成时市场对同一 FT 的预期差。
3. **估值变化**：`MT / M0`。起点和终点必须使用相同盈利定义和 forward 口径。

总回报由三个因子相乘后减一。界面可显示顺序 waterfall 或对数贡献，但必须披露展示方法和交互项；不能把三个百分比直接相加。

这个定义避免两类常见重复计算。第一，不能用 `ET(FT) / E0(F0)` 作为“盈利增长”，再额外加入 `ET(FT) / E0(FT)` 的预期上修；前者已经包含上修。第二，不能把基于 trailing earnings 的当前 multiple 与基于 forward earnings 的终点 multiple 相乘比较。若缺少 `E0(F0)`、`E0(FT)` 或同口径 `MT`，对应分解应显示 unavailable，并指出缺少的 period、metric、ref 和 as-of，不能退回 trailing multiple 冒充。

对于亏损、接近零盈利或口径不稳定的公司，可以显式选择 EV/revenue、EV/EBITDA、FCF yield 等另一套方法，但同一桥内起点与终点必须保持相同 metric、资本结构处理和期间定义。方法变化应形成新的情景版本，不能在一个分解中自动换分母。

## 现有能力

- `src/dalton_core/model_forecast_driver.py` 已保存逐期内部预测、actual/estimate 分离、assumption provenance、版本原因和证据引用，可提供 `ET(FT)` 及内部模型绑定。
- `src/dalton_core/consensus_estimate.py` 已将 sell-side/vendor EPS 和收入预期映射到真实财政期，并能比较同期间的内部预测与市场均值，可提供 `E0(F0)`、`E0(FT)` 的候选来源。
- `src/dalton_core/valuation_snapshot.py` 已提供由价格、股数和 filing fundamentals 确定性计算的估值快照与历史分位，但当前主要是 trailing 指标，不能直接充当本方法的 `M0`。
- `src/dalton_core/conviction_call.py` 已表达观点、市场观点、预期差、风险收益、实现路径和反证；适合承载回报桥的阅读结果，但现有 upside/downside 不是由上述公式机械验证。
- `src/dalton_core/guidance_profile.py` 与 `src/dalton_core/earnings_season.py` 已提供同期间、同单位的历史 guidance-versus-actual 和管理层风格证据，可帮助校准内部预测与 revision 发生概率。
- `src/dalton_core/thesis_impact.py` 已有 `implied_expectation` 概念，但当前不是带价格、期间、盈利口径和反解锚点的结构化市场隐含假设。

## 真实缺口

目前没有正式 authority 把某个观点形成时点的价格、两段 market forward estimates、内部终点估计和同口径起终点 multiples 固定在一个可重放记录中。因此系统能展示估值、预测和 consensus gap，却不能证明总回报与三项来源严格相等。

当前 consensus 数据可以诚实称为 sell-side 或 vendor consensus。系统没有 buy-side consensus authority。内部 ForecastModel 是 Dalton 的 house view，不应改名为 buy-side expectations。若未来接入 buy-side 数据，需要保存参与者类别、metric、period、as-of、分布、来源文档及版本 hash；没有数据时应明确 unavailable。

市场价格本身不能同时识别市场隐含盈利和市场隐含倍数。反解必须固定一个锚：给定可信 forward multiple 反解 implied earnings，或给定同期间 market earnings estimate 反解 implied multiple。记录必须说明 anchor kind、ref、hash、as-of 和来源。

历史 guidance bias 已可计算，但它不等于未来盈利上修。它只能作为带样本期、样本数、period/unit 匹配和证据引用的校准输入。任何对 ForecastModel 的影响都应成为显式 assumption 或 revision proposal，不能静默修改 actual 或共识。

## 建议的有界实现切片

### 1. 确定性 HoldingPeriodReturnBridge

新增 append-only authority，绑定 current mission、company、观点时点、任意有效且不超过十二个月的持有期、metric、F0、FT、price version、exact ConsensusEstimate、exact ForecastModel 和估值情景。期限应以日期或月数验证 `0 < horizon <= 12 months`，允许一至三个月等更短期限，不限制在 `3_6_months`、`6_12_months` 两个枚举。

记录上述三个因子、乘积总回报、公式版本、输入 refs/hashes 和 unavailable reasons。它只做确定性算术，不评价观点是否值得投资，也不新增人审门槛。

### 2. Forward valuation scenario

在现有估值读取之上增加独立的 forward 情景合同，明确 `metric`、`pricing_period`、`multiple`、资本结构输入、as-of 和证据。`M0` 必须由 `P0 / E0(F0)` 计算；`MT` 可以来自有证据的历史区间、peer 框架或明确的研究假设。来源不足时保留空缺。

### 3. Expectation history and market-implied view

把每次 sell-side consensus 的同期间快照作为 append-only 时间序列，使后续跟踪能区分：观点形成时的预期差、观点形成后的真实 consensus revision、公司实际盈利兑现。后两者是观点跟踪结果，不能回写并改变原始回报桥。

市场隐含假设另建确定性反解记录，并强制单一锚点。buy-side 数据在真实来源 authority 建成前保持 unavailable。

### 4. 观点与跟踪层接线

`ConvictionCall` 或后续观点产物引用 ReturnBridge version，而不是让模型自由生成无法复算的 upside。观点可标注收益基础为 earnings delivery、expectation revision、rerating 或 combination；只有明确声称 expectation revision 时才必须证明与市场预期存在差异。盈利兑现或估值正常化观点不因缺少强分歧而沉默。

跟踪层比较冻结桥与后来发生的 consensus、forecast、actual 和 valuation 版本，分别回答盈利是否兑现、预期是否上修、估值是否变化。它不能改变 Initial Screen、行业框架或前序研究阶段的完成条件。

## 必要回归

- 三项因子乘积严格等于总回报，且改变内部 `ET(FT)` 只改变 revision 因子。
- 同一 market curve 下，F0 到 FT 的自然滚动只进入 growth 因子。
- trailing 起点与 forward 终点混用必须拒绝。
- 相同 metric 但不同 fiscal period、currency、per-share/enterprise-value 口径必须拒绝。
- 缺 consensus、亏损分母或缺 target multiple 时返回具体 unavailable，不猜值。
- 单一 earnings-delivery 或 rerating 来源可形成高确信度候选，确信度仍按既有标准校准。
- 一至三个月的有效短期限可记录；超过十二个月的新观点拒绝。
- 旧六阶段产物、signed playbook、历史 ConvictionCall 和 hashes 保持逐字可读。
