# P14b / P14d 修订回路：论点修订候选的人裁决，与过闸后的版本化重出 v1.0

日期：2026-09-09
分支：`w3-revision-loop`（worktree `~/Projects/dalton-w3-revision-loop-worktree`），基线 main `8110b23`，交付前已 `git merge` 到 main `64d3f94`
作者：Wave 3 `revision-loop` agent（Opus 5）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) §1（owner 第二批裁决：ADR-0007 接受；gate 重开必须版本化；机制 vs 判断）与 D5 行、[ADR-0007](../adr/0007-thesis-revision-candidates-and-verified-figures-in-the-ledger.md)、[ADR-0008](../adr/0008-research-outputs-are-never-terminal.md)、[ADR-0001](../adr/0001-thesis-confidence-and-coverage-admission.md)、[P14a 每日跟踪](p14a-daily-tracking-v1.0-2026-09-09.md)、[模型路由与回退 v1.0](model-routing-fallback-v1.0-2026-09-09.md)、[INT1 cockpit 与安装](int1-cockpit-install-v1.0-2026-09-09.md)、[P10c Initial Screen](p10c-initial-screen-v0.1-2026-09-07.md)

全量测试：

```
Ran 4011 tests in 394.933s

OK (skipped=1)
```

（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`；合并 main `64d3f94`（3,932）之后，本片 +79。）

---

## 0. 一句话

P14a 让机器能说出「这条 Claim 支撑的论点可能比我们说的弱」，这一片让人能回答它——接受就顺着 ADR-0001 原来的那几张表长出**新一版 thesis**，老版本一个字节不动；同一套版本化规则推广到 Initial Screen：过闸不再是终态，而是某一版的状态，证据底座从「缺」变「有」时机器提案、人批准、lane 重出一版带 `prior_version_ref` 与 `change_reason`；顺带把因为 verifier 钉死在一个已退役 profile 上而停泊的 thesis-impact 重新接通——新钉一版策略、离线重跑发布门、产出改道进候选记录。

---

## 1. 文件清单

| 文件 | 性质 | 是什么 |
| --- | --- | --- |
| `thesis_revision.py` + `thesis_revision_schema.sql` | 新增 | P14b 候选裁决权威：读未裁决候选（带 `ThesisReflection`）、记 accept / reject / defer（append-only，绑候选 content hash，只认 `human:`）、accept 时经 ADR-0001 原有的准入表长出新一版 `ThesisVersion` |
| `deliverable_reopen.py` + `deliverable_reopen_schema.sql` | 新增 | P14d 评估与提案：`reopen_assessment(company)` 按证据底座做「当时 vs 现在」的 diff；`GateReopenAuthority` 提案 / 人裁决 / 发许可；`approved_reopen()` 是选择规则唯一要读的东西 |
| `thesis_impact_reopen.py` | 新增 | verifier 相位钉的第二版、独立性预检、离线资格重跑（两条路径）、`service.json` 开关的规则、producer 产出 → `ThesisRevisionCandidate` 的接线 |
| `mission_reopen_lane.py` | 新增 | 每周一次、每家过闸公司一次的确定性检查 + 只读 CLI；lane order 118 |
| `writer_server.py` | 共享（加法） | 两个 human-governance op：`decide_thesis_revision_candidate`、`decide_gate_reopen`（照 INT1 的 `record_analyst_journal_entry` 五处登记） |
| `cockpit_plane.py` | 共享（加法） | approvals 视图两种新 checkpoint（`thesis_revision`、`gate_reopen`）+ `decide()` 两条分支 + `DECISION_WORD_LABELS` + lane 名字一行 |
| `mission_deliverable.py` | 共享（加法） | `publish(..., revision=...)`：ADR-0008 的 `change_reason` 闭合词表 + 证据 refs + `reopen_ref`，落进 record，**不进 body hash** |
| `initial_screen_cli.py` | 共享（加法） | 选择规则：有已批准且未被消费的 `gate_reopen` 时不再跳过 `gate_passed`（也跳过 staleness 规则）；重出时带 `revision` |
| `lane_registry.py` | 共享（加法） | `LANE_MODULES` 一行 |
| `tests/test_thesis_revision.py`、`test_deliverable_reopen.py`、`test_thesis_impact_reopen.py`、`test_mission_reopen_lane.py`、`test_revision_loop_writer_ops.py` | 新增 | 79 项 |

---

## 2. P14b：候选裁决回路

### 2.1 记录形状

**读**：`ThesisRevisionAuthority.undecided(company_ref=None)` 返回没有终局裁决的候选，每条挂上 P14a 写的 `ThesisReflection`（通过候选自己的 `reflection_ref`），外加 `deferred` / `last_verdict` / `last_reason`。人裁决时一次看到两样东西——提案，和「我们当时怎么想、可能漏了哪个 debate」。

**写**：`decide(candidate_ref, candidate_hash, verdict, reason, actor_ref, content=None)`，落 `thesis_revision_decisions`：

```
decision_id, candidate_ref, candidate_hash, thesis_ref, thesis_version_ref,
company_ref, candidate_decision, verdict ∈ {accept,reject,defer}, reason,
terminal ∈ {0,1}, resulting_thesis_version_ref, admission_candidate_ref,
admission_decision_ref, record_json, content_hash, actor_ref, created_at
```

三个触发器（`dalton_authorized()` 插入门、禁改、禁删）。

**`defer` 不是终态**，所以唯一键不在候选上而在裁决上：一串 defer 本身就是「这个问题挂了多久」的记录。只有 accept / reject 关闭候选。裁决 id 是 `(候选, 候选哈希, 判词, 理由, actor, 序号)` 的内容哈希，所以**同一个答案重发是 duplicate**（重试的 RPC 不该看起来像人改主意），**不同的答案在终局之后是 conflict**。

### 2.2 accept 走的是哪条路

走 ADR-0001 原来的那几张表，而不是另起一张：一条 `thesis_admission_candidates`（继承首版 admission 的 mandate / driver pack / template 绑定，加 `prior_version_ref` 与 `revision_candidate_ref`）、一条 `thesis_admission_decisions`（`verdict='admit'`，`rationale` 是人写的理由）、一条 `thesis_versions`（`version_number+1`、`prior_version_id` 指向上一版、`authority_kind='human_admission'`），然后把 `current_pointers` 指过去。老版本的行、`content_hash`、内容一律不动。这样查 `thesis_versions` 的人看到的是一条链、一种权威类型，修订不是「某些查询知道、另一些不知道」的第二类 thesis。

**`change_reason` 由候选算出来，人改不了**：

```
THESIS_WEAKENED｜人裁决的 thesis 修订候选 thesis-revision-candidate:<32hex>｜证据：claim-version:…、claim-version:…
```

确定性：同一个候选永远产出同一句话，所以按版本回放是回放，不是重新叙述。人可以传 `content` 把 thesis 改得比候选提议的更多（这是 op 的 `content` 字段），但 `change_reason` 会被权威覆盖成上面这句——**可以改论点，不能改「为什么改」**。

`claim_refs` / `catalyst_refs` / `falsifier_refs` 默认原样带过去：候选的 `evidence_refs` 是**促成**修订的东西，不是修订后的论点所**依赖**的东西，把前者悄悄升级成后者等于往 thesis 里塞一条没人选过的 ref。要动就显式传 `content`。

### 2.3 拒绝清单（都是拒绝，不是「还没做」）

| 情况 | 结果 |
| --- | --- |
| `automation:` / `system:` actor | `only a person decides a thesis revision candidate (ADR-0007)`——writer 先拒一次（op 只在 `HUMAN_GOVERNANCE_OPERATIONS` 里），权威再拒一次 |
| `NEW_THESIS` 候选 accept | 指回 `propose_thesis_admission`：ADR-0007 说新论点是覆盖准入不是修订，准入那条路有修订不会重新推导的绑定。reject / defer 仍然可以，这是它离开队列的方式 |
| thesis 没有 current pointer | 首版是准入（ADR-0001），没有链可以接 |
| 候选写的是旧版本 / 哈希对不上 | `this thesis is now at <v>, and the candidate was written against <v'>` |
| 新内容与当前版本逐字相同（`change_reason` 除外） | `a rewrite of an unchanged view is not a version (ADR-0008)` |
| `claim_refs` 里有账本里没有的 ref | `thesis ClaimVersion <ref> was not found` |
| 终局之后换答案 | `this candidate was already rejected` |

