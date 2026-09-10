# W4 切片交付报告：ZeroBaseReview + 判断事后验证 v1.0

日期：2026-09-10
分支：`w4-zero-base-review`（worktree `~/Projects/dalton-w4-zero-base-review-worktree`，基线 main `ba99ef9`）
出处：`chem-retrospective-implications-v1.0-2026-09-10.md` §1 第 3 行、§3.3、§5 第 3 与第 5 项；
      `parallel-development-plan-v1.0-2026-09-09.md` §4 规则 1–14（规则 9 的四处登记）

---

## 0. 一句话

Chem 设计过月度零基复盘、一次都没跑过（`dalton-coverage-zero-base` 这条 cron 触发次数为 0），
也没法证明自己 89/92 条 `NO_CHANGE` 是对的。这一片把两件事都补上：一条有 cadence、有版本链、
只提案不改权威的 `ZeroBaseReview` lane，和一本冻结公式、可重放、没有模型参与的
「判断—结果」台账。

---

## 1. 做了什么

### 1.1 `ZeroBaseReview` 权威 + lane

| 件 | 文件 |
| --- | --- |
| schema | `src/dalton_core/zero_base_review_schema.sql` |
| 权威、提示词、校验、cadence | `src/dalton_core/zero_base_review.py` |
| 子进程 | `src/dalton_core/zero_base_review_cli.py` |
| launcher | `src/dalton_core/zero_base_review_launcher.py` |
| lane（`dispatch_zero_base_review`，order 155） | `src/dalton_core/mission_zero_base_lane.py` |

**形状照 `statement_snapshot.py`**：append-only、`content_hash`、`dalton_authorized()` 三触发器
（insert guard / no update / no delete，pointer 表另有 update guard）、每公司一条版本链
（`prior_version_ref` + `UNIQUE(review_ref, version_number)`）、写完读回并比对 `id`，
不一致抛 `ZeroBaseReviewConflict`。

**触发**（`review_state()` / `due_reviews()`，两次只读查询，`now` 注入）：

1. 每家过了 Initial Screen 的覆盖公司，本月没有 `trigger='monthly'` 且 `period_label=YYYY-MM`
   的版本 → 到期；
2. 该公司有一版 `earnings_calibration` deliverable，而没有任何一版复盘的 `period_label`
   等于那一版的 `version_ref` → 到期，且**优先于**月度：财报刚出的那天是累积叙事最可能错的时候。

「过了 Initial Screen」直接复用 `tracking_cadence.screen_passed_companies()`，
所以住民资格跨 mission 版本折叠这件事只有一处定义。

**cadence 没有复用 `TrackingCadenceVersion`。** 那个权威是 per `(company, source_key)` 的
**取数间隔**（`interval_seconds` + `adjustable`），语义是「多久去源头拉一次」；零基复盘既不拉源，
也没有可调间隔可言（月度是 owner 的日历，不是模型可以放宽的东西）。硬塞进去会给那张表加一个
不是 source 的 source_key。改为照 Q2 周反思的做法：期次是账本里的事实（`period_label`），
到期与否是一次读，`inputs_hash` 在底下兜底——同一批输入读两次是 `duplicate`，只付一次钱。

**提示词（brain 层，一次有界调用，无 verifier）** 问且只问四件事：

1. 如果今天第一次看这家公司，会不会建立观点（`yes` / `no` / `unclear` + because）；
2. 现有 thesis 哪几条会被重新写（每条指名 thesis 版本 ref + Playbook 的决定词 + 新写法 + 证据 refs）；
3. 哪些 debate 已经不重要了（指名 debate ref + 理由）；
4. 下一个验证点是什么、日期是哪天。

输出是封闭 schema，整体拒绝：多一个 key、引用一个不在提示词里的 ref、改一条这家公司没有的 thesis、
同一条 thesis 改两次、验证日期早于 as-of，都是 `refused`，不修补。
`REWRITE_DECISIONS` 刻意去掉 `NO_CHANGE` 与 `NEW_THESIS`——后者按 ADR-0007 是 coverage admission
而非 revision，放行只会在人裁决的那一刻被拒，那是最坏的发现时机。

**产出**：deliverable 形状的记录（四段中文 narrative + summary + `authority_note`），
走 `WRITE_SCOPE = "deliverable"`（与 Q2 周反思同一类：有日期、按 cadence、给人读、不对任何公司下断言），
写进自己的版本链；**不经 `MissionDeliverableAuthority`**，因此没有动 `DELIVERABLE_KINDS`
（那是一个跨切片热点文件，且复盘不带需要 Claim 支撑的数字）。

