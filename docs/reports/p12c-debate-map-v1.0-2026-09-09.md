# P12c：分歧地图——把「市场在哪、我们在哪、什么能验证」写成版本链

*2026-09-09* · 分支 `w2-debate-map`，基于 main `7708d43`（2,843 项）
· 依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 1 节（owner 自省清单）、
C3（Constitution `question_admission` 作为 debate 候选的闸门）、D2（independence predicate）、
[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) 5.2 P12c、
[ADR-0008](../adr/0008-research-outputs-are-never-terminal.md)、
[P12b Claim 索引](p12b-claim-index-v1.0-2026-09-09.md)

---

## 零、这一片回答的问题

蓝图给 P12c 的定义是「争议清单」。但 owner 在自省清单里给的判据更严：
**同意市场没有价值**——值得写下来的只有 variant view、consensus 可能错在哪、
以及什么事件能把这个分歧收敛掉。所以本片把 `market_position` 与 `our_position`
做成**两个各自必填的字段**，而不是一段叙述里的两句话：

- 一版 DebateMapVersion 里的每条 debate 都必须同时说出「市场站在哪一边」和「我们站在哪一边」；
- 「市场没说话」是 `available: false`，是一个**答案**，不是空白；
- 「我们还没有观点」是 `none_yet`，也是一个答案，而且**不许把市场的观点抄进来**（prompt 里写死了这句）；
- 「哪一边在赢」（`gaining`）是 `open` 变 `shifting` 的唯一来源，只有同时握着上一版和这一版的东西答得出来，
  所以上一版的地图进 prompt。

Playbook 的十二问里，Q7（当前 consensus / bull camp / bear camp）与 Q8（多还是空？variant view 是什么？
consensus 哪里可能错？）正好是这两个字段；Q9（event pathway）是 `last_shift_reason` 与 `resolution`
的位置——一条 debate 靠什么证据收敛，就写在把它推动或了结的那几个 ref 上。

## 一、形状

### `DebateMapVersion`（`debate_map.py` + `debate_map_schema.sql`）

一个 subject（公司或行业）一条链，append-only、content-hash、三个 `dalton_authorized()` 触发器、
写后读回、内容没变就是 `duplicate`——`statement_snapshot` / `claim_index_authority` 的样板。
**没有 pointer 表**：当前版 = `MAX(version_number)`，于是整张表连 UPDATE 的余地都没有。

| 字段 | 说明 |
| --- | --- |
| `map_ref` / `subject_ref` / `subject_kind` | `debate-map:<subject>`；`company` 或 `industry` |
| `version` / `prior_version_ref` | 版本链 |
| `change_reason` | ADR-0008 五词封闭表 |
| `change_evidence_refs` | 触发这一版的 refs，**必须非空且必须是这一版真的引用到的 ref** |
| `constitution_ref` / `constitution_hash` | 哪一版宪法的 `method` 放行了这些问题 |
| `policy_ref` / `policy_hash` | 冻结的闸门策略（见第二节） |
| `evidence_fingerprint` | 起草时那一批 canonical Claim 版本的哈希；lane 靠它判断「证据变了没有」 |
| `debates[]` | 见下 |
| `rejected_by_constitution[]` | 被闸门拒掉的候选，连同理由，**留在版本里** |
| `drafted_by` / `verified_by` | 两次调用的 work order / invocation / route decision / model_family |

一条 debate：

```
{debate_ref, question, driver_refs[]（必须绑到 spec driver 或 IndustryDriverPack driver）,
 bull_position{statement, claim_refs[]}, bear_position{statement, claim_refs[]},
 market_position{available, lean∈{bull,bear,split}, statement, refs[]},
 our_position{state∈{held,none_yet}, side∈{bull,bear,neither}, statement, refs[]},
 status∈{candidate, open, shifting, resolved},
 last_shift_reason{reason, refs[]} | null,
 first_seen_at, source_independence{bull_sources, bear_sources}}
```