---

## 3. 重开 thesis-impact

### 3.1 相位钉出新版本，不是改旧的

`VERIFIER_POLICY_REF = model-routing-policy-version:dalton-openclaw-verifier:1` 只允许 `profile:gemini-3-7-flash`，而 broker 已经不提供它——同步目录之后该 profile 是 `retired`，verifier 路由会以 `profile_retired` **被拒**。相位钉是不可变的（它的哈希在每一次跑过的核验里），所以重指 = 新版本：

`thesis_impact_reopen.openclaw_verifier_policy_v2(created_at=)` → `…-verifier:2`，`prior_version_ref` 指向 v1，`version: 2`，其余（adapter、`family_independence_capabilities`、cost-then-version 排序）与 v1 逐字相同。允许的 profile 是：

```
profile:zai-glm-5-3            family zhipu-glm-5.3
profile:gemini-3-5-flash-lite  family google-gemini-3
```

也就是 `TIER_VERIFIER` 链去掉第一环。**第一环 `profile:claude-fable-5-1` 是故意去掉的**：brain 链的第二环是同一个 profile，producer 从 brain 层出来时 producer 与 verifier 会是同一个家族 `anthropic-claude-5`，路由会在 route 时以 `model_family_not_independent` 拒掉这一对。一个「对常见 producer 必然被拒」的钉子，就是这个模块要修的那个故障换个家族再来一次。所以钉的是**对每一条 brain 环都独立**的那部分，路由的家族过滤器仍然叠在上面。