**人裁决口**：`CHECKPOINT_KINDS` **没有扩**。零基复盘的改写就是
`thesis_revision_candidate`——那个词已经在词表里（P14-0 加的），也已经是 P14b 的裁决入口。
候选只在 mission 同时授予 `may_write: thesis_revision_candidate` 与同名 checkpoint 时才写，
两个条件缺一即拒（与判断 lane 的规则逐字相同）。测试把这条钉住
（`test_the_checkpoint_word_is_one_the_mission_vocabulary_already_has`）。

### 1.2 候选如何走进既有的 ADR-0007 路径

`thesis_revision_candidates.judgement_ref` 是 `NOT NULL REFERENCES event_judgements(judgement_id)`，
而 `PRAGMA foreign_keys` 是开的。零基复盘的候选背后**没有事件**——那正是它的意义——所以三条路：

1. 造一条假的 `event_judgement` 行：会污染判断统计，也会污染本切片自己的事后验证台账；
2. 重建那张 append-only 表把 `judgement_ref` 放松成可空：重写权威表，拒绝；
3. **同形状的兄弟表 + 读者合并**：选这条。

`zero_base_revision_candidates` 与 `thesis_revision_candidates` 列同形（`review_ref` /
`review_version_ref` 取代 `judgement_ref`，`checkpoint_kind` 同为 `thesis_revision_candidate`），
`ThesisRevisionAuthority` 加了一个 `CANDIDATE_TABLES` 元组，`_candidate_row()` 与 `undecided()`
读两张表并按时间合并。**下游一条路径**：同一个 `thesis_revision_decisions` 账本、同一个 `decide()`
（accept / reject / defer 全部不变）、同一条 thesis 版本链。判断 lane 的候选带
`ThesisReflection`，零基复盘的候选带 `zero_base_review`（四个答案的摘要），因为
「什么都没发生、我们仍然会写得不一样」这句话只有把四个答案摆在旁边才是论证。
`thesis_revision.py` 的改动是纯增量：一个元组、两个读者、一个 `_review()` 辅助函数。

### 1.3 `no_change` / `revise` 的事后验证

| 件 | 文件 |
| --- | --- |
| schema | `src/dalton_core/judgement_outcome_schema.sql` |
| 冻结公式 + 权威 | `src/dalton_core/judgement_outcome.py` |

**`no_change` → `should_have_moved` 候选。** 锚点 = 判断写下那天；窗口 = 锚点前最后一个结算收盘
到其后第 N 个结算收盘，N 与阈值都直接取 `tracking-policy` 的 `abnormal_move`
（`window_trading_days` = 10、`divergence_vs_basket_percent` = 6.0、`min_basket_members` = 3），
篮子是 mission universe 里**其余**公司的等权累计收益，方向按 thesis stance 取
（long 想要 excess 为正，所以「逆着走」是 `-excess`）。全部算术由
`market_event.cumulative_return` 提供——阈值与口径与 `price_divergence` 检测器**同一处定义**，
否则系统会对「什么叫背离」持两种意见。超阈值 = `should_have_moved`，否则 `held`。

**`revise` → `moved_right`。** 决定词带方向（`THESIS_STRENGTHENED` → up，
`THESIS_WEAKENED` / `THESIS_BROKEN` → down），判断之后**第一条**（不是最合意的那一条）
`forecast_reconciliations` 带另一个方向（actual > forecast 为 up）。两者一致 = `moved_right`，
相反 = `moved_wrong`，相等 = `not_confirmed`。`NEW_THESIS` 没有方向，`unavailable`。

**三条防自我表扬的规则**，各有测试：

* 窗口没走满是 `pending`，不是 `held`；
* 篮子成员不够、没有股价序列、没有 thesis 方向、决定词没方向 = `unavailable` + 理由，永远不是 0；
* `FORMULA_REF` 版本化，旧公式算出的行在 reflection 里以 `stale_formula` 显式列出。

**存储形状**：`judgement_outcome_check_versions` + pointer，每条判断一条版本链，
`UNIQUE(check_ref, inputs_hash)`。重算同样的行写不进去（`duplicate`）；`pending` 到期变成
`should_have_moved` 是**第二个版本**而不是一次编辑——我们等了多久才评得出来，本身就是这本账要记的。
`dalton_authorized()` 三触发器，写完读回。`record()` 拒绝 `human:` principal
（派生行不是人的决定）、拒绝声称别的 `formula_ref`、拒绝跨 check kind 借用结果词。

**谁来写**：零基复盘 lane 的子进程，两种模式：

* `review`：先跑 check pass，再对到期公司各付一次调用（每轮最多 2 家，
  免得月初五家同时到期把 coverage 池一次抽干）；
* `checks`：只跑 check pass，没有模型，随便跑。

