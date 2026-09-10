# P14f 业绩季工作流交付报告 v1.0

日期：2026-09-09
分支：`w3-earnings-season`（worktree `~/Projects/dalton-w3-earnings-season-worktree`）
分叉基线：main `eaf48f0`；已 `git merge main` 到 `56f666c`（含 C1 事件桥接、P14b/P14d 重出 lane、P15d）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 1 节（机制 vs 判断、自我反思）与 Daily tracking 一节、蓝图 §5.2 P14f、ADR-0007、ADR-0008
消费（按名字，不改）：[C1 事件日历](c1-catalyst-calendar-v1.0-2026-09-09.md)、[P14a 每日跟踪](p14a-daily-tracking-v1.0-2026-09-09.md)、[P13-M2 预测行](p13-m2-forecast-lines-v1.0-2026-09-09.md)、[P12a 公司档案](p12a-company-dossier-v1.0-2026-09-09.md)（guidance profile）、[P12c DebateMap](p12c-debate-map-v1.0-2026-09-09.md)、`forecast_reconciliation`、`mission_deliverable`、`research_playbook`
全量测试：见第 7 节，原文粘贴

---

## 0. 一句话

一家覆盖公司要开口了，Dalton 会在一个月前把**我们的预测、街上的数、公司自己的指引**摆在一张纸上，说清这次要听什么、看到什么算 thesis 被证实、看到什么算被打破；公司报完之后的两天之内，它把**已经发生的数字写回模型的历史格**、按 `forecast_reconciliation` 的三档给这一季打分、拿 P12f 的指引事件表问「公司有没有做到自己说的数」，再用**一次有界调用**写出散文和每条 thesis 的五词决定。全过程不提交任何 thesis、不改任何未来期的预测：留下的是一份文档、一条事件、一个 `ThesisRevisionCandidate`（附着一份 `ThesisReflection`）和必要时一个 `ForecastRevisionProposal`——**人只裁决一次**。

---

## 1. 交付清单

| 文件 | 内容 |
| --- | --- |
| `src/dalton_core/earnings_season.py` | 无模型调用的那一半：窗口判定、occurrence 身份与幂等键、已写过没有、这次业绩对应哪一期、我们对那一期说了什么、`guidance_vs_actual`、`consensus_block`、`watch_list`、可引用 ref 集合 |
| `src/dalton_core/earnings_preview.py` | T−30 窗口：context、prompt、封闭输出与整条拒绝、独立 verifier、`earnings_preview` deliverable |
| `src/dalton_core/earnings_calibration.py` | T+0..T+2 窗口：`actualize_for_report`、对账行与三档、`actual_rows_for_guidance`、context、prompt、封闭输出、`calibration` 事件、走判断机制的效果落地、`earnings_calibration` deliverable |
| `src/dalton_core/mission_earnings_season_lane.py` | 无队列 lane + `LaneSpec`（order 117，driver_key `earnings_season`） |
| `src/dalton_core/earnings_season_launcher.py` | `EarningsSeasonLauncher(LaneChildLauncher)` |
| `src/dalton_core/earnings_season_cli.py` | 子进程：确定性在前、一次调用、效果按外键顺序落地；每个缺的写入权限如实报告 |
| `tests/test_earnings_season.py`（74 项） | 窗口、指引、consensus、预测读取、preview 的六种拒绝、calibration 的确定性三步与八种拒绝、效果与幂等 |
| `tests/test_mission_earnings_season_lane.py`（17 项） | 选择、协调器、注册（含全新解释器进程）、未装配时的行为 |
| `tests/test_earnings_season_cli.py`（9 项） | 全链路：preview → 出业绩 → 对账 → 校准 → candidate，人只裁决一次 |
| 共享增量（每处一行或一词） | `lane_registry.LANE_MODULES`、`budget_pools.LANE_POOLS`、`cockpit_plane.REGISTRY_LANE_LABELS`、`research_event` 的 `calibration` kind、`mission_deliverable` 的两个 kind + 迁移泛化 |

**没有新建 schema。** 本片写的东西全部落在既有权威上：两个 deliverable、一条 `ResearchEvent`、P14a 的 `event_judgements` / `thesis_revision_candidates` / `thesis_reflections` / `forecast_revision_proposals`、以及 `actualize` 出的一版 `ForecastModelVersion`。一个自己的表会是第二本账，而这里没有第二本账要记。

---

## 2. 工作流

