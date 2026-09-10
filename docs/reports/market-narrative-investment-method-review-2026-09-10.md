# 市场叙事投资方法复核

日期：2026-09-10  
基线：integration `06d35ef`

## 定位与边界

市场叙事应作为现有研究流程之上的**增量观点形成与跟踪层**。它不改变
Initial Screen、行业认识、公司研究六阶段、现有 stage gate 或 owner 权限。
它的工作是把“市场现在相信什么、这个看法如何变化、价格可能反映了什么、
我们与它哪里不同、以后用什么验证”保存成可复演记录，并把值得继续查的问题
交给现有 ResearchTask 和跟踪机制。

严谨不等于禁止提出早期推断。系统可以提出 `narrative hypothesis`，但必须同时
写清：当前 confidence、支持证据、反例或替代解释、仍未知的部分、下一次验证的
具体对象和日期。没有可靠频率基础时不硬凑概率；没有研究依据时也不以机械转发
数、点赞数或固定声量阈值替代判断。

本层不新增人审门槛。任何会改变 thesis、mission、stage 或正式投资结论的动作，
仍走现有 authority 和审批路径。

## 当前真实能力

### DebateMap 已具备版本化争议骨架

`src/dalton_core/debate_map.py` 的 `DebateMapVersion` 是不可变版本链。每项 debate
保存 bull/bear、market position、our position、状态、首次出现时间、变化原因、
Claim 引用、driver 引用与两边独立来源计数。版本绑定 mission、constitution 和
policy。`versions`、`shifted_since` 与 `debate_for_driver` 已能回答：现在争什么、
我们站哪边、哪些新增引用或状态变化使地图移动。

来源阶梯也是真实存在的。filing、management、sell-side、expert、sales note、
news、crowd 分层保存；sell-side 可按 publisher 去重，同一家机构的多份文档不会
自动变成多家独立来源。无法识别 publisher 的 sell-side 文档不会仅凭文档数满足
独立性要求。

### 数字分歧与模型敏感性已有正式 authority

`src/dalton_core/consensus_bridge.py` 只把正式 consensus authority，或至少两家不同
券商形成的可验证范围，称为 consensus。单家报告仍是单家观点。

`src/dalton_core/forecast_sensitivity.py` 保存三至五个关键 driver 的历史峰值、谷值、
均值、当前假设、what-if 结果与 ours-versus-consensus bridge。它能够说明哪个经营
假设真正移动模型输出，但还没有把这些结果绑定到某个市场叙事的“priced in”判断。

`src/dalton_core/conviction_call.py` 已把 DebateMap 分歧、consensus gap、风险收益和
event pathway 组合成方向性候选。它要求市场观点有来源，不能把“没有市场资料”解释
为市场与我们不同。

### 人类信息源与事件复盘已有采集和引用路径

sales notes、X、雪球和 employee reviews 已有受治理的 connector、时间范围、artifact
和 source identity。sales notes 保存具名 sender/desk、时间和正文 hash。X/雪球保存
的是帖子或榜单证据，不是买方持仓。当前项目状态仍把 X/雪球列为 shadow；在 owner
批准并实际连通前，系统只能报告 unavailable/proposed，不能把 fixture 或演练当成
生产市场信号。

`src/dalton_core/event_judgement.py` 按 evidence tier 读取事件、thesis、driver、近期
判断和市场观察，并把 rating change、sales note、crowd post、news 分行交给
reflection。资料为空时 prompt 明确要求说不知道。`research_cycle_reflection.py` 的
每周 reflection 则复盘研究系统的花费、积压、质量、反馈与判断结果；它不是公司市场
叙事的生命周期记录。

## 关键缺口

### 1. 当前 market position 折叠了不同含义的信号

`src/dalton_core/debate_map_draft.py` 当前允许 sell-side ratings/targets、sales notes、
crowd 以及 management framing 一起形成一个 `market_position.lean`。虽然底层引用仍
带 tier，最终的单一 lean 容易让读者把以下几类东西误认为同一种“共识”：

- filing 中可验证的事实；
- 管理层观点；
- 券商或分析师观点；
- sales desk 的观察；
- 社媒注意力代理；
- 新闻转述。

社媒声量可以支持“某种说法传播得更广”的早期 hypothesis。它不能单独证明买方共识、
机构仓位、基本面事实或价格变化的因果。两家卖方机构形成的 consensus range 也仍是
sell-side consensus，不应改称 buy-side consensus。

### 2. 版本变化还不是叙事生命周期

DebateMap 有 `first_seen_at`、状态和新引用，但没有按时间保存各 source class 的独立
观察序列，也没有区分：新事实出现、旧观点扩散、主流媒体跟进、反例增长、叙事饱和或
反转。`shifted_since` 比较的是 debate 状态与引用变化，无法独立重放传播路径。