lane tick 每次在写进程内**只读**重算一遍 check pass，把 digest 与上一个**成功**子进程写下的
digest 比对：有公司到期 → `review`；只有台账动了 → `checks`（不花钱）；都没有 → `idle`。
崩掉的子进程不许推进 watermark（写了一半就宣称账本是新的，是这里唯一值得单独写测试的坑）。

lane 的序号是 **155**：在判断 lane（116）与专项研究 lane（150）之后（它读它们写的东西），
在 Q2 周反思（160）**之前**——周反思要报这本台账的计数，让它每周读到晚一个 tick 的数字是
白白付出的代价。周反思仍然是 tick 的最后一条，那是它自己 lane 的既有不变量。

### 1.4 reflection reader 与 cockpit

* `research_cycle_reflection.judgement_outcomes(core, window)`：第九条 metric。
  两种读法——`this_week`（本周改口的那些）与 `to_date`（整本账），另把
  `should_have_moved` 与 `moved_right` 提到顶层。表不在 = `available: false` + 理由；
  表在但空 = `available: false` + 理由。一屏 0 会读成「我们从没错过」。
* `REFLECTION_VERSION` 0.1 → **0.2**：新增 metric 改变了「一周的读数」是什么，
  已经在 0.1 下反思过的周会再出一版，而不是被当成没变。
* narrative 加一句中文，并明写「这是候选，不是绩效考核」。
* `cockpit_plane._judgement_outcome_panel()` + `OUTCOME_LABELS`（七个结果词的中文名），
  挂在 `cycle_reflection()` 的返回里；没有的行不显示，没有台账时是诚实的缺席。

---

## 2. 四处登记（规则 9）与其余接线

| 处 | 内容 |
| --- | --- |
| `lane_registry.LANE_MODULES` | `+ "dalton_core.mission_zero_base_lane"` |
| `cockpit_plane.REGISTRY_LANE_LABELS` | `zero_base_review` → 「每月从零重问：今天第一次看会不会建立观点」 |
| `bootstrap.SCHEMA_DATABASES` | `+ judgement_outcome_schema.sql`、`+ zero_base_review_schema.sql`（均入 core） |
| `scripts/rehearse_deploy.CORE_MIGRATIONS` | `+ JudgementOutcomeAuthority`、`+ ZeroBaseReviewAuthority` 两条 `MigrationSpec` |

另加：

* `model_fallback_chain._PURPOSE_TIERS["zero_base_review"] = TIER_BRAIN`，
  并把 `dalton_core.zero_base_review` 加进
  `tests/test_purpose_tiers_cover_every_registered_purpose.REGISTERING_MODULES`（覆盖测试）；
* `budget_pools.LANE_POOLS["dispatch_zero_base_review"] = "coverage"` 与
  `PURPOSE_POOLS["zero_base_review"] = "coverage"`（lane 与 purpose 走同一个池，
  否则池与账本会各说各话）。`LaneSpec` **不**声明 `budget_pool`：池只在 C2 的中央映射里说一次，
  注册表里没有一条 lane 自报池，C2 自己的测试守着这条线；
* `deploy/macos/install.sh`：新增 `DALTON_ZERO_BASE_REVIEW_MODEL_TIER` / `..._PROFILE`
  一块，播种 `zero-base-review-model-config.json`；不设就打印一行 note、lane 整条不进 plist
  （argv fragment 以该文件为门）。没有新增 `deploy/connector-governance` 记录，
  所以 `DELIBERATELY_UNSEEDED` 不变。

---

## 3. 没做什么（以及为什么）

1. **没有独立 verifier。** 判断 lane 用第二个模型族做独立核验；零基复盘只有一次调用。
   理由：它的每一条产出要么是给人读的文档，要么是必须由人裁决的候选，人就是核验者；
   再加一对调用会让一条月度 lane 的成本翻倍而不改变任何一个决定的责任人。
   如果 owner 认为需要，加法与判断 lane 完全同形。
2. **没有走 `MissionDeliverableAuthority`。** 见 §1.1。代价：复盘不出现在 deliverable 链上，
   cockpit 的 deliverable 面板看不到它。要接的话是 `DELIVERABLE_KINDS` 加一个词
   + 一个 `publish()`，但那是集成时的决定，不该由一条并行切片单方面改热点文件。
3. **没有扩 `CHECKPOINT_KINDS`、没有扩 `AUTOMATION_WRITE_SCOPES`。**
   不需要，也就没有多一次 mission 版本发布。
4. **没有把 debate「不重要了」写回 DebateMap。** 复盘只说哪几条不重要，
   关闭一条 debate 是 DebateMap 自己的版本，由它的 lane 出，带自己的 `change_reason`。
   本切片只提案。
