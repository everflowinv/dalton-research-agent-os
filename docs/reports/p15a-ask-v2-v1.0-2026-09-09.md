# P15a：问答 v2 —— 上下文、可核查的答案、一次有预算的补搜

日期：2026-09-09（v1.1：2026-09-10，按 review 修补搜路径与策略读法）
分支：`w3-ask-v2`（基于 main `ebd2ea8`；已并入 main `eb8e5fb`）
新增：`ask_context.py`、`ask_answer.py`、`ask_refresh.py`、`tests/test_ask_v2.py`、`tests/ask_v2_golden/`（5 例）
共享改动（additive）：`cockpit_plane.py` 的 ask 函数、`answer_routing.py` 的一个纯函数与一个只读读法、`tests/test_cockpit_plane.py` 的 ask 用例
蓝图对应：§3 ⑤「响应 PM」、§5.2 P15a；验收「PM 问 10 个真实问题 ≥8 个可用」

---

## 1. 一句话

问答从「四百条 Claim 按时间排一排」变成「按问题类型取遍所有权威、每一行带标签与出处、答案回来先过七道确定性检查再给人看」；
`answer_after_refresh` 这条 live 跑过 0 次的路，第一次有了一个 owner 能点的入口，而且它被两个版本化的人类动作卡着。

## 2. 自我反思：我们和市场差在哪，路径是什么

计划第 1 节要求每片交付都说清这一点。问答层的「市场」是**别的 AI 助手与卖方即时问答**，它们的共同做法是：
把检索到的片段丢给模型，答案带几个链接，信心靠语气。

我们差在三处，每一处都可被检查：

| | 市场的做法 | 我们的做法 | 为什么这不是装饰 |
| --- | --- | --- | --- |
| 引用 | 段落级链接，读者自己去核 | 每一句话带 `refs`，标签在 `refs` 里而不在正文里，正文由句子行拼装 | P13ap 的教训：把标签写进正文的交付物，剥掉标签后句子的主语跟着走了。live 的 Initial Screen 里有这种残句 |
| 「不知道」 | 用委婉语绕过去 | `unknowns[]` 每条带 `content_kind` + `source`，且用 `SourceCapabilityMap` 校验「这个来源真能给出这类内容」 | 「需要更多数据」不可执行；「缺 EPAM 四个季度的 bookings，是 `sell_side_report`，去 alphaengine 取」是一条可以直接变成检索的指令 |
| 看法 | 复述共识 | 看法类问题必须给 `market_vs_us` 五个槽位（复用 P12a 的 `VARIANT_SLOTS`），缺了就在答案上标记为缺 | owner 2026-09-09 的原话：跟市场同向没有价值。没有 `convergence_pathway` 的看法在这里是一个失败的检查项，不是一句遗漏 |

**市场向我们靠拢的路径**：这三件事都不是模型能力问题，是产品合同问题。谁先把「答案必须能被机械核对」写成合同，
谁的问答就能进入需要留痕的场景（投研、合规、尽调）。我们能提前观察到的信号是：通用助手开始给出结构化引用与
「我不知道 + 去哪取」的组合。在那之前，我们的护城河是**账本**，不是模型。

## 3. `ask_context.py`：上下文 v2

一次问答的上下文由 13 个块组成，每个块都出现在输出里，**没有内容时带一个封闭词表里的原因**：

```
no_authority_on_this_core   这个 Core 上根本没有这张表
no_record_for_this_company  表在，这家公司没有记录
no_company_resolved         这是按公司取的块，而问题没点名公司
reader_not_available        读取器还没合入（consensus / P11b）
dropped_for_budget          有内容，但字节预算没排到它
```

这五个词的区别是整片东西的地基：`unknowns` 与补搜决策都是从「哪个货架是空的、为什么空」推出来的。
「我们没建这个权威」和「这家公司没有这条记录」被混成同一个空白，答案就会告诉 owner「街上没人覆盖」，
而事实是没人去看过。

**块与标签**（标签字母各不相同，读者一眼能看出一句话靠的是 filing 还是我们自己的模型）：

