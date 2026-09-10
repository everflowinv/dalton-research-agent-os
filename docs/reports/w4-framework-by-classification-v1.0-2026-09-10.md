# W4 切片：按 `industry_classification` 的 driver 模板 + `market_proxy` 证据种类

日期：2026-09-10
分支：`w4-framework-by-classification`（基于 main `ba99ef9`）
来源：`docs/reports/chem-retrospective-implications-v1.0-2026-09-10.md` §1 第 1–2 行、§5 第 1–2 项
规则：`docs/reports/parallel-development-plan-v1.0-2026-09-09.md` §4 固定规则 1–14

---

## 0. 一句话

分类现在有后果了：五类商业模式各有一张冻结的、内容哈希的 driver 模板，规格起草、档案 `demand_drivers`、DebateMap 三处共用它；同时新增证据种类 `market_proxy`，预测假设引用它必须写 `proxy_gap`，否则整条记录被拒绝、绝不修补。

Chem 复盘说得最准的一条是：万华按商品周期读、Linde 按合同型 compounder 读，是**两个问题**而不是一个问题的两个答案。我们此前已经有 `industry_classification`（Deep Insight Gate 第一问、五词闭合词表），但它不改变任何下游的形状——分类花了一次模型调用，什么也没买到。这条切片把它变成后果。

---

## 1. 做了什么

### 1.1 冻结的 driver 模板注册表

新模块 `src/dalton_core/driver_template.py`：纯 Python 常量，无表、无版本链（没有"这一轮学到、下一轮要被告知"的东西；表变了就是 `content_hash` 变了、就是新文件）。字节同时发布为 `deploy/phase9/w4-driver-template-v1.json`，一条测试断言两者未漂移——与 `p12c-debate-policy-v1.json` 同一套纪律。

六张模板，槽位就是复盘点名的那些：

| classification | 槽位 |
| --- | --- |
| `commodity_cycle` | `spread` 价差 / `utilisation` 开工率 / `cost_curve_position` 成本曲线位置 / `capacity_additions` 产能投放 |
| `capital_cycle` | `capex` 资本开支 / `returns_on_capital` 投入资本回报 / `capacity_cycle` 产能周期 |
| `contract_compounder` | `pricing` 定价 / `retention` 留存 / `unit_economics` 单位经济 |
| `structural_growth` | `penetration` 渗透 / `tam` 可及市场 / `adoption` 采纳节奏 |
| `turnaround` | `milestones` 里程碑 / `cash_runway` 现金跑道 |
| `generic` | `volume` 量 / `price` 价 / `cost_structure` 成本结构 |

每个槽位带三样东西：

* **basis concepts**：这个槽位在公司真的申报了的情况下会落在哪些报表概念上（本地名，不带 taxonomy 前缀）。`spread` 与 `tam` 的候选是**空的**，这不是遗漏——没人申报价差和 TAM，这正是 `market_proxy` 存在的理由。
* **expected evidence kinds**：来自 claim index 的证据种类词表（见 1.2）。
* **cues**：一张冻结的小写子串表，中英双语，用于判定某个产出是否覆盖了这个槽位。刻意**少报**：cue 漏掉的槽位会变成一条人可以驳回的 gap，cue 虚报的覆盖则是没人找得到的覆盖。

选择规则：`insufficient_evidence`、未知词、`None`、非字符串一律落到 `generic`，且模板自称 generic——提示词里写 `GENERIC, no classification was available`，每一条 gap 字符串里写「通用模板：该公司尚无行业分类」。悄悄用通用框架冒充考虑过的框架，比没有框架更糟。

`template_for()` 也接受整块 dossier `industry_classification` 对象，省掉每个调用方两行相同的解包。

### 1.2 `market_proxy` 证据种类

`claim_index_authority.py` 新增 `EVIDENCE_KINDS` / `EVIDENCE_KIND_DEFINITIONS` / `MARKET_PROXY`。这是与 `IMPORTANCE_TIERS` **正交**的一张词表：`importance` 说「谁说的」，evidence kind 说「这是什么量」。复盘 §7.2 最锋利的一条就是这两件事被混同——价格机构发布的 MDI–纯苯价差是一个完全可靠的公开数字，但它不是装置的现金利润，把它和申报格并排存放，系统就已经悄悄断言公司赚到了这个价差。

