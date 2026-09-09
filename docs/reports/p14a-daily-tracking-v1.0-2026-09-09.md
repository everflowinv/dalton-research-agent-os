# P14a 每日跟踪、ResearchEvent 与事件判断 lane v1.0

日期：2026-09-09
分支：`p14a-daily-tracking`（worktree `~/Projects/dalton-p14a-daily-tracking-worktree`），基线 main `2fa5934`
作者：P14a agent（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md)「Daily tracking：Initial Screen 过闸后默认开启」与 owner 三批追加指令、[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) §3 ④ / §5.2 P11d·P14a-c、ADR-0007 / ADR-0008、[P11a 市场层](p11a-market-layer-v1.0-2026-09-09.md)、[P13-M2 预测行](p13-m2-forecast-lines-v1.0-2026-09-09.md)、[P14e 专项研究](p14e-research-tasks-v1.0-2026-09-09.md)

全量测试：

```
Ran 2746 tests in 328.113s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`；基线 main `2fa5934` 是 2,512，本片 +234。）

---

## 0. 一句话

Dalton 现在**每天都在看**：过闸的公司自动进入常驻跟踪，价格异动、新文档、新 Claim、对账结果、日历到期全部收敛成一个可寻址的 `ResearchEvent`；每个未判定事件由一次有界模型调用给出 Playbook 的五词决定之一 + 六个动作之一，经独立核验后才落效果；决定「不动」也留痕；而当我们改主意、或价格持续与我们的判断相反时，还会写下一条 `ThesisReflection`——我们当时怎么想、发生了什么、可能漏掉了哪个 debate、什么可观测量会把市场拉向我们这一边。

---

## 1. 文件清单

| 文件 | 性质 | 是什么 |
| --- | --- | --- |
| `research_event.py` + `research_event_schema.sql` | 新增 | `ResearchEvent` 权威：append-only、`content_hash`、`dalton_authorized()` 三触发器、按 (company, kind, payload_hash) 幂等；`record_event()` 入口；三个无游标的 emitter |
| `market_event.py` | 新增 | P11d 异动检测 + P14a 背离检测。纯函数，读价格版本、出事件 body，不写库 |
| `tracking_cadence.py` + `tracking_cadence_schema.sql` | 新增 | `TrackingCadenceVersion` 权威、`next_due()` / `due_sources()`、常驻成员判定、`active_coverage` 阶段写入 |
| `source_capability_map.py` | 新增 | 确定性投影：每个连接器能给什么内容、什么证据层级、什么市场、什么额度、什么基线频率、是否通用源 |
| `event_judgement.py` + `event_judgement_schema.sql` | 新增 | 判断层：prompt、封闭输出契约、独立 verifier、`EventJudgement` / `ThesisRevisionCandidate` / `ForecastRevisionProposal` / `ThesisReflection` 四张表、效果分派、`event_response` 预算池 |
| `mission_tracking_lane.py` + `tracking_lane_cli.py` + `tracking_lane_launcher.py` | 新增 | 常驻 lane（order 87） |
| `mission_event_judgement_lane.py` + `event_judgement_cli.py` + `event_judgement_launcher.py` | 新增 | 判断 lane（order 115） |
| `deploy/phase9/p14a-tracking-policy-v1.json` | 新增 | 九条基线频率、异动与背离阈值、即时拉取规则、thesis 方向默认值 |
| `scripts/run_p14a_tracking_smoke.py` | 新增 | 只读冒烟 |
| `tests/p14a_fixtures.py` + 七个测试文件 | 新增 | 234 项 |
| `metric_base.py` | 改（加法） | `STAGE_SPINE["active_coverage"]` 第一次非空 |
| `lane_registry.py` | 改（两行） | `LANE_MODULES` |
| `mission_deliverable.py` + `mission_deliverable_schema.sql` | 改（加法 + 一次窄迁移） | `DELIVERABLE_KINDS` 加 `event_note` |
| `tests/test_mission_market_price_lane.py` | 改（一条断言） | 相邻断言改成序关系（见 §9） |