| 标签 | 块 | 来源 |
| --- | --- | --- |
| `C` | 账本里的结论 | `claim_versions`，经 P12b 索引取 canonical，按 aspect 分组 |
| `D` | 公司档案 | `company_dossier_versions` 最新版的已起草小节 + `variant_view` |
| `B` | 还在争的问题 | `debate_map_versions` 里 `open` / `shifting` 的 debate，带市场立场与我们的立场 |
| `F` | 模型 | `forecast_model_versions`：driver + 未被取代的 assumption（带 `because` 与 refs）+ 每条 result 行的最新 estimate / actual |
| `V` | 估值 | `valuation_snapshot_versions`，分位与它的 `basis` 同句 |
| `P` | 股价 | `market_price_series_versions` 最新一根，盘中价标注为盘中价 |
| `S` | 一致预期 | 注入式 reader（P11b 未合入时为 `reader_not_available`） |
| `K` | 下一个日程 | `catalyst_calendar_versions`，确认 / 推算分开 |
| `E` `G` `R` | 事件 / 判断 / 反思 | P14a 的 `research_events`、`event_judgements`、`thesis_reflections` |
| `T` | 已立论点 | `thesis_versions` |
| `N` | 你上次的反馈 | `analyst_journal_entries` 里未被满足的 verdict |
| `H` | 刚补搜到的文件 | 只在 `ask_refresh` 的第二遍里出现 |

**为什么是直接 SQL 而不是各权威自己的 reader**：每个权威的 reader 类在构造时都要 `executescript` 自己的
schema，驾驶舱拿的是 `mode=ro` 连接。所以这里只 import 各权威的**纯函数与冻结词表**
（`section_body`、`LIVE_STATUSES`、`model_readiness`、`bar_is_provisional`、`map_ref_for`），
选择用 SQL 做。语义只有一份，取数是本地的。

**预算与优先级**（写死并被测试钉住）：

1. 问题、目标、公司名单永远不掉；
2. Claim 先按 45% 的**下限**收费（ADR-0006：答案来自 Claim，把预算全花在档案上就不再是「从账本回答」了）；
3. 其余块按固定顺序 `theses → dossier → debates → forecast → valuation → market_price → consensus → catalyst → events → judgements → reflections → journal`，
   由问题类型把**至多两个**块提到队首（`view` 提 dossier/debates，`valuation` 提 valuation/market_price，`outlook` 提 forecast/consensus，等等）；
4. 剩下的字节全部还给 Claim，从最新的往回收。

问题类型是 8 个封闭词（`view` / `valuation` / `outlook` / `event_impact` / `debate` / `catalyst` / `fact` / `other`），
按固定优先级用中英双语词表判定，**并且把命中的那个词一起返回**——判不出来的分类器等于要 owner 无条件信任它。
同一个问题、同一个 Core 出同一个 `context_hash`；`context_identity()` 被提成函数，
因为补搜的第二遍加了一个块之后必须**重算**它（review #6）——
多了一个块还沿用上一遍哈希的上下文，等于声称两次回答用的是同一批材料。

## 4. `ask_answer.py`：封闭输出 + 七道检查

模型返回的形状：

```json
{"sentences": [{"text": "...", "refs": ["C7","D2"]}],
 "confidence": "high|medium|low",
 "refused": false, "refusal_reason": null, "refusal_detail": null,
 "unknowns": [{"what": "...", "content_kind": "...", "source": "..."}],
 "market_vs_us": {"our_view": "...", "market_view": "...", "where_market_is_wrong": "...",
                  "convergence_pathway": "...", "observable_signals": "...", "refs": []},
 "refresh_suggested": {"content_kind": "...", "source": "...", "query": "..."}}
```

正文由句子行在这里拼装，标签从不进入正文。`confidence` 保持三个词——Q1 的 `confidence_stated`
就是按这三个词写的，加第四个词等于让一个更诚实的答案挂在确定性检查上。拒答因此不是一种信心，
而是 `refused: true` + 四个封闭理由之一（`no_evidence_shown` / `outside_coverage` /
`needs_refresh` / `question_not_understood`）+ 一句人话。

七道检查（每道带中文标签，结果随答案一起返回）：

| 检查 | 失败时会发生什么 |
| --- | --- |
| `cites_only_shown_rows` | 没展示过的标签被去掉并逐个列出 |
| `numeric_sentences_cite` | 带数字却没有引用的句子被点名 |
| `confidence_in_vocabulary` | 按 `low` 记，并说明为什么 |
| `unknowns_typed` | 内容类型或来源不在 `SourceCapabilityMap` 里，或来源给不出那类内容 → 这条缺口被丢掉 |
| `market_vs_us_present` | 看法类问题没写差别与路径，**或五格里有任何一格是空的（逐格点名）** |
| `refresh_names_a_capable_source` | 建议的补搜指向一个给不出那类内容的来源 → 建议被丢掉 |
| `refusal_states_why` | 拒答没有封闭理由或没有一句人话 |