五个词：`company_figure` / `market_proxy` / `third_party_estimate` / `own_assumption` / `qualitative`。只有 `market_proxy` 带规则。

### 1.3 `proxy_gap`：拒绝，不修补

`model_forecast_driver.py`：

* `REF_KINDS` 加 `market_proxy`；`PROXY_GAP_REQUIRED = {market_proxy}`；`MAX_PROXY_GAP_CHARS = 400`。
* 一条 `market_proxy` 引用必须**指名它引的序列**（`ref` 非空），并携带非空 `proxy_gap`；否则 `ForecastModelValidationError`，整条记录被拒（refuse-whole），**不写任何默认句子**——这个字段承载的正是本模块无权做的那个判断。
* 其它任何 kind 携带 `proxy_gap` 同样被拒。
* `proxy_gap` 是**可选字段**：不是 `market_proxy` 时该键**不存在**，而不是写成 `null`。这样此前记录过的每个模型字节不变、`content_hash` 不变，不会凭空多出一次「公司什么也没变」的版本链步进。为此给 `_closed()` 加了一个窄的 `optional=` 参数（`debate_map._closed` 早就是这个形状）。
* 新增构造器 `market_proxy_ref(ref, *, proxy_gap, ...)`（关键字参数漏不掉）与读取器 `proxy_gaps(assumption)`。

### 1.4 三处接线

**规格起草线（`company_model_spec` / `company_model_state` / `company_model_cli`）**
* `build_company_model_state(..., industry_classification=...)`：分类进**被哈希的 body**——公司从 compounder 重分类为商品生产商就该重决一次模型，忽略重分类的哈希会把旧框架下写的规格重放一遍。无分类时该键**省略**（不是 `null`），所以 W4 之前建的每个投影哈希不变。
* `build_prompt()` 按分类选模板，并用公司真的申报了的概念填 basis concepts（`basis_concept_plan`）。没有申报对应格的槽位标 `needs_proxy`，提示词直接说：这不是可以丢掉的槽位，它是靠 market_proxy 的那个槽位，而靠 proxy 的 driver 必须说清它离公司实现值有多远。
* `TASK_HASH` 现在绑 `REGISTRY_REF` / `REGISTRY_HASH`：模板动了，任务就动了，否则商品模板下决的规格和通用模板下决的规格会被记成同一个问题的答案。
* `filed_classifications(store)`：从**档案**读分类（不问任何人；分类是 dossier 里版本化的判断，第二个决定它的地方就是同一个问题的第二个答案）。`insufficient_evidence` 与无档案的公司不进这张表。
* `spec_template_gaps(spec, state)` 进 summary 的 `template_gaps`：**只报告，不强制**。模板是关于一**类**公司的先验，规格是关于**这一家**公司的判断；因此而拒绝规格就是让表压过分析师。

**档案 `demand_drivers`（`company_dossier_draft` / `company_dossier_cli`）**
* `TEMPLATE_UNITS = {"demand_drivers"}`。结构仍然来自 Constitution 的因果链（C3，不是模型的选择）；模板作为**清单**印在槽位下方，并明说「清单，不是结构」。
* `parse_unit_output()` 把未覆盖的模板槽位**追加到 `gaps`**：起草者自己写的 gap 排在前面（它读过材料、这张表没读过），追加受该 section 自己的 `MAX_GAPS = 6` 上限约束。**永不拒绝**——缺一个 driver 是文件上的一个洞，读者必须看见它；拒绝会把「这个问题被问过」的记录一起删掉。
* CLI 传入档案当前持有的分类。`UNITS` 顺序里 classification 排在 sections 之后，所以首次起草这家公司时 demand_drivers 用通用模板、下一轮才接上——这是诚实的顺序：框架跟着分类走，不是反过来。