**没有碰**：`writer_server.py`、`coverage_mission.py`（及其 schema）、`bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、cockpit 两个文件、`PROJECT_STATUS.md`、`tests/test_service.py`、`tests/test_lane_registry.py`。

未推送。未部署。未写 live 状态。未发布 mission 版本。

---

## 2. 对象形状

### 2.1 `ResearchEvent`

```json
{
  "schema_version": "0.1",
  "id": "research-event:bdc1250b85ee19719ada6b1d584a4603",
  "created_at": "2026-09-09T12:00:00.000000+00:00",
  "company_ref": "company:sec-cik:0001467373",
  "kind": "price_move",
  "occurred_at": "2026-09-02T00:00:00.000000+00:00",
  "evidence_tier": "market_price",
  "source_refs": ["market-price-series-version:1", "connector-invocation:yfinance:…"],
  "payload": {
    "as_of": "2026-09-02", "close": "104", "previous_close": "100",
    "return_percent": "4.0000", "direction": "up",
    "basket_return_percent": "0.0000", "excess_vs_basket_percent": "4.0000",
    "basket_members": 4, "benchmark_ref": "company:benchmark:SPY",
    "benchmark_return_percent": null, "excess_vs_benchmark_percent": null,
    "trigger": "absolute,excess_vs_basket", "threshold_percent": "3.0",
    "price_version_ref": "market-price-series-version:1", "invocation_ref": "…"
  },
  "payload_hash": "…",
  "mission_version_ref": "coverage-mission-version:us-it-services:13",
  "mission_version_hash": "…",
  "actor_ref": "automation:coverage-mission",
  "content_hash": "…"
}
```

- **`id` 是内容的函数**，不是时间的函数：`research-event:{sha256({company, kind, payload_hash})[:32]}`。同一个事实读两次是同一个事件。库里有 `UNIQUE(company_ref, kind, payload_hash)`，所以幂等由存储保证而不是由「谁记得检查」保证。
- **`kind` 十二个，封闭**：`price_move`、`price_divergence`、`news`、`filing`、`transcript`、`rating_change`、`calendar`、`reconciliation`、`claim`、`sales_note`、`crowd_post`、`expert_excerpt`。后三个是 owner 第二批指令加的——sales note、X、专家纪要说的是**市场怎么看**，和卖方报告不是一回事，塞进 `news` 就把它们被读的理由丢掉了。
- **`payload` 按 kind 封闭且有界**：字段集合逐 kind 冻结，缺的补 `null`（所以哈希稳定），多的直接拒绝，值只能是 text / int / bool / null。嵌套被拒绝：事件账本是「发生了什么」的索引，不是原文的第二份拷贝。
- **`evidence_tier` 十档**（`primary_filing` → `crowd`），从 spec 优先、source 兜底推导；spec 比 source 更具体（同一个 AlphaEngine 库既有卖方报告又有管理层纪要）；两个都不认识就是 `news` / `news_media`——不认识不等于可以升格。
- **`source_refs` 不能为空**：没有来源 ref 的事件无法核验，直接拒绝。

### 2.2 `TrackingCadenceVersion`

```json
{
  "schema_version": "0.1",
  "id": "tracking-cadence-version:…",
  "cadence_ref": "tracking-cadence:{sha256({company, source})[:32]}",
  "version": 2, "prior_version_ref": "tracking-cadence-version:…",
  "company_ref": "company:sec-cik:0000051143", "source_key": "alphaengine",
  "interval_seconds": 259200, "baseline_interval_seconds": 43200,
  "because": "three AlphaEngine searches in a row returned nothing this company did not already hold",
  "evidence_refs": ["research-event:…", "research-event:…"],
  "change_reason": "driver_event", "decision": "NO_CHANGE",
  "policy_ref": "tracking-policy:p14a:v1", "policy_hash": "…",
  "mission_version_ref": "…", "actor_ref": "automation:coverage-mission",
  "content_hash": "…"
}
```

- 版本链，永不就地改。`change_reason` 用 ADR-0008 的封闭词表，`evidence_refs` 必须非空。
- 三种 `duplicate`：值与证据都没变；第一版与基线相同；同一 (company, source) 重复提同一件事。
- **policy 标 `adjustable: false` 的源不可改**（价格、SEC）：大脑定频率，不定「要不要看」。

### 2.3 `EventJudgement` / `ThesisRevisionCandidate` / `ForecastRevisionProposal` / `ThesisReflection`

`event_judgements` 有 `UNIQUE(event_ref)`：**同一个事件永远只被判一次**，由列保证而不是由循环记忆保证（循环只活一个进程，账本比它活得久）。记录里有 `decision` / `action` / `driver_refs` / `thesis_refs` / `because` / `citations` / `note` / `research_question` / `forecast_change` / `verifier{verdict, findings, independence}` / `model` / `verifier_model` / `effect`，以及事件 ref + 事件 hash + 两次模型 work order。

`thesis_revision_candidates` 是 ADR-0007 的形状：绑一个 `ThesisVersion`（ref + hash）、五词决定之一、`falsifier_ref`、`proposed_statement`（可空）、`proposed_confidence`（`low/medium/high`，float 被拒）、非空 `evidence_refs`、`checkpoint_kind: thesis_revision_candidate`、以及 **`reflection_ref`**（见下）。它不带任何修改权。

`forecast_revision_proposals` 是 mission 没授 `forecast_line` 时 `revise_forecast` 的落点：`{driver_ref, period_end, proposed_value, decision, because, evidence_refs, checkpoint_kind: forecast_overturn, reason}`。

`thesis_reflections`（owner 第三批指令）：`UNIQUE(judgement_ref)`，字段
`{trigger_event_ref, trigger_event_hash, trigger_kind ∈ {revision, price_divergence}, company_ref, decision, action, thesis_refs[], what_we_expected, what_happened, why, citations[], missed_debates[{question, refs[]}], followup_tracking[{source_key, interval_seconds, because}], followup_research[{question, wants}], market_view_vs_ours{available, our_direction, summary, refs[]}, convergence_pathway, verifier{…}, model{…}, verifier_model{…}}`。
**它不改任何 thesis，也没有改的路径**；它挂在 `ThesisRevisionCandidate` 上，人一次看到两样东西。

---

## 3. 频率政策（`deploy/phase9/p14a-tracking-policy-v1.json`）

| source_key | 基线 | 大脑可调 | 理由（policy 原文摘要） |
| --- | --- | --- | --- |
| `yfinance` | 6 小时 | ❌ | 一个交易日一根收盘价加一次盘中 provisional；价格 lane 本来就把「没新增交易日」的公司搁置 6 小时 |
| `sec` | 24 小时 | ❌ | 8-K 与财报日历每天查，没人去找的 filing 是覆盖分析师唯一不能漏的事件 |
| `alphaengine` | 12 小时 | ✅ | 每天 2 次；覆盖薄的公司应当被拉长到 2–3 天，同样四份文档搜十次是白花额度 |
| `x-xreach` | 12 小时 | ✅ | 每天 2 次取街上的说法，经 web search 验证后才当事实读 |
| `sales-notes` | 12 小时 | ✅ | 台里的 note 一天来两次，是「街上在吵什么」最早的一手，比后来登在研报上的同一个论点早 |
| `gemini-web-search` | 12 小时 | ✅ | 通用源；crowd post 的验证路径与公司新闻的兜底 |
| `guidepoint` | 7 天 | ✅ | 专家库周转慢、每次读贵 |
| `company-wiki` | 7 天 | ✅ | 基金自己的档案，有人写才变 |
| `employee-reviews` | 7 天 | ✅ | 招聘与士气按季度动 |

**异动阈值**（`abnormal_move`，任一命中即触发）：绝对 3.0%、相对等权 basket 2.5%、相对 benchmark 3.0%，`min_basket_members: 3`，`benchmark_refs: ["company:benchmark:SPY"]`，回看 5 个已结算交易日。

**背离阈值**（owner 第三批）：窗口 10 个已结算交易日，累计相对 basket **逆着 thesis 方向** ≥ 6.0%。每公司每窗口只发一次（lane 用「窗口内已有 `price_divergence`」抑制），否则一个持续两周的背离会每天发一条哈希不同的事件，幂等一条都拦不住。

**即时拉取**：`price_move` 触发后 6 小时内，`alphaengine` / `x-xreach` / `gemini-web-search` 三个源脱离基线立刻可拉。窗口很短是故意的：上周的事件不能证成今天的一次拉取，没有过期的触发器会让频率永久失效。

**thesis 方向**：`thesis_stances.default = "long"`，overrides 为空。理由写在 policy 与代码里：本基金是 fundamental long-biased（蓝图 §1 owner 原话），覆盖 thesis 就是持有这只票的理由；从 `implied_expectation` 的散文里猜方向正是这套代码在检测器里拒绝做的事。空头 thesis 是一行 policy，不是一次改码。

---

## 4. 判断契约

**输入**（一次调用，一张表）：事件 payload 逐字段 + 证据层级 → 公司在场的 thesis（statement / mechanism / confidence）→ 预测 driver 与最近三期假设 → 最近 5 条已判决定 → 该公司最近的 canonical Claim → **来源能力表**（哪里能拿到什么）。加一条常驻指令：*和市场看法一致等于没有看法；说清我们的看法和价格与街上的看法差在哪，以及什么可观测量会把市场拉过来；如果事件是 `price_divergence`，价格一直在打我们的脸，说漏了什么而不是复述 thesis*。

**输出**（封闭，整条拒绝，不修补）：

```json
{"decision": "NO_CHANGE|THESIS_STRENGTHENED|THESIS_WEAKENED|THESIS_BROKEN|NEW_THESIS",
 "action":   "no_change|note|research|revise_forecast|revise_thesis|revise_dossier",
 "driver_refs": [], "thesis_refs": [], "because": "…", "citations": []}