**五格必须格格写满（review #3）**：报告先前只声称了这一点，检查并没有做。
现在 `market_vs_us` 少任何一格（`our_view` / `market_view` / `where_market_is_wrong` /
`convergence_pathway` / `observable_signals`）都是失败，并且 detail 里逐格点名
（「这几格是空的：市场向我们靠拢的路径」）。说了「我们不同意」却把路径留空，
只回答了 owner 半个问题。

**「未知」要被声明，不能只是写两个字（review #4）**：`market_vs_us` 带上 P12a 自己的
`market_view_available` / `market_view_reason`。为 false 时 `market_view` 被规整成
一句明说没有材料的话，并且**必须**给出 `market_view_reason`，否则 `market_view_reason`
也算一格空的。P12a 分这两个字段的理由在这里同样成立：
「我们不知道市场在哪」和「市场跟我们一致」必须长得不一样，
一个光秃秃的「未知」和一个编出来的市场看法在下游是分不开的。

**Q1 的 rubric 确定性层对每一个答案都跑**（不是等谁想起来去评分），结果放在 `result["quality"]`，
带 `recorded: false`。ADR-0006：答案是驾驶舱产物，永远不是 Claim；分数也是驾驶舱产物，
这个进程不碰任何 Core 写句柄，`QualityScoreAuthority` 一次也没被调用。

## 5. `ask_refresh.py`：一次，等它跑完，有预算，可重放

**它是什么**：一次 `run_mission_source_discovery`，**等这次检索自己的子进程跑完**，
拿到**这次发现记录里的**那些文件的标题，把它们作为 `H1..Hn` 加进上下文，再问模型一次。就这些。

**review 抓到的那个 bug（B1），以及它为什么最危险**：
`run_mission_source_discovery` 起一个子进程，返回的是**票据**，不是结果。
第一版拿到票据就立刻读 `mission_discovered_documents`（按活跃版本、`created_at` 升序），
于是拿到的是账本里**最老的、早就存在的**文件，然后把它们印在「刚补搜到的文件」下面。
这是这条路上最坏的一种失败：它看起来像成功。

改法有三处，缺一不可：

1. **轮询票据**。`mission_source_discovery_status` 每 0.5 秒问一次，最多 20 次
   （`STATUS_POLLS` / `STATUS_POLL_SECONDS`，在模块上读取而不是绑成默认参数，
   所以部署或测试改了就立刻生效）。子进程在这个界限内没跑完，结果就是 `pending`：
   写入端会在下一轮 tick 把 dispatch 结掉，这次问答如实说「补搜还在跑，先按已有材料回答」，
   **什么都不展示**。一个人在等答案，短界限加一句实话胜过一个转圈。
2. **文件来自这次的发现记录**。结掉的票据的 summary 里带 `discovery_ref`，
   那条记录的 `document_refs` / `new_document_refs` 就是**这次检索**返回的东西。
   按公司或来源筛选永远分不出「这次找到的」和「本来就有的」。新发现的排在前面。
3. **按 `mission_ref` 读，不按活跃版本读**。发一个新 mission 版本不能让上一版发现的行消失。

**补搜结果是四个封闭词**（每个带一句中文）：`found` / `empty` / `pending` / `failed`。
`ran` 在**票据起来了就为真**，包括 `empty`——「我们去看了，没有新东西」和「我们没去看」
是两句不同的话。`empty` 与 `pending` 都不会再花第二次模型调用：没有新材料就没有新答案。

**它刻意不是什么**（不变）：

- **驾驶舱不写检索词**。query 由 owner 已发布的 discovery plan（`{terms} research report`）
  加公司检索词编出来；模型的话只作为 `query_intent` 记在答案旁边，永远不发给 connector。
- **规格从账本读**：这个 mission（按 `mission_ref`，跨版本）已经跑过的规格，优先同公司的那条。
- **不读正文**，**不写 Claim**，**同一个 `request_id` 不补第二次**。

**闸门**（每一个都返回封闭词与一句人话，且一次报全）：