`ensure_reopened_verifier_policy(router, created_at=)` 幂等：router 没见过 v1 时先用同一个纯函数重建 v1（新装 / 测试），live router 上 v1 已在，那里只是一次读——在 live 上重建会是哈希冲突，那正是不可变在起作用。

`independence_report(router)` 把路由那一行判据提前问一遍，对**每一条 brain 链家族**都问：每个被允许的 profile 是否已注册、是否 `retired`、是否声明 `verify`、家族是否与 producer 撞车。`live` 为真当且仅当至少有一个 profile 三项都过。

### 3.2 资格重跑：离线，两条路径，都不覆盖新钉的模型

`recheck_eligibility(source=)`，不打任何模型、不碰 broker socket：

| source | 打分对象 | 结果 |
| --- | --- | --- |
| `observed` | fixture 里录下来的输出 | 30 例里**只有 1 例**有录到的输出（8 月 Gate-2 那个 false positive），`automation_eligible=False`，理由是 `calibration coverage is incomplete` + `detection rate is below 90%`。这是发布门今天的真实状态，而且**不是模型不行，是覆盖不够** |
| `gold` | 完美 verifier 会给的答案 | 30 例全评，`detection_rate=1.0`、`high_severity_misses=0`、`automation_eligible=True`——证明语料、阈值（30 例 / 90% / 零高危漏检）和打分器接通且**能解锁** |

两条都带 `covers_the_new_pin: False` 和 `observed_families: ["deepseek-v4"]`，免得有人把「旧家族的重跑」读成「新钉模型的放行」。

### 3.3 产出改道

`route_impact_to_candidate(store, assessment=…, verification=…, thesis=…, claim=…, company_ref=…, mission=…, actor_ref=…)`，走两个权威的公开入口，按 P14a 的顺序：Claim → `ResearchEvent(kind="claim")` → `EventJudgement` → `ThesisRevisionCandidate`。不这样做的话账本里会出现一条背后没有事件的候选，而 cockpit 的「我们对什么做了什么决定」正好会在这条 lane 的提案处开个洞。

| producer 的词 | 五词判断 | 动作 | 结果 |
| --- | --- | --- | --- |
| `supports` | `THESIS_STRENGTHENED` | `revise_thesis` | 候选 |
| `weakens` | `THESIS_WEAKENED` | `revise_thesis` | 候选 |
| `no_change` | `NO_CHANGE` | `no_change` | 只记 judgement，不提案——周会要能回答「为什么没改」 |
| `insufficient` | —— | —— | `skipped`，仍走控制面原来的 backlog 一问 |