```
外加 `note`（当且仅当 action=note，≤4 句）、`forecast_change`（当且仅当 revise_forecast）、`research_question`（当且仅当 research）。

**决定 × 动作的兼容表是冻结的**：
`NO_CHANGE → {no_change, note, research}`；`THESIS_STRENGTHENED / THESIS_WEAKENED → {note, research, revise_forecast, revise_thesis, revise_dossier}`；`THESIS_BROKEN → {research, revise_forecast, revise_thesis}`；`NEW_THESIS → {research, revise_thesis}`。
「NO_CHANGE，所以改 thesis」是合同层的拒绝，不是下游读者需要留意的事。

**拒绝的每一种**：多一个 key、少一个 key、第六个决定词、不兼容的组合、引用 prompt 里没出现过的 ref、点名本模型没有的 driver、非 `no_change` 却不引用任何东西、note 超过四句、要 note 的时候没给 / 不要 note 的时候给了、返回散文而不是 JSON。全部是 `refused`（整条），事件保持未判定。

**独立 verifier**：第二次调用只回 `{verdict, findings[{code, detail}]}`，code 五个封闭词。两次调用的 `model_family` 从 broker 实际走的 route decision 上读（`ModelRouter.get_decision(...)["selected_endpoint"]["family"]`），不是从配置里断言的；**解析不出来或相同就 fail closed**——可能是同一个模型的核验者不是核验者。verdict 不是 `pass` → 不落效果、不记判决、事件仍未判定，理由与 findings 进 lane summary。

**效果**（唯一调用机制层入口的地方）：

| action | 做什么 | 做不到时 |
| --- | --- | --- |
| `no_change` | 记决定与理由（周会问「为什么没改主意」的原料） | — |
| `note` | `MissionDeliverableAuthority.publish(kind="event_note")`，走既有 `deliverable` 授权；每公司一条版本链，每条 note 一版 | 无数字来源的句子被权威拒绝（要求写 `缺来源`），记 `refused` + 理由 |
| `research` | 按名字调 P14e：`plan_admissions(...)` → `admit_inquiry(...)`，plan_ref = 事件 ref | 模块不在 / 不可准入 → `queued` + 原因（问题文本仍记在判决上） |
| `revise_forecast` | `revise_assumptions(prior, [{driver, period, value, because, refs}], change_reason="driver_event", evidence_refs=[{kind:"event", ref:<event>}], decision=<五词>, actor_ref=<principal>)` → `publish` | mission 无 `forecast_line` → `ForecastRevisionProposal`（`forecast_overturn` 人闸） |
| `revise_thesis` | `ThesisRevisionCandidate`（ADR-0007） | mission 无 `thesis_revision_candidate` 或缺同名 checkpoint → `queued` + 点名 ADR-0007 |
| `revise_dossier` | — | `queued`：档案是 Wave 2，决定已记，dossier lane 建好就能读 |

**反思**（owner 第三批）：`revise_*` 任一动作，或事件本身是 `price_divergence`（**包括决定是 `no_change`**）→ 第二对调用（`thesis_reflection` purpose）产出 `ThesisReflection`，同样封闭 schema、同样整条拒绝、同样的独立 verifier 与 family 规则。`market_view_vs_ours.available` 在本 Core 拿不出 consensus / 评级变化 / sales note / crowd post 时必须是 `false` 且不许引用任何东西——**没有 consensus 权威（P11b 是 Wave 2）时，如实说没有，不许从四条新闻里推一个街上的看法出来**。`followup_tracking` / `followup_research` 是**候选**：写在反思记录里，不改任何 cadence、不开任何任务。

---

## 5. 常驻性（owner 第一批指令）

- 成员判定 = 「该公司在当前 mission 版本下有一条 `initial_screen` `gate_passed`」，**单调**：进了 `deep_insight_gate`、开了专项研究、别的公司在做 Initial Screen，都不会把它移出去。唯一的离开方式是被移出 mission universe，那是人的动作。
- lane **没有选择器**：一个 child 扫全部被跟踪公司，不是一 tick 一家。测试 `test_every_tracked_company_is_scanned_on_every_run` 与 `test_tracking_is_resident_across_later_stage_work` 钉住这两条。
- 基线拉取不由判断层裁量：判断层只能提 `TrackingCadenceVersion`（改**值**），policy 标 fixed 的源连值都改不了。

---

## 6. 现有 lane 要加的那一行（`next_due`）

`MissionSourceDiscoveryCoordinator.launch_discovery()` 已经有一个逐 (company, spec) 的闸门 `self._spec_block(mission, company_ref, spec)`（`mission_source_discovery.py:1455`），返回非 `None` 就把这一对记进 `skipped` 并跳过。频率闸门装在同一个地方，形状与已有 block 一致：

```python
# mission_source_discovery.py，_spec_block 里，其它 block 之后
from .tracking_cadence import next_due