```
mission_does_not_grant_research_task   mission 的 may_write 里没有 research_task
mission_budget_leaves_no_adhoc_pool    C2 的 adhoc 池（日预算 25%）上限是零
adhoc_pool_spent_today                 上限不是零，但今天已经被专项研究预定光了
policy_unavailable / adhoc_route_disabled / adhoc_budget_is_zero
source_not_searchable / source_not_connected
no_discovery_spec_used_yet / company_not_resolved / already_refreshed
answer_suggested_no_refresh
```

**预算按余额判，不按上限判（review #5）**：`pool_balance` 复用 P14e 自己的
`research_task.pool_state`（`BoundedPlannerAuthority.admitted_loops` 借到只读连接上，
不复制那条 SQL），给出 `cap_micros` / `reserved_micros` / `remaining_micros`。
上限是 mission 预算的属性、永远不动；**只有余额能说不**，而 25% 这个份额就是为
「今天的专项研究已经把它花完了」这一种情况存在的。
一个从没跑过 P14e 的 Core 没有 loop 表，那是**满的池子**而不是读不到的池子；
只有解析不了的预算才回 `None`，而 `None` 从来不是许可。

**`answer_routing.py` 的三处改动**：

1. `adhoc_research_route_available` 从字面量 `False` 变成新增的纯函数
   `adhoc_route_available(policy, mission)`——策略给了预算 **且** mission 授了 `research_task`。
2. **合同层的硬禁用被解除**（这是让上面那条能为真的前提）：`_route_budget` 里
   「ad-hoc research must remain disabled in S5 v0.2」会拒掉**任何**启用了 adhoc 路由的策略，
   也就是说在此之前根本发不出一份能给补搜预算的策略。这正是 owner 2026-09-09 解除、
   而代码里一直没有拿掉的那条禁令。替换它的规则与 refresh 路由的完全一样：
   启用就必须有正的 `max_cost_units` 与 `max_rounds`，停用就不许留预算。
   **v0.1 合同保留原禁令**，因为那一版是已经发布并被读过的。
3. `read_policy` / `policy_state` / `active_policy_for_company` 三个模块级只读函数
   （见下一节）。

## 5.1 策略必须是路由自己读的那一份（review B2）

第一版的 `_answer_policy` 自己写了一遍投影，只问了「有没有 pointer 行」，
漏掉了 `_policy_state` 的**三个条件**：pointer 的哈希要对得上、策略要是写给**这一版** mandate 的、
现在要落在生效窗口内。漏掉它们意味着一个问题可能在 owner 早已作废的策略下花掉 connector 配额。

改法是把 `_policy_state` 与 `policy()` 从 authority 上**提成模块级函数**
（`policy_state` / `read_policy`），authority 的方法改成一行委托，
再加一个 `active_policy_for_company(connection, company_ref, as_of)`：
解析活跃 mandate（借 `AgendaStore.active_mandates` 到只读连接上，同样不复制 SQL），
挑出覆盖这家公司的那一个，然后调同一个 `policy_state`。
返回路由自己的三个词 `unavailable` / `stale` / `active`，答案把它原样带出来
（`answer_policy.state` / `.reason`）——因为「策略过期了」和「根本没有策略」是两种不同的修法。

一处实测发现：live 的 mandate 的 `scope_refs` 是 `industry:us-it-services`，不是每家公司。
所以「公司必须字面出现在 scope_refs 里」会把唯一那份 mandate 下的每家公司都拒掉。
规则改成：优先取覆盖这家公司的 mandate；没有匹配但只有一份活跃 mandate 时用它
（行业 mandate 管着行业里的公司）；有多份且都不匹配才是真的缺口。

## 6. 只读实测：今天这 10 个问题会拿到什么

对 live 只读副本（`/private/tmp/dalton-ro/core.sqlite`，2026-09-09 12:23，2,170 条 Claim、2 条 Thesis）
跑上下文装配，**没有任何模型调用**。这个 Core 上 Wave 1/2/3 的表**一张都没有**。

活跃 mission：`coverage-mission:us-it-services` v13，`may_write` 里**没有** `research_task`。

**补搜在 live 上今天为什么一定是灰的**（v1.1 复测，只读）：