**DebateMap（`debate_map_draft` / `debate_map_cli`）**
* `build_input_table(..., industry_classification=...)` 把模板放进表并印进提示词，挨着可绑定的 driver 列表。**不是闸门**——闸门是 constitution gate，它检查 driver ref，不检查题材。
* `subject_classification(store, subject_ref)` 从档案读；行业主体没有档案，因此没有分类，落通用模板。
* summary 新增 `industry_classification` 与 `template_gaps`。

**判断层提示（`event_judgement`）**
* judge 提示词现在列举 `EVIDENCE_KIND_LINES`（五个种类逐条定义），并写死一句：market_proxy 永远不是公司自己的数字；如果下面某个 driver 或假设靠着它，与实现值的距离就写在旁边，把 proxy 当成实现值的判断无论 proxy 多好都是错的。
* `model_drivers()` 把每条假设的 `proxy_gaps` 带进上下文，`build_judge_prompt()` 逐条印为 `market_proxy gap: ...`。预测层的拒绝之所以值钱，就是因为判断层是那个否则会把价差读成实现利润的读者。

### 1.5 合同

新增 `contracts/driver-template-registry.schema.json`（闭合、`additionalProperties: false`）。`evidence_kinds` 的枚举就是 `EVIDENCE_KINDS`，含 `market_proxy`。它在 `tests/test_contracts.py` 的 id/created_at 豁免名单里加了一行并写明理由：这是一张冻结的常量表，不是任何 authority 写下的记录，没有实例，所以没有东西给 id 或 created_at 命名；它的身份是内容哈希与发布文件。

`contracts/` 下**没有其它枚举需要改**：查过全部 schema，现有的 `document_type` / `source_type` / `forecast_value_kind` / transcript `evidence_kind` 都是别的词表，`REF_KINDS` 与 `IMPORTANCE_TIERS` 此前根本没有 JSON 合同。

### 1.6 顺带修掉的一个真问题

模板表最初印成 `  <id>\t...`——和起草提示词印自己槽位结构的形状**一模一样**。档案 lane 的 fixture 正是这样读的，于是每一次起草都因为「填了没人要的槽位」被拒。表已改为不缩进（与 dossier 的 classification 词表同一个理由与同一个做法），并留下一条回归测试断言提示词里 `^  (\S+)\t` 只匹配得到真正的结构行。这是一个模型也会犯的错，不只是 fixture 的错。

---

## 2. 没做什么

* **没有新 lane、没有新 `*_schema.sql`**，因此规则 9 的四处登记不适用：`LANE_MODULES`、`cockpit_plane.REGISTRY_LANE_LABELS`、`bootstrap.py`、`scripts/rehearse_deploy.py` 均无需改动。同理 `install.sh` 无需播种——`deploy/phase9/w4-driver-template-v1.json` 与 `p12c-debate-policy-v1.json` 一样，常量是源，文件是发布副本，没有任何代码从磁盘读它。
* **没有把 `market_proxy` 写进 `claim_index` 的条目契约**。`EVIDENCE_KINDS` 目前是一张**词表**，被模板、预测层引用契约和判断层提示词共用；给 `claim_index_entry_versions` 加一列 `evidence_kind` 需要一次 schema 变更 + 一条打标规则（哪些 spec / 文档类型产出 market_proxy），那是一条独立的线，见 §4。
* **没有为 market_proxy 建采集面**。没有任何 connector 现在会产出价差、挂牌价或期货连续。这条切片建的是"如果拿到了，必须怎么写"，不是"从哪里拿"。
* **没有把模板变成闸门**。三处全部是 gap，不是拒绝。唯一的拒绝是 `proxy_gap`，而它拒绝的是"引用了却不说距离"，不是"没有覆盖某个槽位"。
* **cue 表没有覆盖行业术语的长尾**。它对化工价差、开工率、IT 服务的 bookings 这类词是够的；一家做别的生意的公司会有 cue 漏报，表现为一条可以驳回的 gap。这是设计上的方向，不是待修的缺陷。
* **`supply_and_cost` 没有模板**。它与 `demand_drivers` 共用因果链结构，但模板问的是需求从哪来。要不要给成本侧一张对应的表，是一个判断，留给主 agent 或 owner。

