# INT1：把 Wave 1 四条线接到驾驶舱和安装脚本上 v1.0

日期：2026-09-09
分支：`int1-cockpit-install`（worktree `~/Projects/dalton-int1-cockpit-install-worktree`），基线 main `61f4255`（2,627 项）
角色：并行开发计划第 5 节的集成位。四条 Wave 1 lane 都在自己的报告里写了「集成时要接的线」然后一个字都没动
驾驶舱，因为那两个文件不归任何一个 lane agent。这一片就是那些线。

---

## 0. 一句话

公司卡现在有股价、估值（带 basis）、模型完成度、质量分和 PM 的五个反馈按钮；lane 面板把「缺授权」
「没装上」「等你批准」和「闲着」分成四个词；问答上下文走 P12b 的索引，同一件事只进一次 prompt；
install.sh 种两份 yfinance 治理记录并装 `market-data`；`write_owner_only` 自己建父目录。
全量 2,671 项通过。

---

## 1. 为谁接了什么

### A（P11a / P11c 市场层）

- **公司卡加了价格块**：最新 close、`as_of`、货币、区间涨跌（默认约一个交易年，**并把起算日印在旁边**），
  以及 `provisional` 标志。盘中价在页面上是一句话——「盘中价，当天还没收盘」——不是一个布尔值。
  A 的报告第 4 节第 5 条说得很清楚：盘中拉到的当天 K 线和收盘价形状完全一样，卡片不说就是在告诉
  owner 市场收在一个它从没收过的价位上。
- **估值块**：四个指标各自的值、历史分位，以及**每个分位的 basis**。`price_only` 在页面上写成
  「只反映股价高低（这段历史里基本面没有变过）」。`unavailable` 的指标显示它自己的理由句子，不留空白
  （空白会被读成零）。底部一句 shares basis：没有历史股本来源时，历史市值用的是今天的股本。
- **lane 状态词表**（A 报告 3.4 最后一条，也是这一片最实的一块）：见第 2 节。

### B（P12b Claim 索引）

- **`cockpit_plane._claims` 不再直接喂给问答**。新增 `_indexed_claims()`，把行交给
  `company_research_view.annotate_with_index(core, rows, ref_key="ref")`，默认 canonical-only。
  答案下面多了一句「重复的 N 条已合并」，`result["duplicates_dropped"]` 是那个 N。
  **顺序没有动**（仍是最老在前）：哪些 Claim 能挤进 prompt 预算是按时间决定的，在这里偷偷改成按
  importance 排会改变模型看得到什么。改成把 `importance` 作为一个字段写进每条 Claim 那一行
  （`C7 [ACN; 2026Q3; 2026-09-01; filing]`），让模型知道 filing 和新闻不等重。
- **结论浏览**（新路由 `/v1/cockpit/claims`）：按公司 / aspect / importance / canonical 四个过滤器，
  aspect 与 importance 都以中文短语呈现（`guidance_style` → 「指引风格」，`filing` → 「公司报表原文」）。
  排序用 `index_order`（未标注的排在所有已标注之后）。**没有索引的 Core 会明确报错**而不是安静地返回空：
  「这个 Core 还没有给结论建索引，按主题或来源筛选在这里答不了」。这不是新视图——它开在已有的 reader
  浮层里，ADR-0006 的五个视图没有变成六个。
- **`OPERATION_FIELDS["company_research_query"]` 加了五个字段**（`index_aspect` / `as_of_from` /
  `as_of_to` / `importance` / `canonical_only`）。在此之前这些参数进程外根本调不到。

### C（P13-M2 预测行）

- **公司卡加了模型块**：`model_readiness(record)` 的全部字段（`forecast_quarters`、`drivers` /
  `drivers_with_assumptions` / `drivers_without_history` / `drivers_without_a_role`、
  `results_computed` / `results_partial` / `results_unavailable`、`assumption_kinds`、
  `realised_quarters`、`actual_cells`、`superseded_estimates`），加上记录自己的 `change_reason`
  （中文）、`decision` 和版本号。**一个百分比都没有**——C 拒绝给模型打分，这里也拒绝。