```
C1 日历（或它发的 calendar 事件）
      │  T−30，estimated 也开；T+0..T+2，只认 confirmed
      ▼
 occurrence（company × entry × anchor_date）─── 已写过？──→ 跳过（不花钱）
      │ 没写过
      ├── preview ──→ context（我们 / 街上 / 指引 / 看点 / thesis）
      │                └─ 1 次有界调用 + 1 次独立 verifier ─→ deliverable earnings_preview
      │
      └── calibration
            ├─ ① actualize_model（只动历史格；未来期一格不碰）→ 新版 filing_actual
            ├─ ② forecast_reconciliation：reconcile_pending → 三档
            ├─ ③ guidance_vs_actual（P12f 事件表）
            ├─ ④ 1 次有界调用 + 1 次独立 verifier
            ├─ ⑤ deliverable earnings_calibration
            ├─ ⑥ ResearchEvent kind=calibration（forward_estimates_revised: false）
            └─ ⑦ 效果（按外键顺序）：EventJudgement → ThesisReflection → ThesisRevisionCandidate(s)
                                     → ForecastRevisionProposal（≥3% 档触发，人闸 forecast_overturn）
```

**代码里强制的规则**，不靠调用方自觉：

1. **estimated 开 preview，只有 confirmed 开 calibration。** C1 定的，这里再判一次——从账本读事件的 lane 不能假设写事件的就是它以为的那个 emitter。`build_calibration_context` 对未确认的日期直接 `CalibrationRefused`。
2. **`date_confidence` 与 caveat 随 preview 走到底。** 从事件上带下来而不是各处自己推导；模型自己那段 `summary` 里没有这句话，整条回答被拒绝（检查的是模型的散文而不是拼装好的正文——正文是我们自己补的 caveat，检查它等于什么都没检查）。
3. **一次业绩一份工作，不是一个事件一份工作。** 公司把 estimated 确认成 confirmed 时 C1 会再发一条 preview 事件；那是给判断层重排期用的消息，不是再买一份 preview 的理由。幂等键是 occurrence，而 occurrence 就是 C1 的 `entry_ref`（见 3.1）——日期动了还是同一次业绩。
4. **「做过了」要两半都在。** preview 只产出一份文档，文档在就是做过了。校准产出的是文档**和**一条事件加一串判断记录，所以只有两半都在才算做过：只看文档，发布之后判决失败的那一次会被永远跳过，这一季再也到不了判断层；只看判决，文档被拒的那一次会丢掉散文。两半都看，缺哪半下一 tick 补哪半，已经在的那半回 `duplicate`。
5. **先写账本，后发文档。** 最容易丢的是「这一季到了判断层、候选到了人手里」，所以它先落；文档在写的时候，那一半已经是安全的。
6. **公司到点了但还没报，就等。** 窗口在应报日当天就开，而 filing 不在午夜落地。这时候既没有 actual 格也没有对账行，一次调用只能去给没人读过的业绩打分——而且会把这次机会花掉。所以**不调用**，返回 `waiting`；窗口活 `CALIBRATION_DEADLINE_DAYS` 天，明天再问。判据读的是模型自己的格子（该期有没有 `actual` 单元），不是本次运行 actualize 的结果——昨天已经对齐过的那一版，今天再问会说「没什么可对齐的」，读运行结果就会让它永远等在自己已经结清的那一季后面。
7. **mission 没授 `market_event` 就一分钱不花。** 记不下判决的校准是半个校准，丢掉的正是人要读的那一半；这在调用之前就查，占用的机会留给之后授权了的那一 tick。

---

## 3. 形状

### 3.1 occurrence（不落库，是几个账本的联合读）

```json
{
  "occurrence_ref": "earnings-occurrence:{sha256({entry_ref})[:32]}",
  "company_ref": "company:sec-cik:0001467373",
  "entry_ref": "catalyst-entry:…",
  "event_ref": "research-event:…", "event_hash": "…",
  "window": "preview", "event_kind": "earnings",
  "anchor_date": null, "expected_date": "2026-10-01",
  "date_confidence": "estimated", "date_unconfirmed": true,
  "date_caveat": "日期未确认",
  "source_refs": ["catalyst-calendar-version:…"], "as_of": "2026-09-09"
}
```

**身份就是 C1 的 `entry_ref`，别的什么都不掺。** `catalyst-entry:{sha256({company, event_kind, anchor_date})}` 本来就是「一次业绩」的身份：它钉在任何人给出的第一个日期上，之后 `expected_date` 怎么动它都不动。所以这里只是换个名字，不是第二套身份。