| 闸门 | live 现状 |
| --- | --- |
| mission 授权 `research_task` | **没有**（v13 的 `may_write` 里没有这个词） |
| 回答充分性策略 | **一份都没有**（`active_policy_for_company` → `policy_unavailable`） |
| 合同层允许 adhoc 路由 | 本片之前**不允许**：`_route_budget` 会拒掉任何启用它的策略（已在本片解除） |
| adhoc 池 | 有额度：日预算 $100 → 上限 $25，今天预定 0 |
| 跑过的检索规格 | 有：`management-changes` / `industry-demand` 等（web-search），多家公司 |

也就是说 owner 要打开这扇门需要**两个发布动作**：给 mission 加 `research_task`，
以及发一份 `adhoc_research_route.enabled = true` 的回答充分性策略。
第三件（合同层的硬禁用）这一片已经拿掉了。

| # | 问题 | 判定的类型 | 今天会拿到的块 | 缺的块 | 预期 confidence |
| --- | --- | --- | --- | --- | --- |
| 1 | 你怎么看 ACN 现在的位置？ | `view` | 结论 302 条、论点 | 10 块 `no_authority_on_this_core`、1 块 `reader_not_available` | low（且 `market_vs_us` 的 market_view 只能写「未知」） |
| 2 | ACN 的估值现在贵不贵？ | `valuation` | 结论 302 条、论点 | 同上 | low（估值块与股价块都不在） |
| 3 | EPAM 下个季度的收入增速你预测是多少？ | `outlook` | 结论 291 条、论点 | 同上 | low（模型块与一致预期块都不在） |
| 4 | ACN 上季度的新签订单是多少？ | `fact` | 结论 302 条、论点 | 同上 | medium（大概率 `refused: needs_refresh`，账本里没有 bookings） |
| 5 | 市场对 IT services 的需求分歧在哪？ | `debate` | 结论 302 条、论点 | 同上 | low（DebateMap 不在） |
| 6 | ACN 下次财报是什么时候？ | `catalyst` | 结论 302 条、论点 | 同上 | low（日历不在） |
| 7 | GCC 自建团队对 ACN 有什么影响？ | `event_impact` | 结论 302 条、论点 | 同上 | medium（有 risk-factor Claim 直接支撑） |
| 8 | EPAM 的利用率最近怎么样？ | `fact` | 结论 291 条、论点 | 同上 | medium（有利用率 Claim） |
| 9 | 我们和市场在 ACN 上的看法差在哪？ | `view` | 结论 302 条、论点 | 同上 | low |
| 10 | 这个行业现在处在周期的什么位置？ | `other` | 结论 302 条、论点 | 同上 | medium |

第 1 题的上下文：花掉 89,800 / 90,000 字符，渲染成 80,546 字符的 prompt，展示 304 行，
`duplicates_dropped: 0`（这个 Core 没有 Claim 索引，所以没有一条被判定为重复）。

**结论要说直白**：`≥8 个可用` 的验收**今天在 live 上过不了**，而且原因不在这一片。
10 个问题里有 6 个的答案取决于 Wave 1/2 的权威，而 live 部署上一张都没有。
这一片把「答不了」变成了**可执行的答不了**——每个空白带原因、每个缺口带内容类型与来源。
把 Wave 1/2 部署上去（价格、估值、索引、模型、档案、DebateMap、日历、事件流），
同一批问题的块覆盖会从 2 块变成 11 块，届时再跑这张表就是真正的验收。

## 7. INT2 需要改的 `cockpit_control.html`

我没有碰这个文件。`renderAnswer(a, r)`（第 563 行）需要五处**增量**改动；
**答案的旧字段全部保留**（`answer` / `citations[].statement` / `.company` / `.period` / `.at` /
`gaps` / `confidence` / `claims_considered` / `duplicates_dropped` / `cost_usd` / `replayed` / `feedback`），
所以现在的页面**不改也不会坏**，只是看不到新东西。

1. **`market_vs_us`**（`r.market_vs_us`，可能为 null）：五个槽位各一行，标题用
   「我们的看法 / 市场的看法 / 市场错在哪 / 市场向我们靠拢的路径 / 可观察的信号」。
   建议给它自己的底色——这是 owner 点名要看的东西。
2. **`unknowns`** 取代现在的 `gaps` 行：每条渲染成
   `缺什么 · 那是「内容类型」· 去「来源」取`。`gaps` 仍然存在（由 unknowns 拼成），
   所以两种渲染都能工作，但 `unknowns` 是可点的那一个。