三条契约级硬规则（都有测试）：

1. **`available: false` 的 market_position 不许同时带 lean 或 refs。**「不知道市场在哪」
   和「市场同意我们」必须长得不一样，否则前者会被读成后者。`none_yet` 的 our_position 同理。
2. **`shifting` 与 `resolved` 必须带 `last_shift_reason` 且其 refs 非空。** 说「争论移动了」
   而说不出被什么移动的，是断言；`resolved` 的那几个 ref 就是「resolving ref」。
3. **`change_evidence_refs` 必须是这一版引用到的 ref 的子集。** 拿一份这一版根本没用到的文件
   当「这次为什么改」，是关于别的东西的理由。

### 为什么多一个 `candidate`

蓝图给了三个状态，本片是四个，多出来的 `candidate` 是最重要的一个。
一个问题两边都有证据、但每边只有一家券商，是**真问题、假 debate**。
把它记成 `open`，周报里它就会和「两家独立机构互相打架」的那条并排——两条都会被错误定价。
所以它被收下、留着、并如实标成 candidate；`open_debates()` 不返回它。

### 版本规则（ADR-0008）

`novelty(prior, candidate)` 一个函数，两侧读同一条规则：
**新 debate / 状态变化 / 出现当前版没引用过的 ref**，三者有其一才是新版本；否则 `duplicate`，一个字节不写。
`publish_map` 是 ADR-0008 说的「机制入口」：`change_reason` 与证据 refs 由调用方给，
类里没有任何一条路径会自己调它。

## 二、Constitution 闸门（C3）

每个候选 debate 在**起草成正文之前**先过 `screen_candidate()`，判据全部机械——
不是 ref 属不属于某个集合，就是下标在不在范围内。没有一处是读句子形成判断的，
否则闸门就成了第二个没有 verifier 的起草器。

十一个理由，每个一个测试（`test_every_gate_reason_has_a_candidate_that_triggers_it`
还断言这十一个词与 `GATE_REASONS` 逐字相等，所以加一个理由不加测试会红）：

| 理由 | 检查 |
| --- | --- |
| `empty_question` / `empty_position` | 问题或某一边的陈述是空的 |
| `no_driver_binding` | 一条 driver 都没绑 |
| `unknown_driver` | 绑的 driver 不在「宪法绑定的 driver pack + 该公司 model spec 的 revenue driver」里 |
| `no_bull_evidence` / `no_bear_evidence` | 某一边一条 Claim 都没有 |
| `unknown_claim_ref` | 引了没给它看过的 Claim |
| `no_question_admission_rule` / `unknown_question_admission_rule` | 没说自己被哪条 `method.question_admission` 准入，或说了个不存在的号 |
| `no_causal_chain_link` / `unknown_causal_chain_link` | 没说自己坐在 `method.causal_chain` 的哪一环，或说了个不存在的号 |

**「落在研究问题 / 因果链上」怎么做成确定性的**：把宪法的两张表**编号后放进 prompt**，
要求起草时点名 `question_admission_index` 与 `causal_chain_index`；闸门只校验下标存在。
这样机械的部分（下标合不合法）和判断的部分（它到底坐在哪一环）分得干干净净，
而且 verifier 有一个 `question_not_on_the_named_causal_link` 的 finding code 专门复核后者。

**独立来源不是拒绝理由，是降级理由**：两边各 ≥2 个独立来源才给 `open`，否则收下但标 `candidate`。
被拒的候选进 `rejected_by_constitution`，随版本一起存——闸门的拒绝如果看不见，就没人能审计这个闸门。

### 冻结策略 `deploy/phase9/p12c-debate-policy-v1.json`

阈值、tier 映射、publisher 表、极性词表全在里面，`debate_map.DEBATE_POLICY` 是同一份字节，
`POLICY_HASH` 进每一版 DebateMapVersion，测试断言文件与常量没有漂移。
理由和仓库里每个派生量一样：写在 `if` 里的阈值没法重放，而九月标成 `open` 的一条 debate
十二月要能解释得清。