- **模型表在新路由 `/v1/cockpit/model?company=` 上**，用的是 `company_model_report.render_forecast_model`
  本身，不是驾驶舱重新排一遍版。理由是那个渲染器把每条假设的 `because` 印在假设下面、把 unavailable 的
  理由印在数字该在的位置——重排一次正好丢掉这两样。卡片上是「看模型表」按钮，表开在 reader 浮层里。
  （**与 C 报告 6.3 的差别**：表没有塞进 overview 的 JSON。五家公司的表加起来是几十 KB，overview
  是每次刷新都拉的。）

### D（Q1 质量回路）

- **每份已发布交付物带它的质量分**：rubric 标题、平均分与最低分、每条标准以**它自己的问句**呈现
  （不是 criterion id）、低于及格线的标准单独列出、确定性检查逐条带中文标签、以及有没有独立复核确认过。
- **五个反馈按钮**（读过 / 有用 / 证据不够 / 不同意 / 要重写）加一个可选备注框，出现在：公司卡的交付物
  块、交付物全文页的末尾、以及每一条问答答案下面。走新的 writer op（见下）。
- **答案也是交付物**：`_answer` 用 `research_quality_score.artefact_from_ask_answer` 算出
  `target_ref` 与 `target_hash` 并放进 `result["feedback"]`。
  **与 D 报告 6.2 的一处差别**：`target_ref` 用的是 `cockpit-ask:<request_id>` 而不是 `<job_id>`。
  同一个 request 重放会得到同一个答案但不同的 job id，用 job id 会把同一个答案的反馈劈成两条链。
- **`writer_server.record_analyst_journal_entry`**（唯一一个新 op，additive）：
  参数 `{target_ref, target_hash, target_kind, verdict, company_ref?, note?, score_override?,
  idempotency_key?, actor_ref}`，`actor_ref` 由 writer 从认证主体绑定（`OPERATION_ACTOR_FIELDS`），
  在 `HUMAN_GOVERNANCE_OPERATIONS` 里，所以 automation 主体在跑到 authority 之前就被拒了；
  authority 自己再拒一次非 `human:`。一件事两道门是刻意的：这是唯一承载人的判断的地方。
  - **与 D 报告 6.2 的第二处差别**：`idempotency_key` 不是 `cockpit:{login}:{target_ref}:{verdict}`，
    而是 `cockpit:` + 这三样的内容哈希前 32 位。login 是邮箱，而 actor 被刻意做成它的哈希
    （`_subject_for_login`）——把邮箱原样写进 Core 的幂等键会把那个设计撤销掉；顺带也是唯一无论
    target_ref 多长都不会超过键长上限的形式。

---

## 2. lane 状态词表（A 点名要的那件事）

`_lane_states` 拆成两半：原来四行（web / alphaengine / extraction / weekly，带各自的预算说明）保持
不动，后面按 `lane_registry.registered_lanes()` 每条 lane 一行（去掉已经被前四行代表的
`mission_source_discovery` 与 `document_extraction`），共 13 行。状态来自心跳里 tick summary 的
`driver_key`，note 来自它的 `reason`，没有 reason 时用 `skipped[].reason` 汇总。

| 状态 | 页面上的话 | 什么时候 |
| --- | --- | --- |
| `idle` | 待命 | 装好了、有授权、这一轮没事做 |
| `ungranted` | 缺授权，一次也没跑 | mission 的 `may_write` 里没有这条 lane 的词 |
| `unconfigured` | 还没装上 | writer 上没有它的 launcher |
| `unapproved` | 等你批准数据源 | 治理记录在盘上但 `status != approved` |
| `unstarted` | 这台机器上还没跑过 | tick summary 里根本没有这个 key |

`unapproved` 是怎么判出来的：调这条 lane 自己的 `argv_fragment(LaunchAgentContext(state=...))`，
看它输出里有没有一个落在 `connector-governance/` 下的路径，有就读那份记录的 `status`。
**用的是 lane 自己已经在声明的那件事**——安装脚本和 plist 都从同一个片段推导——而不是在驾驶舱里另立一张
表，那会变成第三份按自己的节奏过期的意见。

页面上这四个词各有自己的圆点颜色：等 owner 的三个是琥珀色，不是灰色。

---

## 3. install.sh

只加、不重构：