初版还把 anchor date 一起哈希进去，那是错的两次：anchor 已经在 `entry_ref` 里面了；而且它**根本不随事件走**——`research_event` 冻结的 `calendar` payload 只有九个字段，`anchor_date` 不在其中（它可导出，账本的职责是当那本薄的）。从 `expected_date` 把它凑出来就等于把工作键在日期上，而那正是绝对不能做的事：改期会买第二份付费 preview。事件上没有 `entry_ref`（值为 `null`）时，本片**不产生 occurrence 也不做任何工作**——叫不出名字的一次业绩没法保证只写一次。

### 3.2 `earnings_preview` deliverable

一段散文（`summary` / 值得听的 / 证实的信号 / 打破的信号 / gaps / caveat），`claim_refs` 与 `numbers` 是被引用的活 Claim，`idempotency_key = earnings_preview:{occurrence_ref}`，`template_ref = template:earnings-preview:p14f:v1`。

模型输出封闭为五个键，多一个少一个都整条拒绝：

```json
{"summary": "…",
 "what_to_watch": [{"question": "…", "why": "…", "refs": []}],
 "confirms_thesis": [{"observable": "…", "thesis_ref": "thesis-version:…", "refs": []}],
 "breaks_thesis":   [{"observable": "…", "thesis_ref": "thesis-version:…", "refs": []}],
 "citations": ["…"]}
```

### 3.3 `earnings_calibration` deliverable

同样一段散文，但输出是四个键：`summary`、`lines[]`（`inline|better|worse|unknown` + 理由 + refs）、`theses[]`（每条 thesis 一个五词决定 + `no_change|note|revise_thesis` + 理由 + `proposed_statement` + refs）、`reflection`（P14a 的反思形状，逐字段同名）。

**反思搭在同一次调用上，不是第二对调用。** 一份校准本来就是「我们预期什么、实际发生了什么、为什么」；再买一次等于为同一段话付两次钱。它按 P14a 的规则校验（包括「说自己拿不到市场看法就不许引用任何东西」），并通过 P14a 的 `record_reflection` 落库。

### 3.4 `calibration` 事件（`research_event` 新增 kind，纯增量）

```json
{"occurrence_ref": "…", "period_end": "2026-08-31",
 "model_version_ref": "forecast-model-version:…",
 "reconciliation_count": 1, "overturn_candidates": 0,
 "notable": 1, "within_tolerance": 0,
 "decision": "THESIS_STRENGTHENED",
 "forward_estimates_revised": false}
```

最后一个字段是这条事件存在的全部理由：这一季结清了、历史格对齐了，**未来期没人动过**；要不要因为这次业绩改明年，是判断层的事。

### 3.5 效果（全部是候选）

| 触发 | 落什么 | 谁裁决 |
| --- | --- | --- |
| 每次校准 | `EventJudgement`（`UNIQUE(event_ref)`，同一事件永远只判一次） | — |
| 任一 thesis `action=revise_thesis`，或 ≥3% 档触发 | `ThesisReflection`（`UNIQUE(judgement_ref)`） | — |
| 每条要修订的 thesis | `ThesisRevisionCandidate`（ADR-0007，带 `reflection_ref`） | **人**：`thesis_revision_candidate` |
| 每条 `band = overturn_candidate` 的对账行 | `ForecastRevisionProposal` | **人**：`forecast_overturn` |

`ForecastRevisionProposal` 在这里**不是「mission 没授权时的退路」**：三个点以上的偏差按 `forecast_reconciliation` 的合同本来就抬人闸，自动化把它裁掉就是在裁掉这个闸存在的理由。

---

## 4. 人裁决什么，只裁决一次

一次业绩结束后，等着人的东西恰好是**一条 `ThesisRevisionCandidate`**（每条在场 thesis 至多一条），上面挂着当次的 `ThesisReflection`：提案和「我们可能漏了什么」在一个地方，不用翻两处。偏差过大时旁边多一条 `ForecastRevisionProposal`。

人不需要批准的：preview 的发布、模型历史格的对齐（`filing_actual`）、对账行的生成、校准文档的发布、`calibration` 事件的发出。这些要么是既有权威已经授权的写入，要么是机械的（把已经发生的数字写到它自己的格子里）。

端到端测试 `test_the_print_produces_a_calibration_and_one_thing_to_decide` 把这句话钉住：跑完之后 `thesis_versions` 计数是 0，未来期的每一格与跑之前逐字节相同，`thesis_revision_candidates` 恰好一条。