核验不是 `pass` 直接拒。mission 没授 `thesis_revision_candidate`（写入范围**和**同名 checkpoint 都要）时是 `queued` 并点名 ADR-0007，judgement 仍然留痕。judgement id 绑在 producer 的 result envelope 上，所以同一份 assessment 路由多少次都只产出一条 event、一条 judgement、一条候选；这一步记的花费是 0，因为 producer 那次调用早已在 thesis-impact 的预算账本里付过，再记一次会对着它从没花过的池子重复计数。

### 3.4 `service.json` 的开关变成一条规则

`flag_state(independence=…, mission=…)`：

> `thesis_impact.enabled` 为真，当且仅当（钉的策略版本 live）且（mission 同时授了 `thesis_revision_candidate` 写入范围与同名 human checkpoint）。

2026-09-08 关掉它是因为一次策略滚版让所有结果不合格；修法不是把它翻回去，而是让这个开关成为**两个可以被检查的事实**的陈述——下一次滚版会自己把 lane 停住，理由已经写好了。

---

## 4. P14d：版本化重开

### 4.1 一个必须先说的发现：出口门的四问在过闸版本上翻不动

D5 写的判据是「重跑出口门结构自评，任一项从缺变有即提案」。照字面跑一遍，答案是**什么也不会发生**，原因值得写下来，因为它决定了这一片的形状。

`initial_screen.assess_exit_gate` 问四个问题：

| check | 测的是什么 | 会不会因为证据变厚而翻 |
| --- | --- | --- |
| `source_base` | mission 的四项必读资料是否齐备 | **不会**——不齐就过不了闸，所以过闸版本上它已经是「有」 |
| `number_provenance` | 硬编码 `True`（发布路径已逐条校验过每个数字绑一条定量 Claim） | 不会 |
| `key_driver` | 第 4 节字数 ≥200 且引用 ≥3 条 Claim | 不会——是对**已经写好的那份文档**的度量 |
| `street_and_risk` | 第 4/5/6 节字数都 ≥120 | 同上 |

四问在一份**已过闸**的版本上按构造全是「有」。它们是发布门，不是重开触发器。

会翻的是它们底下的证据底座，也正是 owner 点名的那几项：报表行、逐条核对过的数字、有没有价格、有没有 consensus、每节能引多少条结论。这些都是账本里可以数的事实，而且都可以**按时点数**——这就是 diff 诚实的地方。基线不是「存下来的那份自评」（从来没存过：门的结果只以一行散文活在 stage record 的 `rationale` 里），基线是**把表针拨回过闸那一刻重新数一遍**。所以这份评估回答的问题只有一个意思：**今天我们知道了哪些当初写这份文件时不知道的东西？**

四问仍然重算、仍然报告（`gate_recomputed`），因为一次**退步**——资料底座少了一项、退役掏空了 thesis 节——是人该看见的；但它只报告，永远不触发。

### 4.2 评估的形状

`reopen_assessment(connection, company_ref, policy=None, gate=None)`：

- `passed_version()` 从 **stage record 自己的 `evidence_refs`** 找过闸的那一版，而不是「最新的那一版」——那是门被记录时的绑定，用最新版会让 diff 的起点悄悄移动（ACN 就有 v2）。
- `evidence_items(..., as_of=<过闸版本的 created_at>)` 与 `as_of=None` 各数一遍，逐项 diff：

```
{item_ref, label, threshold,
 was: {value, present, mark: "缺"|"有"},
 now: {value, present, mark},
 flipped, regressed, refs, note}
```

- `flipped` = `was.present == False and now.present == True`。`evidence_refs` 是**翻了的那些项**背后的 refs（filing ingest id、figure id、价格版本 id、claim version id），封顶 12 条。
- `assessment_hash` = (公司, 过闸版本, 策略 ref, 每项的 was/now/值) 的内容哈希——幂等键。

阈值在 `DEFAULT_POLICY`，可用 `load_policy(path)` 从 JSON 覆盖（闭合形状：多一个不认识的键就是打字错误，而阈值写进文件的全部价值就是「这一次跑用的就是这个」）：

```
min_statement_lines 100 · min_verified_figures 1 · min_market_price_versions 1
min_consensus_versions 1 · min_claims_per_section 3 · max_evidence_refs 12
```