due = next_due(
    self.cadences.cadence(company_ref, SOURCE_KEY_FOR[self.source_ref], policy=self.policy),
    now=datetime.now(timezone.utc),
    last_pull_at=self.missions.latest_discovery_at(company_ref, self.source_ref),
    pulled_forward_by=self.pulled_forward.get(company_ref),
)
if not due["due"]:
    return f"cadence: next due {due['due_at']}"
```

三样东西这条 lane 需要在构造时拿到，都是加法：

1. `TrackingCadenceAuthority(store)` 与 `load_policy(path)`（协调器缓存在 `server.lane_state` 里，和 coordinator 同寿命）；
2. `last_pull_at` = `MAX(created_at) FROM coverage_mission_source_discoveries WHERE company_ref=? AND source_ref=?`——已有表已有列，不需要新账本；
3. `pulled_forward` = `tracking_cadence.immediate_pull_sources(policy, events, now)`，用该公司最近 20 条事件算，价格异动 6 小时内把三个新闻源拉到「立刻可拉」。

`SOURCE_KEY_FOR` 是 `source_ref → policy source_key` 的三行表（`source:alphaengine → alphaengine`、`source:web-search → gemini-web-search`、`source:sec-edgar → sec`），和 `source_capability_map.SOURCE_PLAN_ALIASES` 同源。

**本片没有改这条 lane**：`mission_source_discovery.py` 不在我的所有权里，而且频率闸门装上去就会立刻改变 live 的取数行为——那应当是集成时一次有意的动作，不是随本片附带的副作用。跟踪 lane 每次运行都会把 `due` 表写进 summary（`summary["due"][company][source]`），所以在闸门装上之前，「哪个源该拉了」是可读的，只是没人服从它。

价格 lane（P11a）与 SEC lane 不需要改：它们的源在 policy 里标 fixed，基线就是它们已有的行为。

---

## 7. owner 需要做的事

1. **发一版 mission，`autonomy.may_write` 加 `market_event`。** live 第 13 版有 `observation`、`deliverable`、`forecast_line`、`stage_record`，缺 `market_event`——跟踪 lane 每 tick 返回 `ungranted`、一个事件都不写（冒烟已实测，见 §8）。这是本片**唯一必须**的授权。
2. 想让判断层能提 thesis 修订候选：同一版再加 `thesis_revision_candidate`，并把它加进 `autonomy.human_checkpoints`。缺任一个 → `revise_thesis` 记 `queued` 并点名 ADR-0007。缺席不影响其它五个动作。
3. **装 lane（两个开关，都是文件存在与否）**：
   - `{state}/tracking-policy.json` ← `deploy/phase9/p14a-tracking-policy-v1.json`。没有它，跟踪 lane 整条不存在，LaunchAgent argv 一字不变。
   - `{state}/event-judgement-model-config.json` **和** `{state}/event-verifier-model-config.json`。**两个都要**：只有 judge 没有独立 verifier 的判断 lane 会产出「verification 字段存在但没有意义」的决定，所以 launcher 与 argv 片段都要求成对。两份配置应指向**不同 family** 的 routing policy，否则每次判断都会 fail closed。两个名字都已 `register_model_config_name`，所以下次抬日预算上限会自动重指。
4. 不需要新的 connector 治理记录、不需要 `install.sh` 的模型配置块以外的东西。

---

## 8. 冒烟（只读，无模型调用）

`cp /private/tmp/dalton-ro/core.sqlite /tmp/p14a-core.sqlite`，然后
`PYTHONPATH=$PWD/src .venv/bin/python scripts/run_p14a_tracking_smoke.py --core /tmp/p14a-core.sqlite`：

```
mission     : coverage-mission-version:us-it-services:13
policy      : tracking-policy:p14a:v1 (34411714c04c)
universe    : 5 companies
tracked     : 4 with a passed Initial Screen
  market_event     NOT GRANTED — the lane would be idle
  observation      granted
  deliverable      granted