5. **没有 `TrackingCadenceVersion` 复用。** 见 §1.1。
6. **check pass 只覆盖 `screen_passed_companies`。** 篮子用整个 universe（没过闸的公司也是
   有价格的同行），但被评的判断只到住民为止。

---

## 4. 集成时要接的线

1. **mission 版本**：live mission 需要 `deliverable`（复盘本体）与
   `thesis_revision_candidate` + 同名 checkpoint（候选）。缺后者时 lane 照跑，复盘照写，
   summary 里 `candidates_ungranted` 记下有几条改写没能提案，并给出理由。
2. **模型配置**：`zero-base-review-model-config.json`（brain 层）。没有则 lane 不进 plist。
3. **cockpit 页面**：`cycle_reflection()` 现在多返回一个 `judgement_outcomes` 块
   （`available` / `checked` / `rows[{outcome,label,to_date,this_week}]` / `note`），
   HTML 端还没有画；建议挂在「每周回头看」那一页的表下面。
   这块也正好是 Chem §3.5「四格」里「上周产物验收」的一半。
4. **人裁决队列**：cockpit 的 revision 候选面板（`cockpit_plane` 2500 行附近那段）
   现在会收到 `origin: zero_base_review` 且 `reflection: null`、`zero_base_review: {...}` 的行。
   现有代码不会崩（多余的键被忽略），但要把四个答案显示出来需要在那段加一个分支。
5. **首跑顺序**：lane 的 `checks` 模式在 live 上第一次跑就会把历史判断全部评一遍并写进台账，
   之后每次 tick 只写变动的那些。若 owner 想先看不花钱的那一半，
   可以先只装 tracking policy 不装模型配置——lane 不会进 plist，
   但 `python -m dalton_core.zero_base_review_cli --mode checks` 可以手跑。

---

## 5. 验收

全量测试（`cd ~/Projects/dalton-w4-zero-base-review-worktree && PYTHONPATH=$PWD/src
.venv/bin/python -m unittest discover -s tests -t .`）：

```
Ran 5486 tests in 444.615s

OK (skipped=1)
```

新增测试文件：

| 文件 | 数 | 覆盖 |
| --- | --- | --- |
| `tests/test_judgement_outcome.py` | 34 | 窗口算术、两条检查的每一个结果词、权威的版本链与拒绝、触发器、可重放 digest、schema 登记 |
| `tests/test_zero_base_review.py` | 47 | cadence 的六种情形、提示词、封闭 schema 的九种拒绝、模型调用、权威、候选与既有裁决回路、四处登记 |
| `tests/test_mission_zero_base_lane.py` | 12 | tick 的每一个分支、崩溃子进程不推进 watermark、授权缺失、plist 门 |
| `tests/test_judgement_outcome_reflection.py` | 12 | reflection metric 的在场与缺席、`inputs_hash` 变动、narrative、cockpit 面板、CLI 端到端 |
| `tests/zero_base_fixtures.py` | — | 共用夹具（价格序列是字典；判断行走真 schema 与触发器） |

（那一条 skip 是主线既有的、与本切片无关的跳过。）

改到的既有测试三处，都是本切片踩到的既有不变量，改的是不变量的表述而不是不变量本身：

* `tests/test_research_cycle_reflection.py`：`compute_metrics` 从八条变九条；
* `tests/test_crowd_source_lane.py`：`dispatch_zero_base_review` 加进「不取证据的 lane」集合
  （与 research_task / reflection / conviction_call 同类，它一条证据也不取）；
* `tests/test_purpose_tiers_cover_every_registered_purpose.py`：新模块加进被扫描的清单。

**确定性**：没有网络、没有模型调用（`FakeModel` 直接返回测试给的 JSON）、
没有挂钟——`now` 一路注入（`review_state`、`build_context`、`run_zero_base`、
lane coordinator 的 `clock`），权威的 `clock` 也可注入。

---

## 6. 留给 owner / 主 agent 的问题

1. 零基复盘要不要独立 verifier？（现在没有，理由见 §3.1）
2. 复盘要不要同时进 `MissionDeliverableAuthority` 的 deliverable 链？
   要的话需要 `DELIVERABLE_KINDS` 加 `zero_base_review` 一个词。
3. 月度是 owner 的日历月（`YYYY-MM`，本地时区）。若想要「距上次复盘满 30 天」而不是「换月」，
   是 `review_state` 里一行的改动，但语义不同：换月会让 1 月 31 日与 2 月 1 日各出一版。
4. `should_have_moved` 的窗口现在跟 `price_divergence` 共用 10 个交易日 / 6%。
   事后验证也许想要更长的窗口（一个季度）；那要在 tracking policy 里另开一组阈值，
   属于 policy 决定而非代码决定。