`consensus` 这一项现在拿不出权威（P11b 是 Wave 2），代码检查 `sqlite_master` 后如实标注「现在不可能翻」，而不是报一个测出来的 0。

### 4.3 提案 → 人 → 重出

```
gate_reopen_proposals(proposal_id, company_ref, deliverable_ref, stage_ref,
  passed_version_ref, passed_version_hash, assessment_hash, flipped_count,
  checkpoint_kind='gate_reopen', change_reason, mission_version_ref,
  record_json, content_hash, actor_ref, created_at,
  UNIQUE(company_ref, assessment_hash))

gate_reopen_decisions(decision_id, proposal_ref UNIQUE, proposal_hash,
  company_ref, verdict ∈ {approve,decline}, reason, record_json,
  content_hash, actor_ref, created_at)
```

`propose()` 拒绝没有翻项的评估、拒绝没有证据 refs 的提案（ADR-0008）、拒绝不是 mission principal 的 automation actor。`decide()` 只认 `human:`，绑提案的 content hash，同答案是 duplicate、换答案是 conflict。

`approved_reopen(connection, company_ref)` 返回**已批准且未被消费**的许可。「消费」= 已有一版 deliverable 的 `record_json.revision.reopen_ref` 指着它。没有这条，一次批准会在每个 tick 上重出一次筛选，那正是 ADR-0008 警告的、披着版本号的噪声。

选择规则（`initial_screen_cli._target`）现在是：

```python
if entry["stage_status"] == "gate_passed" and reopen is None:
    skipped.append({... "reason": "initial screen already passed"})
    continue
...
if (reopen is None and published is not None
        and published["created_at"] >= own[-1]["created_at"]):
    skipped.append({... "reason": "nothing new since the last version"})
    continue
```

批准同时解掉 staleness 那条，因为促成重开的那个发现恰恰是「进来的证据不是这家公司的 Claim」——10,023 条报表行一条 Claim 都不是。

重出的那一版是 `publish(..., revision=reopen_revision(permission))`：

```
revision = {"change_reason": "evidence_thicker",
            "evidence_refs": [批准 id, 提案 id, 老版本 id, …翻项背后的 refs],
            "reopen_ref": 提案 id}
```

`change_reason` 走 `model_forecast_driver.CHANGE_REASONS` 那个闭合词表（不另立一份），没有证据 refs 直接拒。**`revision` 不进 body hash**：产出完全相同文档的重出仍然是 duplicate，理由再好也一样——ADR-0008 拒的是「对没变的世界的重写」，而理由不是变化。老版本的行、`content_hash`、`gate_passed` 的 stage record 全部不动；新版本自带 `prior_version_ref`，cockpit 的版本链读的就是这个。

### 4.4 lane

`mission_reopen_lane`，order 118（在判断层 116 之后——两者读同一周的到货，这条应该看见那条刚记下的；在 feed lane 120 之前——它便宜且确定，把取数卡在它后面没道理）。

**没有子进程**：这条 lane 不打模型、不碰网络，一次通过是每家公司五个 `SELECT COUNT(*)`。子进程能买到的隔离它没有对应的故障模式，却要付一张 ticket、一次 spawn、一次 settle。所以它在 controller tick 里跑，`budget_pool=None`、无 launchagent 片段——这三件事在 `LaneSpec.note` 里写着。

幂等两层：`(company, ISO week)` 是进程内的省事，`(company, assessment_hash)` 在权威里，是**真正**管用的那层（进程内的会随进程一起没）。授权判据是 mission 的 `autonomy.human_checkpoints` 里有没有 `gate_reopen`——没有 `may_write` 词，也不该有：提案没人裁决就一文不值，所以要看的是 checkpoint。

只读 CLI：`python -m dalton_core.mission_reopen_lane --core <path> [--policy p.json] [--company ref] [--json]`。只读打开、只打印 diff、什么都不写。

---

## 5. cockpit 与 writer

两个 op，照 INT1 的 `record_analyst_journal_entry` 五处登记（`HUMAN_GOVERNANCE_OPERATIONS`、闭合 `OPERATION_FIELDS`、`OPERATION_ACTOR_FIELDS`、构造/关闭、`_op_*` 方法）：