price days  : none — this Core holds no MarketPriceSeriesVersion, so neither the
              abnormal-move nor the divergence emitter would record anything

ACN   company:sec-cik:0001467373: 103 events {'news': 42, 'filing': 1, 'claim': 60}
EPAM  company:sec-cik:0001352010:  94 events {'news': 34, 'claim': 60}
IBM   company:sec-cik:0000051143:  97 events {'news': 37, 'claim': 60}
DXC   company:sec-cik:001688568 :  92 events {'news': 31, 'filing': 1, 'claim': 60}

total       : 386 events over the last 7 days
judgement   : 386 bounded calls at $0.10 a pair would cost $38.60; the
              event_response pool is $15.00 a day
```

读法：

- **过闸四家（ACN / EPAM / IBM / DXC）自动进入跟踪，CTSH 不在**——它的 Initial Screen 还是 `entered`。这正是「过闸后默认开启」。
- **价格事件是零，因为 live 上还没有一根 K 线**：P11a 的 lane 尚未在这台机器上跑过。异动与背离两条路径在这份 Core 上都不会产出任何东西，这是数据缺口不是逻辑缺口（隔离测试里两条路径都跑通）。
- **`claim` 每家 60 条是扫描上限（`MAX_EVENTS_PER_SCAN`）而不是真实计数**，见开放问题 ①。
- 386 条 × $0.10 > 一天 $15 的池：跟踪 lane 一次最多记 120 条、判断 lane 一次最多判 8 条（每公司 3 条），池耗尽时返回 `skipped:pool_exhausted`，积压量由 `metric:tracked-events-open` 可见。

---

## 9. 集成时要接的线

1. **lane 注册已完成**：`LANE_MODULES` 两行，`writer_server.py` / `bounded_planner_driver.py` / `macos_launchagent.py` 一字未动。新解释器里实跑验证过（`tests/test_mission_tracking_lane.py::RegistrationTests::test_a_fresh_interpreter_derives_the_lane_from_the_registry_alone`）。order：跟踪 87（价格 85 之后），判断 115（Initial Screen 110 之后）。P14e 的 120 不冲突。
2. **`tests/test_mission_market_price_lane.py` 改了一条断言**：它原本断言价格 lane 的**下一条**是 model spec lane。P14a 的跟踪 lane 必须排在价格之后（异动是从那条 lane 刚发布的 K 线上读出来的），所以相邻断言改成了序关系断言，注释写清了原因。这是 P11a 的测试文件，不在我的禁改清单里，但集成时请确认这是可接受的改法。
3. **`mission_deliverable` 的 CHECK 迁移**：`event_note` 进 `DELIVERABLE_KINDS` 需要同时放宽 schema 的 `CHECK(kind IN …)`，而 `CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做。`MissionDeliverableAuthority.__init__` 里加了一段窄迁移（照 `DaltonStore._migrate_thesis_authority_columns` 的写法，含 `PRAGMA foreign_key_check` 收尾），只在旧 CHECK 存在时重建。live 只有几条 deliverable，重建很小。**这是本片唯一动到既有表结构的地方，值得在 review 时单独看一眼。**
4. **cockpit** 需要的字段（都是现成读者）：
   - 公司卡：`ResearchEventAuthority.counts(company)`、`events(company_ref=…, limit=…)`（kind / tier / occurred_at / payload），`EventJudgementAuthority.recent(company)`（decision / action / because）、`judged_count`、`reflections(company)`；`tracking_cadence.active_coverage_metrics(events, judgements, company)` 的三个数。
   - 待审批页：`thesis_candidates(company)` 与 `forecast_proposals(company)`，候选上的 `reflection_ref` 应当直接展开成反思正文——ADR-0007 的候选和「我们可能漏了什么」并排看才有意义。
   - lane 面板：`dispatch_mission_tracking` 与 `dispatch_event_judgement` 的 `status`（`launched`/`idle`/`busy`/`rejected`/`unconfigured`/`unavailable`）、`settled.tracking_status`（含 `ungranted`）、`settled.judgement_status`（含 `skipped:pool_exhausted`、`gated`）、`pool`。
   - **`ungranted` 必须可见**：一条因为缺授权而永远沉默的 lane，看起来和一条健康的空闲 lane一模一样。