## 三、来源独立性：同一家券商说两次不是两个来源

`source_identity()` 一条严格的阶梯，每一级都记 `basis`：

| basis | 来源 |
| --- | --- |
| `publisher` | 冻结表按**最长模式优先**匹配文档标题（`Morgan Stanley` 不会被读成 `jp morgan`） |
| `issuer` | filing / management tier：公司自己说了多少遍都是一个声音 |
| `host` | P9d-13 记在 discovered document 上的 URL host |
| `document` | 同一份文档里引三句是一个来源 |
| `unattributed` | 什么都建立不起来；**列出来，但永不计数** |

`unattributed` 不计数是保守方向：少算把一条薄 debate 留在 `candidate`，
多算会让一家券商说两遍就把它推成 `open`。

**Ledger 自己的 `independence_group` 被明确排除在阶梯之外**，这是读了 live 之后的决定：
它在 live 只有三个值——`independence:source:public-web`、`independence:source:alphaengine`、
`independence:source:sec-edgar`——它按**连接器**分组。用它，就等于断言两份互不相干的报纸是同一个来源，
和断言它们是两个来源一样错。「我们分不出来」才是真话，那是 `unattributed` 的意思。

## 四、起草与核验（`debate_map_draft.py`）

一次调用一家公司，用途 `debate_map`（`register_purpose` 在 import 时登记，`cockpit_model.py` 一个字节没改）。

**输入表**：canonical Claim 按 aspect 分组、每行带 **tier 与 publisher 标签**
（`sell_side` / `management` / `expert` / `sales_note` / `news` / `crowd` / `filing` / `other`），
确定性前置扫描（第五节）找出的争议 aspect 排在最前；再加 DRIVERS、编号的 QUESTION ADMISSION
与 CAUSAL CHAIN、OUR THESIS、PREVIOUS DEBATES。行号 `C1..Cn`、`T1`，**只能引这些行号**。

**prompt 里写死的四句**（都有测试断言原文在 prompt 里）：

- 「一条两边都同意的 debate 一文不值」；
- `market_position` 是 consensus 站在哪（卖方评级与目标价、sales-note 与 crowd tier、管理层自己的说法），
  **表里看不出来就 `available:false`，那是答案，不是猜**；
- `our_position` 是 OUR THESIS 让我们承担的立场，没有就 `none_yet`，**不许把市场抄进来**；
- `gaining`：自上一版以来哪一边在赢。

**整份拒绝，永不修补**：多一个 key、引一个没给它看过的行号、绑一个没给它看过的 driver、
`debate_ref` 既不是上一版的也不是 `new-<n>`、同一条 debate 出现两次、外面裹一句散文——
`parse_draft` 抛 `DebateDraftRefused`，整份丢掉，**第二次调用（verifier）根本不发生**。
一个凭空造出行号的回复说明它不是在读那张表，那么它碰巧答对的部分也是同一个回复的产物。

**debate 身份的连续性**：延续上一版的争论必须逐字复用那个 `debate_ref`（模型能看到它们）；
新的用 `new-<n>`，由 `(subject, drivers, fold(question))` 内容寻址成稳定 ref。
这是版本链能被读成「一个争论在移动」而不是「每周一批新争论」的原因；
`first_seen_at` 也因此能跨版本保留。

**独立 verifier**：第二次调用，只回 verdict + findings（六个封闭 code），
不重画也不改写。`independent()` 三条，**fail-closed**：

1. 必须是两次不同的调用；
2. 两边的 `model_family` 都必须能从 `model_route_decisions.selected_endpoint.family` 读出来——
   **读不出来就不发布**（「分不出来」和「不一样」绝不能产生同一个结果）；
3. 两个 family 必须不同。

family 由**路由权威**给，不由任何一次调用自称——这正是这条谓词唯一的价值所在。