---

## 5. 集成时要接的线

### 5.1 日历事件到账本这一段（开发过程中发现，已由 main 的 `c1-event-bridge` 修好）

本片开工时 main 上有两个缺口，两个都会让 P14f 的窗口开在空气里：

1. `catalyst_calendar.emit_calendar_events` 写的 payload 比 `research_event.PAYLOAD_FIELDS["calendar"]` 声明的五个字段宽，`validate_payload` 见到没声明的字段整条拒绝；
2. `mission_catalyst_lane` 把写入口解析成 `getattr(server, "record_research_event", None)`，而 `writer_server` 上没有这个属性，于是 lane 每一 tick 都报 `events_unwired`——一个长得像接好了的 fallback。

**合入 main（`56f666c`）后两个都已修好**：`calendar` 的字段集加宽到九个（`window` / `entry_ref` / `date_confidence` / `disagreement`），lane 直接构造 `ResearchEventAuthority`。所以本片的 `window_of` 现在走的是「读 emitter 自己写的那个词」这条路，派生只是兜底。

**保留的兜底仍然有用**：`due_occurrences` 先读事件账本，读不到再直接读 C1 的日历——用 C1 自己的 emitter 配一个收集器代替 writer，窗口规则仍然是 C1 的，一个字节都不写。live 今天既没有 `research_events` 表也没有日历版本（第 6 节），日历 lane 第一次跑起来之前，这是唯一能回答「今天该写什么」的读法；也是只读冒烟唯一诚实的读法——不写一条事件就能知道会开哪个窗口。

### 5.2 deliverable 正文放不下我们自己的预测数字

`mission_deliverable.validate_section` 要求正文里每个数字都被 `numbers[]` 里一条**活 Claim** 覆盖。预测格不是 Claim，所以：

- 正文里的数字只能来自被引用的活 Claim；我们自己的估计在散文里用「高于指引上沿 / 低于街上」这类措辞说；
- 我们的数字写在文档的 `summary` 行里，**每个数字后面跟着它的 cell ref 与模型版本 ref**；
- 该期没有预测行时，`summary` 行里写 `缺来源` 而不是 `None`。

一句更准确的说法：`summary` 字段本身**不被数字检查扫**，所以把数字写在那里是可行的，但它不是「带 ref 的正文」的等价物——它是一行摘要，没有分节、没有 `claim_refs`、也不进 `unsourced_numbers` 的核验。所以这是一个**权衡**，不是一个等价替代：读者拿得到我们的数字和它的 cell ref，但那一行不受权威的数字纪律保护。

要让预测数字进正文并同样受检，需要 `validate_section` 能接受 Claim 之外的 ref 种类（比如 `forecast-model-version:…` 加 cell ref），照今天检查 Claim 的方式检查它存在且未被取代。这是 `mission_deliverable` 所有者的一次改动，不在本片权限内，列为 to-do。

### 5.3 live 现在缺什么（第 6 节冒烟测出来的）

| 缺口 | 影响 | 归属 |
| --- | --- | --- |
| `catalyst_calendar_versions` 0 行、没有 `research_events` 表 | 没有 occurrence 可开窗；ACN 10/1 要靠日历 lane 先在 live 上跑一次 | C1 部署 |
| `forecast_model_versions` 0 行（只有 4 条旧的 `model_forecast_line_versions`） | preview 没有我们自己的数字；calibration 的 `actualize` 报 `unavailable` | P13-M2 在 live 上跑一次 |
| claim 索引表不在 live | `guidance_profile_for` 返回 `None`，指引块 `available:false` | P12b 部署 |
| 没有 consensus 权威 | consensus 块永远 `available:false`（**如实如此，不是降级**） | P11b |
| mission 未授 `market_event` / `thesis_revision_candidate` | 校准不发事件、修订候选排队并点名 ADR-0007 | owner 发一版新 mission |

### 5.4 install.sh / LaunchAgent

lane 需要两份模型配置才装：`earnings-season-model-config.json` 与 `earnings-season-verifier-model-config.json`（两者缺一 `argv_fragment` 返回空）。两个 purpose（`earnings_preview` / `earnings_calibration`）在 `model_fallback_chain` 里已经是 `brain` 档，无需再登记。`scripts/raise_day_budget_cap.py` 的 `MODEL_CONFIG_NAMES` 由 `register_model_config_name` 自动收到。

### 5.5 cockpit