5. **`install.sh`**：加一个种子块把 `deploy/phase9/p14a-tracking-policy-v1.json` 放到 `{state}/tracking-policy.json`，再加两个模型配置文件的生成（照 `initial-screen-model-config.json` 的写法，verifier 那份要指向不同 family 的 routing policy）。本片按分工没碰它。
6. **P14e 已在 main（`e6c87b9`），本片基线在它之前**：`event_judgement_cli.research_admitter_for` 是按名字 import 的，合并后 `research` 决定会自动开始走 `plan_admissions` / `admit_inquiry`；合并前它返回 `queued` + 原因。两边都有测试。

---

## 10. D3 裁决：`perception.py` / `agenda_coordinator.py` 应当**显式退役**，不迁移

读完两个模块后的结论，供 owner 拍板：

`perception.py` 的全部内容是 `LegacyCoveragePerceptionAdapter`——它从**一个 legacy Coverage sqlite**（万华那套已退役系统的库）里读 companies / events / evidence / filings 四张表，规范化成 `PerceptionSnapshot`。它的数据源不是 Dalton 的账本，是另一个产品的库。`agenda_coordinator.py` 是 Phase 1 的单 lane 协调器，import 的正是这个 adapter，服务的是 `agenda_cycles`（live 有 112 条，全部是 2026-08 的 shadow 运行，主体已于 09-04 退役）。

所以「把 `PerceptionSnapshot` 迁移成 ResearchEvent 的一个 emitter」这条路是没有意义的：它没有可迁移的输入。ResearchEvent 的 emitter 读的是 Dalton 自己的表（`coverage_mission_discovered_documents`、`claim_versions`、`forecast_reconciliations`、`market_price_series_versions`），一条也不经过 perception 那条链。两套事件平面并存的 ADR 级债，正确的还法是**把旧的那套标成退役**而不是给它接一根新的输入管。