```
decide_thesis_revision_candidate: {candidate_ref, candidate_hash, verdict, reason, content, actor_ref}
decide_gate_reopen:               {proposal_ref, proposal_hash, verdict, reason, actor_ref}
```

两个都**不在** `CORE_OPERATIONS` / `CORE_DISCOVERY_OPERATIONS`：没有哪个 lane 能认证成的 principal 可以裁决这两件事。actor 由 writer 从认证 principal 注入，caller 传一个不一样的会被 `PermissionError` 拒；多传一个字段是 `ProtocolError`；automation principal 在 handler 跑之前就被 `governance changes require an authenticated human principal` 拒掉。因为 `governance_cli.ephemeral_call` 铸的临时 principal 的 operations 就是 `HUMAN_GOVERNANCE_OPERATIONS`，**加进那个集合就是全部的授权**，不用改 token 配置。

approvals 视图两行，标题是人话，没有机器 ref：

| kind | 标题 | 按钮 |
| --- | --- | --- |
| `thesis_revision` | 自动化说这条论点可能要改 | 接受，出新版本 / 不接受 / 先放着，再看看 |
| `gate_reopen` | 证据变厚了，是否重出这份 Initial Screen | 重出一版 / 不重出 |

论点那一行的 `details` 里带反思的三段（当时怎么想、实际发生了什么、可能漏了的 debate），重开那一行的 `summary` 直接是翻项：「已入库的财报报表行：缺（0） → 有（2926）」。两行都 `needs_rationale: True`——这两个决定都不该没有理由。`decide()` 里两条分支在把词表外的判词、空理由挡在 cockpit 内（一个临时 principal 都不铸）。

---

## 6. 只读 smoke：今天这四家会不会被提案

在 live 只读副本（`/private/tmp/dalton-ro/core.sqlite` 拷到 `/tmp`）上跑 `python -m dalton_core.mission_reopen_lane --core /tmp/w3rl-core.sqlite`：

| 公司 | 过闸的那一版 | 结论 | 翻了哪一项 |
| --- | --- | --- | --- |
| ACN `0001467373` | v2，2026-09-09T09:13:27 | **会提案** | 报表行 缺(0) → 有(2926) |
| EPAM `0001352010` | v1，2026-09-09T10:24:45 | **会提案** | 报表行 缺(0) → 有(3752) |
| IBM `0000051143` | v1，2026-09-09T10:28:56 | **会提案** | 报表行 缺(0) → 有(483) |
| DXC `001688568` | v1，2026-09-09T10:29:56 | **会提案** | 报表行 缺(0) → 有(2572) |
| CTSH `0001058290` | —— | 不评估 | `not_passed`（它是 `gate_failed`） |

四家都因为同一项翻了，而且这个翻是真的：live 里 33 份 statement filing 的 `recorded_at` 全部落在 2026-09-09 13:36–15:58，而四份筛选是 09:13–10:29 过的闸。**这四份 Initial Screen 是在这个系统一行报表都没有的时候写的**，之后这四家进来了 9,733 行（五家合计 11,831 行）。

不会翻的三项，各有各的诚实理由：`verified_figures` 在 ACN(1) 与 DXC(3) 上过闸前就已经是「有」，EPAM / IBM 到今天仍是 0；`market_data` 与 `consensus` 的权威还没落到这个 Core（P11a 在别的分支、P11b 是 Wave 2），后者代码里直接标注「现在不可能翻」；`claims_per_section` 四家过闸前就远超阈值（22–96 条/节），ACN 从 60 涨到 61 条/节，仍然是「有 → 有」，不构成翻转。

---

## 7. owner / 集成时要做的事

1. **发一版 mission**（P14b 生效的唯一条件）：`autonomy.may_write` 加 `thesis_revision_candidate`，`autonomy.human_checkpoints` 加 `thesis_revision_candidate`。缺任一个，判断层与 thesis-impact 的 `revise_thesis` 都记 `queued` 并点名 ADR-0007，其它动作不受影响。
2. **同一版 mission**（P14d 生效的唯一条件）：`autonomy.human_checkpoints` 加 `gate_reopen`。没有它 `mission_reopen` lane 每个 tick 回 `ungranted` 并说明理由，不写任何东西。
3. **注册 verifier 相位钉 v2**：对 live model-router 跑一次
   `ensure_reopened_verifier_policy(router, created_at=now)`。它幂等；live 上 v1 已在，只会追加 v2。之前请先跑目录同步（[模型路由报告](model-routing-fallback-v1.0-2026-09-09.md) §5 第 2 步），否则 `independence_report` 会报 `profile:zai-glm-5-3 is not registered with the router`。