“主流化”和“非线性影响”不应由一个固定转发门槛决定。合理判断需要同时看来源类别、
独立来源 breadth、持续时间、观点是否跨 cohort 传播、价格和 consensus 是否同步变化，
并保留替代解释。早期资料不足时可以记录低 confidence hypothesis，而不是强行判为
mainstream 或直接删除。

### 3. 当前定价与未来争议没有同一份绑定记录

MarketPrice、ValuationSnapshot、ForecastModel、Sensitivity 和 Consensus 都已存在，
但 DebateMap 没有绑定 exact as-of 版本来表达：

- 当前价格或估值倍数中可能隐含了哪个 driver 区间；
- 我们与该隐含假设的差异；
- 哪个未来指标、事件或时间窗口会解决争议；
- 当前判断是事实、观点，还是带约束的推断。

利率也没有现成的“宏观利率变化直接等于公司估值变化”合同。它必须通过公司特定的
明确通道进入，例如 discount rate、债务再融资、利息收入、FX 或需求敏感性，并绑定
公式、单位、期间和来源。没有该桥时应记录 gap，而非给出看似精确的估值影响。

### 4. 历史复盘尚未闭合到未来跟踪

Event reflection 能复盘单个事件，weekly reflection 能评价研究过程，但目前没有一条
authority 把某次 narrative hypothesis 的预测、反例、估值 driver、验证日期，与后来
的 filing、consensus revision、价格反应和事件判断逐项对账。因而系统难以回答：过去
哪些叙事判断有效、哪些只是声量、哪些替代解释被忽略，以及未来应跟踪什么。

## 有界实现切片

### Slice 1：MarketNarrativeObservation

新增纯 authority 和 reader，从现有 Claim/Event 引用构建不可变的日或周观察。建议
closed shape 至少包括：

- subject/company/debate ref 与 observation window；
- `source_class`：`filed_fact`、`management_view`、`sell_side_view`、
  `sales_desk_signal`、`crowd_attention_proxy`、`news_report`；
- stance、statement、observed_at、source identity/ref/hash；
- 可选原生 attention counts，但不得换名为 consensus 或 positioning；
- `hypothesis`：statement、confidence（有序词表即可）、supporting refs、
  counterevidence refs、alternative explanations、unknowns、next validation/date；
- mission、policy 和 input fingerprint。

统计只在同一 source class 和明确 cohort 内比较 breadth、velocity、persistence。
confidence 由证据质量和反例共同解释，不要求虚构概率。验收反例包括：同一帖子高转发
不增加独立来源；同一券商多份报告仍是一家；management 重复话术不算外部扩散；事实
与声量同时上升不自动产生因果字段；shadow source 只能进入 unavailable/proposed。

### Slice 2：PricedDebateSnapshot

新增只读派生产物，绑定 exact DebateMapVersion、MarketPrice、ValuationSnapshot、
ForecastModel、Sensitivity、Consensus 的 ref/hash/as-of。每个 debate 分两部分：

- `current_pricing`：可观察价格/倍数、明确的 implied-driver 计算、我们的 model 区间、
  数据缺口；
- `future_resolution`：metric、window、catalyst、falsifier、支持和反例 refs、下一验证日。

如果 implied expectation 不能从现有公式重放，就保存 narrative hypothesis 与低/未知
confidence，不伪造数字。利率只有在已批准的公司特定 driver bridge 存在时参与计算。
该产物不修改 DebateMap、thesis 或 stage。

### Slice 3：NarrativeLifecycleReview

在固定窗口比较 Observation 版本，生成 `emerging`、`broadening`、
`mainstream_proxy`、`saturated_proxy`、`reversing`、`unresolved` 等带限定词的状态。
状态依据应是可审阅的多维证据，而非单一机械阈值；记录具体 supporting/counter refs 和
confidence rationale。

到验证日后，用正式 filing、consensus、price、event authority 对此前
`future_resolution` 记 `supported`、`refuted` 或 `unknown`，保留当时输入和现在结果。
产物可以提出 ResearchTask 或 tracking candidate，但仍走现有 admission、预算和权限
规则；它不自动修改 thesis，也不增加新的人工 checkpoint。

## 实施顺序

Slice 1 优先。若不先拆开事实、卖方观点、desk 观察和 crowd attention，任何主流化、
非线性或 priced-in 层都会继承错误的“市场共识”语义。Slice 2 随后把观点与真实价格和
driver arithmetic 对接。Slice 3 最后闭合历史复盘与未来跟踪。

这三个切片均可作为现有六阶段旁路的版本化研究读物逐步加入，不需要重写已有流程，
也不需要先新增 owner 裁决。