**本片没有删除任何东西**（指令要求不删）。建议的退役形态，留给一个专门的清理片：
- `perception.py` / `agenda_coordinator.py` / `agenda.py` 的 perception 部分加 `status: retired` 的模块级说明与一条指向本节的 ref；
- `service.py` 的 `agenda` 配置块保持可解析但默认 `None`（现在已经是）；
- live 的 `perception_snapshot_versions`（103 条）与 `agenda_cycles`（112 条）**不删**：它们是历史，append-only 的账本不因为主体退役而被清空。

---

## 11. 测试

各文件项数：`test_research_event` 27、`test_market_event` 26、`test_tracking_cadence` 34、`test_source_capability_map` 15、`test_event_judgement` 75、`test_mission_tracking_lane` 26、`test_mission_event_judgement_lane` 31，合计 234。

覆盖到的、指令逐条点名的：

- **异动检测**：整板块同跌不触发任何一家；一家逆着平的同业动就触发；basket 剔除被测公司自己（否则五家里一家的动会被自己稀释掉五分之一）；basket 成员不足下限时 basket 字段是 `null` 而不是把一家当一个板块；benchmark 缺席被记录而不是当成零收益；benchmark 在场时被减掉；**provisional K 线永不触发**；序列第一根没有收益；事件 body 带价格版本 ref 与 invocation。
- **背离检测**（owner 第三批）：long thesis 遇下跌触发、遇上涨不触发（对了不是事件）；同样的价格在 short thesis 下反过来；低于阈值不触发；整板块下跌不是背离；窗口末端 provisional 不触发；没有 thesis 就无从背离；窗口内已有背离则抑制（否则每天一条哈希不同的事件）；一天不成窗口；累计收益两端都要有。
- **事件幂等**：同一事实两次是一条；payload 变了就是另一条；id 是 `(company, kind, payload_hash)` 的函数；未声明字段整条拒绝；缺字段补 `null` 后哈希稳定；空 payload 不是事件；嵌套值被拒；naive 时间戳被拒；库层不可改不可删；行与 json 漂移被读回校验抓住。
- **阶段自动进入**：过闸即被跟踪；未过闸不被跟踪；进入更深阶段 / 开了别的公司的 screen 之后**仍**被跟踪（常驻）；顺序跟 mission universe；`enter_active_coverage` 尝试写并如实报告被 Playbook 阶梯拒绝的原因。
- **`STAGE_SPINE["active_coverage"]`**：三个 metric 形状合法、只由两个账本计数、`extraction_requests` 仍只服务 `initial_screen`。
- **频率**：九条基线与 owner 表格一致；价格与 SEC 不可调；从未拉过就 due、区间内不 due、超过区间 due；异动把三个新闻源拉到立刻可拉、一周前的事件拉不动、`news` 不是触发器；提新版本 = 新版本带 prior ref；同值同证据 = duplicate；第一版等于基线 = duplicate；无证据被拒；fixed 源被拒；未知源被拒；未知 change_reason 被拒；人也可以发版本。
- **来源能力表**：每个声明的源至少一种内容；每个 tier 在冻结词表里；未知 slug 拒绝且报出已知集合；未知 content kind 拒绝；未合并的 S 线连接器标 `in_inventory: false` 但有内容；已合并的带 transport / operations / 额度；specific 源排在两个通用源前面；live mission 里每个 connected 源都映射到 ≥1 种内容；`source:web-search → source:public-web` 别名解析（否则会读成 undeclared）；投影哈希稳定。
- **判断**：每条动作路径（no_change / note / research / revise_forecast / revise_thesis / revise_dossier）；每一种拒绝（多 key、少 key、第六个词、不兼容组合、未展示的引用、不存在的 driver、无引用的非 no_change、note 超四句、note 该有没有 / 不该有却有、散文）；verifier pass 与 reject；**同 family 拒绝**、**family 解析不出来拒绝**；verdict 绑定它读的那条决定；模型不可达是带理由的拒绝；**同一事件不判两次**（第二次返回第一次的记录、动作不变）；`revise_forecast` 真的发出一版 `change_reason=driver_event` + `decision` + 事件 ref 的 `ForecastModelVersion`（v2）；无授权时变成 `ForecastRevisionProposal`；候选形状（float confidence 被拒、无证据被拒、第六个词被拒）；note 走 deliverable 且带上它引用的 Claim、无来源数字被权威拒绝；research 按名字调 P14e 且缺席时 `queued`；池是账本求和、耗尽时 `skipped:pool_exhausted`。
- **反思**（owner 第三批）：divergence 即使决定是 `no_change` 也欠一条；每个 `revise_*` 都欠一条；普通事件上的 note 不欠；封闭 schema 与整条拒绝；无引用被拒；`market_view_vs_ours` 声称 available 必须引用、声称不可得则不许引用；follow-up 点名没有基线的源被拒；反思的 verifier 遵守同一条 family 规则；同一判决第二条反思是 duplicate；**follow-up 只是候选**（跑完之后 `TrackingCadenceAuthority.latest(...)` 仍是 `None`）；候选带 `reflection_ref`；被拒的反思不落库、判决仍在；lane summary 里 `reflections` / `reflections_refused` / `followups` 三个字段。
- **lane**：新解释器实跑推导（writer 三处 + driver tick 顺序 + 两条 lane 的相对位置）；policy 文件 = 开关；judge 与 verifier 缺一不成 lane；两个模型配置名都进了抬预算清单；同一窗口只起一个孩子、新窗口再起、上一个孩子在下一 tick 被结算；没有 mission 是 unconfigured 不是崩溃；选择器抛错不掀翻 tick；每次运行扫全部被跟踪公司；第二次运行零新增；summary 永远写。