4. **一次 live canary（必须的那一次）**：本片的资格重跑是离线的，`observed` 路径只有 1 例、`gold` 路径不是任何模型的成绩。要让 thesis-impact 真的能自动落效果，需要拿**新钉的 verifier**跑一遍 30 例校准语料：
   `scripts/run_real_thesis_impact_calibrate.py`（或 `thesis_impact_calibration_runner.run_live_calibration`）指向 `REOPENED_VERIFIER_POLICY_REF`，把输出喂给 `score_verifier_outputs`，要求 `automation_eligible=True`（30 例全评、检出率 ≥90%、零高危漏检）。**没跑之前不要把 `thesis_impact.enabled` 翻成 true**——`flag_state` 只检查「钉是活的」与「mission 授了权」，它检查不了「这个模型能不能干这活」。费用量级：30 次 verify 调用，`profile:zai-glm-5-3` 是 1.4/4.4 USD 每百万 token。
5. **`service.json`**：确认前四步之后，`thesis_impact.enabled` 置 `true` 并重跑 `install.sh`（plist 由 `macos_launchagent` 按这个标志渲染 / 删除）。`config` 块原样保留。
6. **重开的阈值**（可选）：若觉得 `min_statement_lines: 100` 太松，写一份 JSON 交给 lane 的 `--policy`；本片没有把它接到 `install.sh`，见 §9。

**先看再批的顺序**：先跑一次只读 CLI（§6 那条命令）看四家的 diff，再决定要不要在 mission 里放开 `gate_reopen`。批准是不可逆的（append-only），但它只批**一版**：重出之后许可就被消费掉了。

---

## 8. 测试

新增 79 项，五个文件。原文行：

```
Ran 4011 tests in 394.933s

OK (skipped=1)
```

覆盖的判据，逐条对应任务书：

- **候选裁决（accept / reject / defer）**：`test_thesis_revision.DecisionTests.test_accept_appends_a_version_and_leaves_the_old_one_exactly_as_it_was`、`…test_reject_records_the_reason_and_writes_no_version`、`…test_defer_is_not_terminal_and_the_candidate_comes_back`
- **automation 被拒**：`…test_automation_can_never_decide`（三种 actor 前缀）+ `test_revision_loop_writer_ops.WriterDecisionTests.test_an_automation_principal_is_refused_before_the_handler_runs`
- **actor 绑定**：`…WriterDecisionTests.test_the_writer_binds_the_actor_and_refuses_a_spoof`
- **accept 之后的 thesis 版本链**：`…test_accept_appends_a_version_and_leaves_the_old_one_exactly_as_it_was`（v1 内容与哈希逐字不变、v2 的 `prior_version_ref`、pointer 移动）、`…WriterDecisionTests.test_the_operation_decides_the_candidate_end_to_end`
- **verifier 策略钉 + 独立性**：`test_thesis_impact_reopen.PolicyPinTests.test_v2_is_a_new_version_chained_to_the_pin_it_replaces`、`…test_the_pin_is_the_verifier_chain_minus_the_link_the_producer_shares`、`IndependenceTests.test_every_pinned_profile_is_independent_of_every_brain_link`、`…test_the_old_pin_is_not_independent_of_the_producer_it_would_check`、`…test_a_retired_or_unregistered_profile_makes_the_pin_dead`
- **producer → 候选（fixture，无模型调用）**：`RoutingTests.test_a_weakens_becomes_a_candidate_behind_an_event_and_a_judgement`、`…test_no_change_is_recorded_and_proposes_nothing`、`…test_insufficient_never_becomes_a_proposal`、`…test_an_unverified_assessment_is_refused`、`…test_without_the_grant_the_judgement_is_kept_and_the_candidate_is_queued`、`…test_the_same_assessment_routed_twice_writes_one_of_everything`
- **重开评估的 diff**：`test_deliverable_reopen.AssessmentTests.test_the_baseline_is_the_evidence_base_as_of_the_passed_version`、`…test_an_item_that_flips_proposes_and_one_that_does_not_exist_says_so`、`…test_the_diff_is_measured_from_the_version_the_gate_cited_not_the_newest`
- **提案幂等**：`…test_the_same_evidence_base_always_hashes_the_same`、`ProposalTests.test_one_proposal_per_company_and_assessment_hash`、`test_mission_reopen_lane.LaneTests.test_one_proposal_a_week_and_the_ledger_holds_the_line_after_a_restart`
- **重出是新版本、老版本不动**：`ProposalTests.test_an_approval_is_a_permission_that_one_version_spends`
- **选择规则的改动**：`SelectionRuleTests.test_a_passed_gate_is_skipped_until_a_person_approves_a_reopen`、`…test_an_approval_also_defeats_the_nothing_new_rule`
- **writer op 闭合字段**：`test_revision_loop_writer_ops.OperationContractTests.test_both_operations_are_human_governance_with_closed_fields`、`WriterDecisionTests.test_an_unknown_parameter_never_reaches_the_authority`
- **approvals 列表**：`CockpitApprovalTests.test_a_candidate_appears_with_a_plain_title_and_its_reflection`、`…test_a_reopen_appears_with_the_diff_that_argues_for_it`、`…test_each_button_routes_to_its_own_human_governance_operation`、`…test_a_word_outside_the_vocabulary_never_leaves_the_cockpit`
- **lane 登记**：`test_mission_reopen_lane.RegistrationTests.test_the_lane_is_registered_once_at_its_own_order`、`…test_the_lane_has_a_name_a_person_can_read`