`draft_debate_map()` 返回状态而不写库：`verified` / `refused` / `no_admitted_debates` /
`not_independent` / `verifier_rejected` / `unverified` / `model_unavailable`。
发布的决定留在 lane，机制留在 authority。

## 五、确定性前置扫描

`contested_aspects()`：按 P12b 的 `index_aspect` 分组，用冻结的极性词表把 Claim 分到 bull / bear，
两边都非空的 aspect 就是一个 seed，附带两边的独立来源数与 `would_open`。**不调模型。**

**故意不按 driver 分组**：把问题绑到 driver 是判断，而且正是闸门第一件要验的事；
规则去猜它，等于规则在发明闸门要验的那个东西。

极性词表故意短，而且故意不含在这个行业里两头读的词——`contract` 在别处是熊词，
在 IT 服务里大单是牛的理由；两边都会点着的线索找到的是不存在的 debate。

## 六、Lane（order 135）

`lane_registry.LANE_MODULES` **加一行**，`LaneSpec(operation="dispatch_debate_map", order=135,
driver_key="debate_map")`。选 135：在读取层（120 / 130）之后；
**故意不挨着 Claim 索引**——同一个 tick 里先打标签再对刚打完一半标签的那批争论，是在对半成品争论。

每 tick 一个 subject：mission universe 顺序，**行业排最后**（行业地图读的是各公司，
在被总结的东西之前画总结，顺序是反的）。选中的判据是**证据变了**：
该 subject 当前 canonical Claim 版本集合的 `evidence_fingerprint`
≠ 当前版记下的那个。无队列——「要不要重画」是关于一个**状态**的判断，不是待办事项。
静默是常态，也是正确答案：每 tick 都重画的 lane 会产出一条看不出任何变化的版本链，
正是 ADR-0008 要防的那件事。

失败按 `(subject, fingerprint)` 挂起（进程内），一条新 Claim 改变 fingerprint 就自动解挂；
`duplicate` / `gated` / `dry_run` **不算失败**（对一小时是对的，永远挂着就是错的）；
`busy` / `model_unavailable` 是关于此刻而非关于这个 subject 的，也不挂。

## 七、读取侧

| 入口 | 给谁 |
| --- | --- |
| `open_debates(subject)` | 周报。返回 `open` + `shifting`（在移动的争论仍然是开着的），**不返回 `candidate`** |
| `shifted_since(subject, version)` | 周报与 P14a 判断 prompt。接受版本号或版本 id（周报记得它引用过的 id，人记得号）；返回每条的 `from_status` / `to_status` / `last_shift_reason` / `new_refs`，以及消失的 debate |
| `debate_for_driver(driver_ref)` | 预测层：改一条 assumption 之前，先看它挂的那个 driver 上在吵什么 |

`current(subject)` 对没有地图的 subject 返回 `None`，`open_debates` 返回 `[]`——不是错误。

## 八、验收结果（原文）

```
Ran 2938 tests in 304.490s
OK (skipped=1)
```

命令：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`
（`PYTHONPATH` 必须绝对路径，理由同 P12b 报告第四节。）
基线 main `7708d43` 是 2,843，本片新增 **95** 项：
`tests/test_debate_map.py` 37、`tests/test_debate_map_draft.py` 36、`tests/test_debate_map_lane.py` 22。

## 九、冒烟：今日 live Core 只读副本（`/tmp` 拷贝，跑 B 的规则标签，不调模型）

```
claims pending index: 2170  (0.6s)
rule-settled aspects: 22  (2.1s)
subjects: 5      industry subjects: []

ACN   claims  487  tiers {'sell_side': 5, 'management': 8, 'news': 470, 'filing': 4}
                   basis {'document': 475, 'issuer': 12}
       production seeds 0   variant seeds 2   would_open 0
         new bookings                      bull 1/1src  bear 4/3src  open=False
         revenue growth by industry group  bull 1/1src  bear 1/1src  open=False