---

## 12. 开放问题

1. **每条 Claim 都值一次模型调用吗？** 冒烟显示 live 一周产出 ~240 条 `claim` 事件（每家扫描上限 60 条封顶，真实值可能更高）。现在的答案是：全部入账本，判断 lane 按每公司 3 条 / 每次 8 条 / 池 $15 的三重上限消化，积压由 `metric:tracked-events-open` 可见。两个可选的收窄，都需要 owner 定：(a) 只为**定量** Claim 发事件（live 2,170 条里只有 22 条定量），定性 Claim 由它所在文档的 `news` / `transcript` 事件代表；(b) 等 P12b 的 `importance` 落地后按重要性过滤。我倾向 (b)，因为 (a) 会漏掉「新签大单」这类定性但要命的 Claim。
2. **阈值。** 3.0% / 2.5% / 3.0%（异动）与 10 日 6.0%（背离）是没有历史校准的第一版数字——live 上一根 K 线都还没有，无从校准。建议 P11a 的价格 lane 跑满三年历史之后，用五家的真实分布重标一次（比如取相对 basket 日收益的 2σ）。阈值在 policy 文件里，重标不用改码。
3. **`active_coverage` 阶段记录写不进去。** Playbook 的阶段阶梯要求进入某一阶段前它的上一阶段已过闸，而 `active_coverage` 排在 Investment Memo 之后，后者是人类检查点。owner 说的「过闸后默认进入每日跟踪」和 Playbook 的「第六个研究阶段」是**两件事共用一个名字**。三条路，请 owner 选：(a) `coverage_mission.record_stage` 加一条窄豁免——`active_coverage` 的 `entered` 只要求 `initial_screen` `gate_passed`（四行 diff，但改的是冻结的阶段语义）；(b) 常驻跟踪不叫 `active_coverage`，另起一个不在 `STAGE_ORDER` 里的状态；(c) 维持现状——成员判定用「screen 过闸」这个事实（本片就是这么做的，lane 完全能跑），阶段记录等 memo 过闸后自然补上。本片选了 (c) 作为默认，并且每 tick 仍然尝试写、把拒绝原因如实报出来。
4. **thesis 方向的默认值。** 「覆盖 thesis 默认是 long」对一家 long-biased 基金是对的，但一条「某二线厂商会被挤压」的 thesis 就是空头，而它今天会被读成多头、于是背离检测的方向反了。overrides 在 policy 里，owner 加一行即可；更好的解法是 thesis 对象本身带一个 `stance` 字段，那是 ADR-0001 的合同改动，本片不碰。
5. **`market_view_vs_ours` 现在几乎总是 `available: false`。** 没有 consensus 权威（P11b 是 Wave 2）、没有评级变化事件（要 P11b 的研报抽取）、没有 sales note / X（S1 / S3 还在别的分支）。反思因此现在只能诚实地说「我们没有街上的看法可比」。这不是缺陷，是缺口的如实呈现；P11b 与 S 线合并后同一段 prompt 会自动变得有内容，无需改码。
6. **`event_response` 池的口径是预留，不是结算。** 和 P14e 的 `adhoc` 池同一个开放问题：`ThesisImpactBudgetStore.admit` 按 `mission_ref` 分组、没有 pool 维度，所以实际结算的钱归不到池上。C2 应该给 `mission_binding` 加 `pool` 字段。本片的池账**从判决账本求和**（`cost_micros` 列），所以池和账本不可能各说各话，但它统计的是本 lane 自己的花费，不是全局。
7. **反思把判断的成本翻了一倍。** 一条 divergence 事件是四次调用（判断 + 核验 + 反思 + 核验），$0.40 预留。这是有意的：owner 要的正是这一半。如果池变紧，先降的应该是每次判几条，不是要不要反思。