没有一个测试打模型或碰网络。`test_mission_reopen_lane.CliTests` 用的是测试自己建的 Core，不是 live 副本。

---

## 9. 待办与开放问题

1. **重开阈值没有进 `install.sh` / launchagent。** lane 现在用代码里冻结的默认值，`--policy` 只有 CLI 用得到。要让 owner 能在不改代码的情况下调阈值，需要在 state 目录放一份 `reopen-policy.json` 并在 dispatch 里读它——但那要碰 `install.sh`（禁改），所以留给集成。
2. **`consensus` 与 `market_data` 两项现在结构上翻不动。** 前者的权威（P11b）是 Wave 2，后者（P11a）在别的分支。两条 lane 合进来之后，这两项会自动开始翻——**而且四家都会立刻再产生一份提案**（不同的 `assessment_hash`）。这是对的，但集成时值得知道会一次来四份。
3. **出口门的四问在过闸版本上按构造不会翻**（§4.1）。如果 owner 想让「文档本身退步」也能触发（比如 Claim 退役掏空了 thesis 节），需要一条与 `flipped` 分开的 `regressed` 触发路径——现在只报告不触发，因为「证据变薄了要重写」和「证据变厚了可以重写」是两个不同的决定。
4. **重出用的是当前 mission 版本的 playbook，不是过闸那一版的。** 版本链因此可以跨越 playbook 变更，这在回放「当时按什么方法写的」时要靠 deliverable 自己记的 `playbook_version_ref` 去读。够用，但如果 playbook 结构变了，v1 与 v2 的节数可能不同，`section_count` 会跟着变，从而改变 `claims_per_section` 的基线——目前 diff 用的是**过闸那一版的节数**（两边都用它），所以 diff 本身是可比的。
5. **`NEW_THESIS` 候选目前只能 reject / defer。** accept 要走 `propose_thesis_admission`，而那条路要人手工填 template / driver refs / mandate 绑定。把候选的内容预填进一份 admission candidate 是个明显的下一步，但它是 ADR-0001 的准入路径，不该由这一片顺手扩。
6. **`thesis_impact_reopen.route_impact_to_candidate` 还没有 lane 在调。** 它是接线与契约，调用点是 `thesis_impact_production` 的 runner——那是 `thesis_impact*.py` 的主流程，改它要动停泊中的生产路径。等 §7 的 canary 跑完、开关打开时一起接，那时才有东西可跑。
7. **`_target` 现在对 universe 里每家公司各查一次 `approved_reopen`。** 五家公司五次查询，没问题；universe 大到几十家时应该改成一次查询。