`REGISTRY_LANE_LABELS` 已加一行「业绩前写前瞻、业绩后对账」。真正值得上 cockpit 的是**等人裁决的那一条**：`thesis_revision_candidates` 与 `forecast_revision_proposals` 已有读者（P14a / INT2），本片不新增页面。

---

## 6. 冒烟：ACN 今天的 preview 会包含什么（只读副本，零模型调用）

`/private/tmp/dalton-ro/core.sqlite` → `/tmp/p14f-smoke.sqlite`，mission `coverage-mission-version:us-it-services:13`，过闸公司四家（ACN / EPAM / IBM / DXC）。

按 2026-10-01 的预期日、2026-09-09 的今天构造 occurrence：

```
occurrence: earnings-occurrence:aebf11464c7a6b706e6ecae50b555e31
window preview　expected_date 2026-10-01　date_confidence estimated　date_caveat 日期未确认
period_end: None（live 没有 ACN 的 ForecastModelVersion）
forecast rows: 0
consensus: available=false —— this Core holds no consensus authority (P11b), so there is
           no street number to compare against
guidance:  available=false —— no guidance profile: this Core holds no readable guidance
           statement for this company
watch source: thesis_falsifiers_and_drivers（DebateMap 对 ACN 没有 open debate）
  - thesis_falsifier 这次业绩里有什么会检验：New bookings lead revenue by two to four quarters…
  - thesis_falsifier 这次业绩里有什么会检验：AI-led programs create consulting work and follow-on…
citable refs: 21　claim-backed numbers: 12　theses: 2　claims: 12
gaps:
  - 缺来源：这家公司还没有覆盖该期的预测模型行，preview 里没有我们自己的数字
  - 缺来源：consensus this Core holds no consensus authority (P11b) …
  - 缺来源：guidance no guidance profile …
文档摘要行:
  company:sec-cik:0001467373 2026-10-01 preview（estimated）｜期间 未知｜街上 available:false
  ｜指引 available:false｜日期未确认
prompt: 9,002 字节
```

读法：**今天这份 preview 是薄的，而且它自己说得清楚为什么薄**——三个 `缺来源` 各点名一个还没在 live 上跑的上游，两条看点来自 thesis 的机制而不是 DebateMap（ACN 没有 open debate），十二个可引用数字全是活 Claim。它不会去猜街上的数，也不会拿新闻凑一个指引区间出来。第 5.3 节的三个缺口补上之后，同一段代码会自动变厚。

---

## 7. 验收

### 7.1 全量测试（原文）

```
$ PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
...
Ran 4494 tests in 740.176s

OK (skipped=1)
```

合并 main（`56f666c`）之后一条不红。本片新增 100 项（`tests/test_earnings_season.py` 74、`tests/test_mission_earnings_season_lane.py` 17、`tests/test_earnings_season_cli.py` 9）。

**开发过程中撞到的一个与本片无关但值得记的东西**：`tests/test_openclaw_web_search_broker_client.py` 的 `FUTURE` 是在**模块导入时**算的「五分钟后」，而 `unittest discover` 会先导入全部测试模块再开始跑。全量套件跑到这个模块时早已过了五分钟，于是它的八项全部 `ERROR: web search deadline has already passed`。这不是随机的 flake，是一颗随套件变长必然引爆的定时炸弹：合入前在 main 上量过，`test_[a-n]*.py` 这一段是 4:35（刚好在闸内），本分支加了约 13 秒的测试后是 5:13（刚好在闸外）。已报给主 agent，**main 上已修**，本次全量因此干净。

### 7.2 逐条对着任务书