---

## 3. 集成时主 agent 要接的线

1. **cockpit**：`company_model_cli` 的 summary 多了 `driver_template`（classification / generic / registry_hash）与 `template_gaps`；`debate_map_cli` 的 summary 多了 `industry_classification` 与 `template_gaps`。复盘 §3.5 要求「缺口数」并排展示——这两处的 `template_gaps` 加上档案 section 的 `gaps` 就是 driver 维度的缺口来源，建议一起进「四格」的缺口格。
2. **档案顺序**：一家公司的第一版档案里，`demand_drivers` 会用通用模板（分类还没写），第二轮才接上真模板。如果希望第一版就用对框架，需要让 `industry_classification` 在 `UNITS` 里排到 `demand_drivers` 之前，或者让一次 tick 内先起草分类再起草需求。这是一个产品判断，我没有替它做。
3. **`spec_template_gaps` 目前只进 summary**。如果希望它进 deliverable 或进 cockpit 的公司页，需要在 `company_model_report` / `cockpit_plane` 里读 summary 的这个键。
4. **重分类会触发重决模型规格**：分类进了 `state_hash`，所以档案改了分类之后，规格 lane 会认为这家公司没有 current spec 并重跑一次。这是有意的，但它会花一次调用；如果不希望某次重分类触发重决，只能不改分类。
5. **`market_proxy` 目前没有生产者**。预测层已经能接受它，`default_assumptions` 不会产出它——它需要人写的假设或某条尚不存在的行情线。建 P13-M5 Excel 导出时，Sources 表的「口径备注」列天然就是 `proxy_gap` 的落点。

---

## 4. 建议的后续（不在本切片内）

* `claim_index` 条目加 `evidence_kind` 列 + 打标规则（哪些 discovery spec 产出 market_proxy），让"四类数字分开"从预测层扩到整个 Ledger。
* `supply_and_cost` 的分类模板。
* cue 表按行业扩充，或者把覆盖判定改成"模板槽位 → driver ref"的显式绑定（更准，但需要 driver pack 与模板对齐，是一条更大的线）。

---

## 5. 验收

全量：`cd ~/Projects/dalton-w4-framework-by-classification-worktree && PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`

```
Ran 5442 tests in 695.605s

OK (skipped=1)
```

新增测试 60 项，分三个文件：

* `tests/test_driver_template.py`（28）：注册表与发布字节一致、合同自洽、五类分类的槽位就是复盘点名的那些、槽位 id 唯一、未知证据种类进不了槽位、无分类/未知/`insufficient_evidence` 一律落 generic 且自称 generic、basis concept 只填公司真的申报了的、前缀不决定匹配、cue 中英双语覆盖、gap 有界且确定性、三个读取器（档案 section / DebateMap / 规格）各自的读法与空输入。
* `tests/test_market_proxy_evidence.py`（12）：词表位置与定义、不是 importance tier、缺 `proxy_gap` 被拒且理由具体、空白/非字符串/超长被拒、未指名序列被拒、非 proxy 携带 `proxy_gap` 被拒、**非 proxy 引用的字节一个字节没动**、`proxy_gaps()` 按顺序读出。
* `tests/test_framework_by_classification_wiring.py`（20）：规格提示词按分类选模板并填申报概念、无分类落通用并自称通用、`state_hash` 随分类变而无分类时不变、模板表读不成槽位结构（回归）、档案 `demand_drivers` 有模板而别的 section 没有、未覆盖槽位变 gap 不变拒绝、起草者自己的 gap 排前面且 `MAX_GAPS` 生效、DebateMap 表与提示词携带模板、整条预测模型带 proxy 能发布 / 无 gap 被整体拒绝 / 无 proxy 的模型仍然是 duplicate、judge 提示词列举证据种类且 proxy gap 随假设进提示词。

全部确定性：无网络、无模型调用、无时钟依赖（唯一的日期是 fixture 里的固定季度）。