3. **补搜按钮**：`r.refresh.available` 为 true 时显示「去补搜一次」，点击后用同一个
   `request_id` 再调 `POST /v1/cockpit/ask` 并带 `refresh: true`；
   为 false 时把 `r.refresh.reason_labels` 直接印出来（都是中文整句），
   并且在有 `r.refresh.suggestion` 时印「它想去 X 取 Y」——**闸门关着时这句话最有用**，
   它就是让 owner 去发新 mission 版本的那句话。
   注意这次请求会**同步等最多 10 秒**（子进程的轮询界限），按钮要有 pending 态。
4. **`refreshed_with` 与 `r.refresh.status`**：补搜跑过之后，`status` 是四个词之一，
   `status_label` 是可以直接印的中文。
   - `found`：列出文件标题 + host，并把 `r.refresh_note`（「只拿到标题，正文还没被读」）
     原样印在旁边。**不要把它们渲染成证据**。
   - `empty`：印「补搜跑完了，这个来源没有新的东西」——不要退回成「没补搜」。
   - `pending`：印「补搜还在跑，这次先按账本上已有的材料回答」，并提示稍后再问一次。
   - `failed`：印 `status_label` 加 `r.refresh.reason`。
5. **`r.answer_policy`**：`state` 不是 `active` 时，在补搜被拒的理由旁边补一句
   （`policy_stale` = 「回答充分性策略过期了」，`policy_unavailable` = 「这个账本上没有策略」）。
   这两句指向的是两种不同的修法。
6. **`verification` 与 `quality`**：`r.verification.passed` 为 false 时，
   把 `checks` 里 `status == "fail"` 的 `label` 列出来（例如「有一句带数字的话没有出处」）。
   `r.quality` 与公司卡上已有的 `qualityBlock(doc.quality)` 形状不同（它是
   `run_deterministic` 的原始结果 + `rubric` / `rubric_ref` / `recorded`），
   建议复用 `QUALITY_CHECK_LABELS` 的中文标签，只印 checks 那一段。
7. 另外，`r.citations[].block_label` 可以印在每条依据后面（「账本里的结论」/「估值快照」/「我们的模型」），
   这样读者能看出一句话靠的是 filing 还是我们自己的估计。

请求体的唯一新字段是 `refresh`（bool，缺省 false）。**没有新路由。**

## 8. 顺手发现的问题

1. **P12a 现在发不出「已起草的 variant_view」**。`company_dossier.validate_variant_view` 的
   drafted 分支返回值里**没有 `gaps` 键**（unavailable 分支有）。输入的闭合形状要求 `gaps` 在，
   输出把它丢了，于是 `body_hash(record)` 与 publish 时算的 `digest` 对不上，
   authority 用 `company dossier body_hash is not its body` 拒掉自己的记录。
   现有 dossier 测试全部用 `variant()` 的 unavailable 默认值，所以没有暴露。
   **影响 P15a 的核心功能**：`market_vs_us` 最好的素材就是档案的 variant view。
   一行修复（在 drafted 分支的返回值里加回 `"gaps": _gaps(wire["gaps"], f"{name}.gaps")`）。
   我在 `DossierVariantReaderTests` 里用手工构造的行测了读取端，等这条修了就能端到端。
2. **`coverage_mission_source_discoveries` 只按 mission version 索引**。任何按 mission version
   读历史 discovery 的读者，在 owner 发新版本的当天会看到空。我在 `known_specs` 与
   `_discovered_by` 里都用 join 按 `mission_ref` 绕过了；如果别处也这么读，值得一并检查。
3. **合同层还禁着 adhoc 路由**（我改了，见第 5 节）：`answer_routing._route_budget` 里
   「ad-hoc research must remain disabled in S5 v0.2」拒掉任何启用 adhoc 路由的策略。
   owner 2026-09-09 解除了这条禁令，但代码里没人拿掉，于是
   `adhoc_research_route_available` 无论 mission 怎么写都不可能为真。
   这是补搜这条路上真正的最后一道墙。
4. **live 的 mandate 按行业而不是按公司划范围**（`scope_refs = ["industry:us-it-services"]`）。
   任何按「公司是否字面出现在 scope_refs 里」找 mandate 的读者，在 live 上会对每家公司都失败。

## 9. 验收：全量测试

第一轮（改动前，main `ebd2ea8`）：

```
Ran 3932 tests in 538.942s

OK (skipped=1)
```