1. `pip install ...[deploy,pdf,sec-financials,market-data]`——多了 `market-data`（yfinance）。
   没有它 lane 会带理由拒绝，也就是永远不跑。
2. 照 roic 块的循环写法种两份记录，copy-once + `chmod 600`，**两份都仍是 `proposed`**：
   `yfinance-daily-prices-v1.json`、`yfinance-analyst-estimates-v1.json`。
3. **没有为不在 main 上的 lane 种任何东西**（guidepoint 自己的 lane、xueqiu / x-xreach /
   employee-reviews / sales-notes / company-wiki / cn-hk-findata）。脚本里留了一句注释说明原因：
   给一条不存在的 lane 种一份治理记录，等于给 owner 留一个关于「无」的批准动作。
   `tests/test_service.py::InstallerSeedTests` 有一条测试钉住这一点。

`--market-price-governance` 的 plist 断言加进了 `test_enabled_control_plane_gets_a_separate_launchagent`：
记录不在盘上时 plist 一个字都不多，放上去之后参数指向那份记录的绝对路径。

---

## 4. 另外三件小事

- **`lane_child_launcher.write_owner_only` 自己建父目录**（`path.parent.mkdir(parents=True,
  exist_ok=True)`）。S3 发现的：一个还没做任何事就拒绝的 child，会通过这个函数写它的拒绝理由，而运行
  目录是起 ticket 的时候才建的，于是它死在 `FileNotFoundError` 上——丢掉的正是解释那次拒绝的那句话。
  两条测试（`tests/test_lane_child_launcher.py::OwnerOnlyWriteTests`）。
- **模型配置名 `claim-index-model-config.json`** 在 `claim_index_tagging` 导入时通过
  `register_model_config_name` 登记（就在它已有的 `register_purpose("claim_index")` 旁边）。
  `scripts/raise_day_budget_cap.py` 在读注册表之前先 `load_lanes()` 并显式 import 这个模块——
  登记发生在 import 期，注册表只知道被 import 过的东西，而 claim-index 还不是 tick lane
  （见第 6 节）。D 复用 initial-screen 配置，无需接线，`install.sh` 的模型配置块一个字没改。
- **修了 `cockpit_plane.document()` 的一个真 bug**：它 `SELECT status, rationale, ...
  FROM coverage_mission_stage_records`，而那张表**没有 `rationale` 列**（理由在 `record_json` 里）。
  也就是说「阅读全文」这一页在有第一份交付物可开的那一刻就会抛 `no such column: rationale`。
  之前没人发现是因为没有测试打开过一份真交付物。现在有了
  （`test_opening_a_deliverable_reads_its_gate_reason`）。

---

## 5. 老 Core 的退化（这一片最花力气的一半）

驾驶舱要跑在比这四条 lane 都老的 Core 上。每个读取器在自己那张表不存在时返回空，卡片上对应的块就是
`None`，页面还是那张页面：

```
$ cp /private/tmp/dalton-ro/core.sqlite /tmp/int1-smoke/    # 只读副本，没有碰 live
overview in 1.02s
ACN  market= None valuation? False model? False feedback= None
CTSH market= None valuation? False model? False feedback= None
EPAM market= None valuation? False model? False feedback= None
IBM  market= None valuation? False model? False feedback= None
DXC  market= None valuation? False model? False feedback= None
feedback_enabled False
claims indexed? False total 2098
model view: CockpitError 这个系统还没有开始建预测模型
```

live Core 上这四张表一张都没有（四条 lane 还没部署），2,098 条 Claim 照常列出，1 秒出页。
`feedback_enabled` 为 false 时页面**不显示按钮**，而不是显示一按就报错的按钮。

---

## 6. 还需要 owner 做的事

1. **就地批准 yfinance 记录**。跑过 install.sh 之后：
   ```
   dalton-connector-governance approve \
     --path <state>/connector-governance/yfinance-daily-prices-v1.json \
     --approved-by human:<owner>
   ```
   在那之前驾驶舱会把这条 lane 显示成「等你批准数据源」。
   `yfinance-analyst-estimates-v1.json` 可以继续留 `proposed`，Wave 2 才有消费者。