| 要求 | 落在哪 | 测试 |
| --- | --- | --- |
| preview 在日历 `preview` 事件上，每次业绩一次 | `open_occurrences` + `idempotency_key_for` | `test_confirming_the_date_does_not_change_the_occurrence`、`test_the_same_occurrence_is_written_once`、`test_a_preview_is_written_once_for_the_occurrence`（CLI） |
| 我们的预测（带 refs）vs consensus（或 `available:false`）vs 指引 | `forecast_rows` / `consensus_block` / `guidance_vs_actual` | `test_the_context_carries_our_numbers_the_street_and_the_guidance`、`test_no_reader_is_available_false_with_the_reason` |
| 看点：open debates，否则 thesis falsifier + driver | `watch_list` | `test_open_debates_win_when_there_are_any`、`test_the_fallback_is_the_thesis_and_the_drivers_and_says_so` |
| 什么会证实 / 打破 thesis | `confirms_thesis` / `breaks_thesis` | `test_a_preview_that_cannot_say_what_would_break_the_thesis_is_refused` |
| `date_confidence` caveat 带到底 | occurrence + 校验 + 正文 + 摘要行 | `test_an_unconfirmed_date_with_no_caveat_is_refused` |
| 一次有界调用、每个数字有 ref、整条拒绝、P14a 式 verifier | `draft_preview` / `verify_preview` | `test_a_figure_with_no_live_claim_behind_it_is_refused`、`test_a_citation_that_was_never_shown_refuses_the_whole_answer`、`test_a_verifier_on_the_producers_family_refuses_rather_than_passing` |
| 校准：先确定性 `actualize_model` | `actualize_for_report` | `test_the_filed_quarter_lands_beside_the_estimate` |
| 三档对账行 | `reconciliation_rows` / `tier_counts` | `test_the_three_tiers_are_counted_and_the_overturn_named` |
| 指引 vs 实际，出成一个可按名字消费的函数 | `guidance_vs_actual`、`actual_rows_for_guidance` | `test_one_period_is_read_back_with_its_verdict_and_refs`、`test_the_actual_cells_are_handed_to_p12f_in_the_shape_it_reads` |
| 一次有界调用出散文 + 每条 thesis 一个五词决定 | `validate_calibration_output` | `test_no_change_may_not_be_a_reason_to_revise_the_thesis`、`test_a_sixth_decision_word_is_refused` |
| revise → `ThesisRevisionCandidate` | `apply_calibration_effects` | `test_a_revision_becomes_a_candidate_a_person_rules_on` |
| ≥3% → `ForecastRevisionProposal`（人闸） | 同上 | `test_the_three_percent_tier_raises_a_forecast_overturn_proposal` |
| 两种情况都写 `ThesisReflection` | 同上 | `test_the_reflection_is_written_and_attached_to_the_candidate` |
| 没有任何东西自动提交 thesis | 同上 | `test_no_thesis_version_is_written_by_any_of_this` |
| 未来期不改，发事件给判断层 | `emit_calibration_event` | `test_the_event_tells_the_judgement_lane_the_forward_view_is_unreviewed`、`test_the_calibration_does_not_move_a_forward_quarter` |
| lane：每 tick 扫过闸公司里未处理的窗口，幂等 | `due_occurrences` / `newest_due` | `test_a_window_already_written_about_is_not_named_again`、`test_a_second_run_over_the_same_occurrence_pays_nothing` |
| lane 注册（全新解释器） | `LaneSpec(order=117)` | `test_importing_this_module_does_not_pull_in_the_writer`、`test_the_lane_is_registered_at_its_own_order` |
| ACN 全链路自动跑通、人只裁决一次 | CLI | `test_the_print_produces_a_calibration_and_one_thing_to_decide` |

---

## 8. 没做什么

- **不改未来期的预测。** 一格都不动，连提案都不由本片对未来期提——`ForecastRevisionProposal` 提的是已发生那一期被打脸的那条线，未来期的事写在 `calibration` 事件里交给判断层。
- **不建第二个事件平面。** 校准只发一条 `calibration` 事件；每条对账行的 `reconciliation` 事件是 P14a 自己的扫描发的，本片不重复发。
- **不做 investor_day / guidance 事件的窗口内容。** C1 的 `WINDOWED_EVENT_KINDS` 包含它们，本片的 prompt 是照业绩写的；这两类事件今天会开窗，写出来的是同一份骨架，内容值不值得另写一版留给 owner 看过真实产出后定。
- **不做周报投递、不碰 cockpit 页面、不发 mission 版本、不部署。**
- **不给自己建表。** 见第 1 节。

---

## 9. 开放问题（攒给 owner）

1. **mission 要不要授 `market_event` 与 `thesis_revision_candidate`？** 不授也能跑：校准照写，但判断层不会被告知这一季结清了，修订候选会排队并点名 ADR-0007。授了之后本片才是完整的。
2. **deliverable 正文能不能带预测格的 ref？**（第 5.2 节）不能的话，我们自己的数字永远只出现在文档摘要行里；能的话，preview 会好读很多。这是 `mission_deliverable` 所有者的一次改动。
3. **investor_day / guidance 窗口要不要单独的 prompt？**（第 8 节）

（原来的第 2 条「`calendar` 事件 payload 合同谁改」在合并 main 后已由 `c1-event-bridge` 解决，见第 5.1 节。）