v1.0（本片首次交付）：

```
Ran 3988 tests in 623.792s

OK (skipped=1)
```

v1.1（并入 main `eb8e5fb` 之后，含 review 修复）：

```
Ran 4467 tests in 596.760s

OK (skipped=1)
```

新增测试：`tests/test_ask_v2.py` 73 项——问题判定、旧 Core、全表 Core、预算、
答案形状（含空格逐格点名、`market_view_available` 的两种写法）、
补搜闸门与池余额、补搜执行的四种结局（`found` / `empty` / `pending` / `failed`）、
`known_specs`、`adhoc_route_available`、5 例 golden、以及驾驶舱端到端。

**端到端走的是真的票据生命周期**（review 要求）：`run_mission_source_discovery` 与
`mission_source_discovery_status` 两个 op 在测试里按 `writer_server` 自己的写法跑
——真的 `authorize_source_discovery`、真的 `record_discovery_dispatch`、
真的 discovery 记录（过 `validate_mission_source_discovery`）——
唯一被替换的是子进程本身。账本里事先放了一条**上周**同公司同来源的发现与它的文件，
测试断言补搜返回的只有**这次**找到的那一条。第一版会返回上周那条。

**golden 的位置说明**：5 例放在 `tests/ask_v2_golden/`，不是 `tests/golden/ask_answer/`。
原因是 Q1 的 `tests/test_research_quality_golden.py` 有两条形状断言：
`tests/golden/` 下的目录集合必须**恰好等于** `RUBRIC_ALIASES`，且每个 rubric 的例数在 **5–10** 之间；
`ask_answer` 已经有 7 例，再加 5 例就是 12。要放进去必须把那个上界从 10 改成 12，
而那是 D 线的测试文件、不在我的所有权范围内。这 5 例跑的是**真实的两层**：
本模块的 7 道检查，以及 Q1 `ask_answer` rubric 的确定性层（`run_deterministic`），
两层的每一项状态都被钉住。

## 10. 没做的事

- **没有做 P15b（事件影响问答）**：`event_impact` 类问题现在拿到的是事件块与判断块，
  不是 thesis-impact 生产者与 verifier 的结论。那是下一片。
- **没有做 Feishu / Discord 投递**（P15e），没有碰周报。
- **没有做「两次补搜」或「补搜后再抽取」**：抽取是 lane 的活，补搜只到标题为止。
- **没有实现 consensus reader**：接口留成注入式的 `consensus_reader`，
  w2-consensus 合入后接一行；未接时块是 `reader_not_available`，不是 `no_record`。
- **没有碰 `cockpit_control.html`、`writer_server.py`、`coverage_mission.py`、
  `bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、`PROJECT_STATUS.md`。**

## 11. 需要 owner 或主 agent 定夺的

1. **要不要打开补搜？** 需要两个发布动作：(a) 给 live mission 的 `may_write` 加 `research_task`；
   (b) 发一份 `adhoc_research_route` 启用且有正预算的回答充分性策略（live 现在一份都没有）。
   打开之后，一次问答最多花：一次发现调用（AlphaEngine 占 130/24h 的配额，或 web-search 占它的）
   + 一次额外的模型调用，都在 mission 日预算与 adhoc 池（$25/天）之内，且同一个问题只补一次。
   不打开也没关系：答案照样会说「它想去哪取什么」，那句话就是让人决定要不要发这两个版本的依据。
2. **补搜的 spec 选择规则**：现在选「这个 mission 最近跑过的、优先同公司的那条」。
   另一个选择是让答案的 `content_kind` 决定（`transcript` → 电话会规格，`sell_side_report` → 研报规格），
   但那需要驾驶舱能看到 discovery plan 的 `document_type`，也就是一个新的 writer 只读 op。
   我选了不加 op 的那条；如果要更准，这是需要拍板的地方。
3. **答案要不要进 Ledger？** 现在坚持 ADR-0006：不进。但 `refreshed_with` 里的文件是真的被发现了，
   discovery 记录进了 Core（那是 discovery 权威自己写的，不是问答写的）。
   如果以后要让「问答触发的发现」和「lane 触发的发现」在账本上区分开，需要 discovery 记录上加一个来源字段。
4. **golden 目录**：合并进 `tests/golden/ask_answer/`（要动 D 的测试上界）还是保持独立（见第 9 节）。