2. **发一版新的 mission，`autonomy.may_write` 至少加 `market_price`**（A）。词表里已经有这个词，
   live mission 没授予；没授予时 lane 每 tick 返回 `ungranted` 且一次网络调用都不发，页面会如实说。
   同时建议加 `research_task`（词表已有，P14e 要用）。
   C 不需要新版本（`forecast_line` 已在 live manifest 里）。
3. **`claim_index` 这个词还不在 `AUTOMATION_WRITE_SCOPES` 里**（Wave 0 加的十个词不含它）。
   `coverage_mission.py` 在我的禁改清单上，所以没加。今天 `claim_index_cli.granted_scope` 会退回
   `claim` 放行，live mission v13 有 `claim`，所以能跑；要按 ADR-0004 给它自己的词，需要一次
   `coverage_mission.py` 的加词改动 + 一版新 mission。
4. **figure 准入的策略重签**：发布并签署一版治理 policy，其 `research_candidate_auto_commit.rules`
   列出 `research-auto-commit:mission-verified-figure:v1`
   （常量 `research_verification.MISSION_VERIFIED_FIGURE_RULE_REF`，做法与 ADR-0005 的
   `research-auto-commit:mission-document-qualitative:v1` 一致）。在那之前 B 实现的是人工审阅那条路，
   自动准入分支还没接。
5. 部署时记得 `pip install -e '.[market-data]'`（install.sh 已经带上，手工升级的机器要补）。

---

## 7. 没做的 / 留给下一片的

- **B 的 claim-index lane 没有注册**。`mission_claim_index_lane.py` 里没有 `LaneSpec`，
  `lane_registry.LANE_MODULES` 里也没有它——B 的报告第五节把 LaneSpec 的字段列出来了但没有写进代码。
  加上它会改动 writer 的操作集与 LaunchAgent argv，超出这一片的交付范围，所以只记在这里：
  **今天索引没有 tick lane，只有手跑的 `claim_index_cli`**。驾驶舱那一侧已经准备好了——有索引就显示，
  没有就说没有。
- **模型表没有进 overview 的 JSON**，在自己的路由上（第 1 节 C）。
- **`tests/test_model_vocabularies.py::test_the_seed_is_what_the_script_tuple_said` 现在依赖类内的
  tearDown 顺序**：它断言注册表等于三个种子名，而 `claim_index_tagging` 一被 import 就会让它变成四个。
  同一个类里字母序更靠前的测试的 tearDown 会先把它重置回去，所以全量和单跑都过（都验证过），
  但这是一个顺序上的巧合。那个文件不归我，所以只记录。
- 没有跑过任何真实模型调用；没有碰 live 状态目录、没有部署、没有发 mission 版本。

---

## 8. 验收（原文）

`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`：

```
Ran 2671 tests in 298.629s

OK (skipped=1)
```

基线 main `61f4255` 是 2,627 项，本片 **+44**：

| 文件 | 新增 |
| --- | --- |
| `tests/test_cockpit_wave1.py`（新） | 39 |
| `tests/test_service.py::InstallerSeedTests` | 3 |
| `tests/test_lane_child_launcher.py::OwnerOnlyWriteTests` | 2 |

`tests/test_cockpit_wave1.py` 覆盖的：老 Core 逐项退化为 `None`（含索引过滤器在无索引 Core 上的
明确拒绝）、收盘价与盘中价、分位的 basis、模型只计数不打分、模型表走 lane 自己的渲染器、
质量分以标准的问句呈现、无 judge 的分只有检查层、反馈以 owner 主体出去且重复点击是 duplicate、
词表外的 verdict 出不了驾驶舱、被拒的写入抛 `CockpitConflict`、writer op 的三张表登记与
automation 被拒、五个 lane 状态词各自可分辨且批准之后回落、canonical-only 与三个过滤器、
未标注的 Claim 显示且排在最后、三个新路由的查询串解析与 CSRF、页面确实说了那些词。

改动过的既有测试两处，都在我的所有权内且原因写在旁边：
`test_cockpit_plane` 的 lane key 断言改成「前四个不变、且注册表 lane 出现在后面」；
`test_cockpit_figures` 的 helper 改调 `_base_lane_states`（那四行才是有预算的那四行）。