EPAM  claims  775  tiers {'management': 473, 'sell_side': 55, 'news': 243, 'filing': 4}
       production seeds 0   variant seeds 2   would_open 0
         ai-native revenue growth          bull 1/1src  bear 2/1src  open=False
         competitive positioning           bull 2/1src  bear 2/2src  open=False
CTSH  claims  456  tiers {'sell_side': 149, 'management': 15, 'news': 288, 'filing': 4}
       production seeds 0   variant seeds 1   would_open 0
         revenue growth guidance           bull 1/1src  bear 1/1src  open=False
DXC   claims  267  tiers {'management': 50, 'news': 213, 'filing': 4}
       production seeds 0   variant seeds 1   would_open 0
         bookings and book-to-bill         bull 1/1src  bear 1/1src  open=False
IBM   claims  183  tiers {'management': 25, 'sell_side': 8, 'news': 146, 'filing': 4}
       production seeds 0   variant seeds 0
total variant seeds across five companies: 6
```

**「production seeds」= 0，而且这是当前的真实答案。** 前置扫描按 P12b 的封闭 aspect 分组，
而 live 只有 22 条 Claim 有 aspect（规则能定的那些，全是定量、全落 `segments_and_mix`、
没有一条带极性）；其余 2,148 条的 aspect 要 P12b 的模型标注跑过才有，本片不对 live 调模型。
「variant seeds」是把 aspect 临时换成折叠后的自由文本 `metric_or_aspect` 的同一次扫描——
它说明 **P12b 的标注一跑完，前置扫描立刻有东西可给**：五家一共 6 个 seed。

四件事值得单独说：

1. **验收标准（ACN ≥3 条 debate、bull/bear 各 ≥2 个独立来源）今天的素材还不够。**
   ACN 487 条 canonical Claim 里只有 **5 条**是 sell-side tier，而 discovery 已经找到 885 份
   sell-side 文档——差距不在本片，在「研报还没被抽成 Claim」。EPAM（55 条）与 CTSH（149 条）好一些。
   本片的闸门会如实把这种 debate 标成 `candidate` 而不是 `open`，这正是 `candidate` 存在的理由。
2. **live 没有任何行业主体的 Claim**（`industry subjects: []`）。行业那条链今天没有证据可画。
   `is_industry_subject` 在 P12b 里存在，但 Ledger 里一条都没有。
3. **live Core 里没有文档标题**：`document_index_documents` 是可丢弃投影、不在 Core 里，
   `coverage_mission_document_reviews` 也不存标题。所以 publisher 那一级今天全空
   （`publishers: {}`），475 条落到 `document` 一级。**这不是设计缺口，是输入缺口**：
   标题一旦随 DocumentIndex 落到同一个 Core 上，冻结的 publisher 表立刻生效，
   「TD 的两份报告」就会从 2 个来源折成 1 个。见第十节。
4. **极性词表在 live 上的分布**：ACN 487 条里 bull 78 / bear 87 / neutral 322。
   三分之二是 neutral，这是保守方向的错，符合设计。

## 十、集成要接的线

1. **lane registry 已经接好**（`LANE_MODULES` 一行 + `LaneSpec`，order **135**）。
   `writer_server` / `bounded_planner_driver` / `macos_launchagent` 一个字节没改（Wave 0 的合同生效）。
2. **模型配置名**：lane 复用 `initial-screen-model-config.json`（与 model spec lane 同样的理由：
   两者都是判断不是抽取）。若要独立配额，`scripts/raise_day_budget_cap.MODEL_CONFIG_NAMES`
   加一个 `debate-map`，并把 `mission_debate_map_lane.DEBATE_MAP_MODEL_CONFIG` 指过去。本片没碰那个文件。
3. **`may_write`：`debate_map` 已在 `AUTOMATION_WRITE_SCOPES`（Wave 0 加的），live mission 尚未授予。**
   没有它，child 在花钱之前就 `held` / `not_authorized`。owner 发新版 mission 时授予即可，代码不用改。
4. **路由：verifier 必须能落到与起草不同的 model_family。** 本片 fail-closed——
   family 读不出来就不发布。集成时要确认 `debate_map` 用途的路由链里至少有两个 family，
   否则这条 lane 会一直停在 `not_independent`（这是**正确**的停法，但要有人知道为什么停）。
   更好的做法是让第二次调用带 `producer_family` 走 `ModelRouter.route`（路由器已经支持
   `family_independence_capabilities` 与 `model_family_not_independent`），本片没有改 `cockpit_model`
   的调用签名，所以走的是「事后从 route decision 读 family」这条路。
5. **DocumentIndex 与 Core 同库**：publisher 那一级要的是 `document_index_documents.title`。
   今天它不在 live Core 里（第九节 3）。接上之后 `_document_titles` 会自动把标题喂给冻结的
   publisher 表，一个字节代码都不用改。
6. **打包**：`debate_map_schema.sql` 靠 Wave 0 的 `*_schema.sql` 通配进包，本片没碰 `pyproject.toml`。
7. **驾驶舱**：公司卡可以读 `open_debates(company)`；周报读 `shifted_since(company, version)`。
   本片没碰 `cockpit_*`。
8. **P12b 的模型标注要先跑**，否则前置扫描按 aspect 分组会一直是 0（第九节）。
   起草本身不依赖 aspect（表照样有 Claim），但排序质量会差很多。

## 十一、没做的

- **不对 live 调任何模型**，所以起草与核验两条路只在假模型上跑过（三个测试文件里的 `FakeModel`）。
- **行业地图没有真实素材**（第九节 2），`subject_kind="industry"` 的路径只有测试覆盖。
- **`resolution` 的自动提出没有做**：模型可以提 `resolved` 并给出 resolving refs，
  闸门也会照办，但没有任何确定性检测器会去发现「这个争论已经被某份 filing 了结了」。
  那是 P14a 事件流的活。
- **`debate_for_driver` 是全表扫描**（每个 subject 读当前版）。live 五个 subject，无所谓；
  真要多起来，schema 里已经有 `debate_map_versions_by_subject` 索引，加一张 driver→debate 的
  投影表即可。
- **极性词表只有英文。** 中文素材（雪球、cn-hk-findata）进来之后要扩，扩的是策略文件不是代码。

## 十二、开放问题（需要 owner 或主 agent 定）

1. **`candidate` 要不要进周报？** 现在不进（`open_debates` 只给 `open` + `shifting`）。
   但「我们发现了一个问题，只是还没找到第二个独立来源」本身是**研究缺口**，
   可能值得单独一节。倾向：周报里作为「待补来源」列表出现，不与真 debate 并排。
2. **两个来源够不够？** 策略文件里 `min_independent_sources_per_side: 2`，
   与 Constitution 的 `source_standards.minimum_independent_sources`（live 是 1）**不是同一个数**。
   本片没有让它去读宪法那个字段，因为那个字段说的是「一条 Claim 要几个来源」，
   而这里说的是「一边要几个声音」，是两件事。要不要合并成一条治理，请 owner 定。
3. **verifier 的第二个 family 谁来配？** 见第十节 4。
4. **卖方研报什么时候被抽成 Claim？** 这是 P12c 验收（bull/bear 各 ≥2 独立来源）
   今天达不到的唯一原因（第九节 1）。素材已经在库里（885 份 sell-side 文档），
   缺的是抽取那一步。
5. **`gaining` 的诚实度没有确定性校验。** verifier 有 `shift_not_supported_by_refs`
   这个 finding code，但没有任何机械检查能判断「自上一版以来这一边确实在赢」。
   一个可能的做法：把 `shifting` 的判据改成确定性的——新增的 refs 里某一边的独立来源数
   比上一版多。要不要收紧，请主 agent 定。
