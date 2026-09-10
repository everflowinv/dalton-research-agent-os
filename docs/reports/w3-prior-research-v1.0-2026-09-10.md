# W3 既有资料的入职处理 v1.0

日期：2026-09-10
分支：`w3-prior-research`（worktree `~/Projects/dalton-w3-prior-research-worktree`），基线 main `189ab19`
规格：[并行开发计划 v1.0 §「既有资料的入职处理」](parallel-development-plan-v1.0-2026-09-09.md)、ADR-0005 / 0007 / 0008
owner 的话：**有些公司我们已有资料。新分析师先读它——能省一周——然后仍然自己写一版，因为资料可能过时，市场在问的问题已经变了。**

---

## 0. 一句话

既有资料被做成**一个受治理的来源**（`prior-research` 连接器，证据层级 `internal_prior`，超过 180 天降级）
和**版本链的第 0 版**（旧 Initial Screen 导入为 v0，Dalton 写的是 v1 并指回 v0），
而不是被做成「认知」——没有任何一条路径能让旧文档里的数字变成一个 figure，也没有任何一条路径能让 v0 算作「已经写过了」。

---

## 1. 设计：五条规则，每一条都是某处的一个拒绝

### 1.1 `as_of` 是硬要求，没有退化梯子

`prior_research_core.py` 读一个**声明的**目录（`DALTON_PRIOR_RESEARCH_DIR`），一家公司一个文件夹，
一个文件夹一份 `manifest.json`。对布局**宽松**（子目录、空格、中文文件名都行；owner 保留他现有的整理方式），
对日期**严格**：条目没有 `as_of`、或者日期解析不了，就带理由被拒绝，永不进入枚举。

company-wiki 那条 feed 可以从 period 退到 published_at 再退到 retrieved_at；这一条不可以。
一份 2024 年备忘录的 retrieval date 是今天。**日期被猜错的 prior view，会在猜错的那一天被当成当前观点读。**

拒绝是**上线的**（wire 上的 `refused` 数组），不是日志里的。
「给这份 memo 补个日期」是三十秒的事；一份悄悄没枚举成功的文档是没人知道要去修的事。

```
{"company": "ACN", "relative_path": "2024/undated.md",
 "reason": "manifest entry is missing ['as_of']"}
```

### 1.2 `internal_prior` 层级，以及唯一会随时间降级的一层

`claim_index_authority.IMPORTANCE_TIERS` 从五词变六词，`internal_prior` 插在
`management_statement` 和 `sell_side` 之间——按 owner 的理由：我们自己以前的活儿常常是楼里最好的东西，
但它仍然不是公司在说话。

它也是**唯一**带年龄规则的一层，这个不对称就是设计本身。
2019 年的一份 10-K 不会因为老了就变得「不那么 filed」——它仍然是公司为那个期间公布的那个数字。
prior view 是唯一一种全部内容就是「我们当时这么想」的 claim，因此也是唯一一种时间流逝本身就是反面证据的 claim。

- 阈值：`STALE_AFTER_DAYS = 180`（两个季度 = 两次财报 + 两次电话会）。进了 `RULE_TAGGER_HASH`，
  所以改阈值会让受影响的条目重新出版本，而不是拿新规则默默复用旧答案。
- 降级：`STALE_DOWNGRADE = {"internal_prior": "sell_side"}`——一格，写成表而不是算出来的，
  因为「降一格」是实现，「一份过期的内部观点和卖方研报同级」是一个应该能被反驳的决定。
- 落点：`importance_basis` 追加 `;may_be_stale:<age>d><threshold>d:<原层级>`。
  没有新加布尔列：`importance_basis` 本来就是「这条 claim 为什么是这个分量」那一栏，
  而条目契约的字段集是封闭的——加一个 flag 是为了说一句原因串已经说了的话而改 schema。

### 1.3 `internal-prior-document` grade：有，且永远不是 figure

`document_figure_grade.INTERNAL_PRIOR` 有 basis 也有 qualifier（读者读到一条 claim 时会被告知这是什么），
并且**故意**不在 `GRADES`、不在 `GRADE_BY_SPEC`、不在 `FIGURE_ADMISSIBLE_GRADES` 里。
`NON_FIGURE_GRADES` 把这个排除写成一个可被测试读到的名字，而不是一句注释。

`GRADES` 就是执行本身：`record_document_figures` 拒绝不在其中的 `source_grade`，
figure 表的 SQL CHECK 也只认那两个词。所以 prior 文档可以被定级，永远不可能成为一行 figure，
而一行 figure 正是定量 Claim 被提升的来源。
我们自己模型里的 FY26 收入不是关于 Accenture 的数字，是关于我们 2024 年假设了什么的数字。

### 1.4 旧 Initial Screen = v0，Dalton 写的是 v1

`mission_deliverable_versions.version_number` 的 CHECK 从 `>= 1` 放宽到 `>= 0`
（附一个和 `_widen_kind_check` 同形的表重建迁移，因为 `CREATE TABLE IF NOT EXISTS` 不会回头看已存在的表）。
0 只有一个意思：**这个文档在这个系统之前就存在**。

四条拒绝撑住这个形状：

| 规则 | 在哪里拒绝 |
| --- | --- |
| v0 必须带 `change_reason: imported_prior`，且这个词只在 v0 合法 | `mission_deliverable.publish` |
| 一条链只能开始一次 | `publish`（v0 遇到已有 pointer 就是 `MissionDeliverableConflict`） |
| v0 什么都不主张：正文里的数字记成 gap，不是拒绝 | `validate_section(imported=True)` |
| v0 永不过闸：自评每一项 `imported` | `prior_screen_import.imported_gate` |

第三条值得多说一句。figure 规则的存在是为了阻止**这个系统**写它引不出处的数字；
v0 是「某份文档这么说」的记录，绑着文档的 ref 与 hash。
为了让旧 screen 能过闸而把里面的数字剥掉，会毁掉正在被导入的那个东西本身。
所以数字留在正文，并在 `gaps` 里留一条「上一版里的数字，未在本系统重新核对」。

第四条同理：拿本基金的出口门四问去评判一份 2024 年的文档，无论答「是」还是「否」都是编造。
所以有第三个词 `imported`（`GATE_ITEM_STATUSES = ("passed", "failed", "imported")`）。

**选择规则显式清掉 v0**（`initial_screen_cli._target`：`published.get("version") == 0` → 当作没有）。
按版本号，不按日期，故意的：否则「把旧 screen 导进来」这个动作本身就成了「新 screen 不会被写」的原因，
和导入它的目的正好相反。v1 的 `prior_version_ref` 是白得的——authority 从 pointer 里取。

### 1.5 起草上下文的「上一版」块，和出口门的 `delta_vs_prior`

起草提示词最后追加一块：

```
上一版（内部，2024-03，距今约 29 个月）——参考，不是结论：
上一版是我们自己以前的判断，不是事实，也不是本版的结论。逐条处理它的关注点与 debate：
仍然成立 / 已经变了 / 已经有答案，每一条都要用下面的 C、N 材料说明依据；
拿不出依据的，写成本版的未决问题。不要照抄上一版的结论、措辞或数字——
上一版的数字没有在本系统里核对过，本版的每个数字只能来自 N 标签。
【结论】…
  当时未决：…
上一版出处：…
```

年龄用**月**，因为判断真正转折的单位是月：四个月和十四个月是两个问题，181 天和 184 天是同一个问题。
块放在最后，所以「数字只能引 N 标签」那条硬规则先被读到；块本身再提醒一次。
没有上一版就完全没有这一块——大多数公司就是这样，而一行「上一版：无」是模型要读过去的噪音。

出口门自评新增 `delta_vs_prior`，**在 `answers` 旁边而不是里面**：
`passed = all(item["answer"] for item in answers)` 看不到它。
「相对上一版发生了什么」是 owner 要求每次重出都能看到的，同时也是一个答案经常就是「没什么」的问题——
一个在平静季度上失败的闸门是在惩罚一份诚实的文档。沿用 P14d `gate_recomputed` 的「只报告，不触发」先例。

内容尽量确定性：`new_filings`（prior 日期之后落地的 filing，逐条列）、
`debates_shifted`（当前 map 上 `shifting` 的、或 `first_seen_at` 晚于 prior 日期的）、
`price_move`。行情那一格在没有序列时返回 `{"available": false, "reason": ...}`——
「这个 Core 上没有行情」和「股价没动」是两句话，一次说「price flat」却根本没看过的重出，比一次说「我没看」更糟。

### 1.6 `PriorModelVersion`：读得到，永远算不进去

自己的表、自己的 authority，不是 `ForecastModelVersion` 里的一种形状。理由：
M2 的 assumption 行是**承重的算术**——它指的 driver 之所以存在是因为某张 filing 里有它，
它的值进 `compute_results` 再进已发布的预测行。旧模型的假设常常对应根本没有 filed driver 的行，
而且它们必须是**惰性的**。让一行惰性的唯一办法，是把它挡在会拿它做算术的那份 record 之外。

于是：`kind: prior_human`（这个词只存在于本模块）；不注册 provenance mode；
不往 `FIGURE_ADMISSIBLE_GRADES` 加 grade；不往任何地方传 `verified_figure`；
不 import `model_forecast` / `model_forecast_driver` / `forecast_reconciliation` /
`coverage_mission` / `claim_index_figures` / `research_verification` / `statement_snapshot`，
它们也不 import 本模块——箭头单向，有一个走 import graph 的测试守着。

- **公式逐字存文本**，不求值、不翻译、不规范化。`=B12*(1+B13)-B14` 是「一个人认为这门生意怎么运转」
  最压缩的表述；一旦被解析成这个系统会拿去算的东西，它就从一次判断的记录变成了对一个数字的主张。
- **值是 decimal 字符串，record 里没有 float**。Excel 给的是二进制浮点，这里取 `repr()`——
  最短可回环的字符串，也正是表格显示给写它的人看的那个。不做任何 round：
  一个旧模型把比率保留到几位本身就是信息，在这里挑一个 quantum 要么发明精度要么销毁精度。
- **单位是猜的，猜带 basis**：`number_format` → `label` → `unknown`。梯子很短，
  因为错的单位比没有单位更糟（`prior_assumption_bands` 遇到单位不一致的行会拒绝给区间）。

`prior_assumption_bands(connection, company_ref, driver_label, unit=None)` 返回
`{matched_on, unit, count, rows, low, high, as_of_from, as_of_to, reason}`。
按名字匹配，两级梯子（`exact` / `contains`）并报告站在哪一级——
旧模型的 label（「Consulting revenue growth」）不会等于 filer 的 XBRL label（「Revenues」），
所以空区间是常态，返回的是带理由的空区间而不是一个静默的 0。
**不给均值也不给中位数**：三年里手打的四个数不是一个分布，给它一个集中趋势是把四个数打扮成一个统计量。

### 1.7 带日期的 `prior_view`

DebateMap 的 debate 形状和 ThesisReflection 的契约各新增一个**可选**的 `prior_view`。
放在 `market_view_vs_ours` 旁边而不是里面：街上的立场和我们自己以前的立场是两个不同的争论对象，
揉在一起就看不出正在跟哪一个争。

「可选」是**真的缺席**，不是默认 null。DebateMap 每次读都会从规范化后的 debate 重算 content hash，
所以一个「热心地」插入 null 的解码器会让这次改动之前发布的每一张 map 读不出来。
`_closed()` 因此长出一个 optional-key 模式，并在 docstring 里说明为什么。

`as_of` 两边都是必填，且**永不默认成今天**：prior view 唯一必须带的东西就是它的年龄，
一个盖着「被读到的那天」的 prior view 会被当成当前观点来争论。

`attach_prior_views` 把**同一条**最新的 prior view 挂到每一条 debate 上，是故意的。
「我们 2024-03 时怎么看这家公司」是关于这家公司的一个事实；
判定那份旧 screen 在回答六条 debate 里的哪一条，会是一次没人核对过的匹配，
而 debate map 的全部纪律就是：记录在案的立场是某个能被追问「为什么」的人放上去的。

---

## 2. 接受的格式

| 后缀 | wire 上的 `doc_format` | 怎么读 | 缺依赖时 |
| --- | --- | --- | --- |
| `.md` / `.markdown` | `markdown` | 原文逐字（含 frontmatter） | — |
| `.txt` / `.text` | `text` | 原文逐字 | — |
| `.pdf` | `pdf` | 复用 web lane 那条可选 `pypdf` 路径；加密 / 超 400 页 / 无可抽文本都拒绝 | 带理由拒绝（`[pdf]` extra） |
| `.docx` | `docx` | 手写二十行走 zip 读 `word/document.xml` 的段落 | — |
| `.xlsx` | `xlsx` | 阅读用的扁平文本；**数字走 `prior_model_import` 逐格读** | 带理由拒绝（新的 `[prior-models]` extra） |

`.docx` 不引依赖：wordprocessingml 的正文就是段落套 run 套 text，读它二十行，
为二十行加一个库等于把一个版本号写进这个连接器的 adapter identity 而毫无所得。

`openpyxl` 3.1.5 现在只是 `cn-hk-data` 的传递依赖，而 `install.sh` 装的 extras 里**没有** `cn-hk-data`——
所以 live Core 上根本没有它。新增 `[prior-models]` extra 显式声明；没装就带理由拒绝，绝不猜。

---

## 3. owner 要做的事

1. **放文件**。选一个目录，一家公司一个文件夹：

   ```
   ~/Documents/dalton-prior-research/
     ACN/
       manifest.json
       2024/screen.md
       2024/model.xlsx
       memo-2023.docx
     EPAM/
       manifest.json
       ...
   ```

   文件夹里怎么摆随意；被读的只有 `manifest.json` 列出来的东西
   （文件夹不是意图声明，一份顺手下载的东西不是既有研究）。

2. **写 manifest**。每家公司一份：

   ```json
   {"documents": [
     {"path": "2024/screen.md", "kind": "initial_screen", "as_of": "2024-03-28",
      "author": "human:pm", "source_note": "FY24 Q2 电话会之前写的"},
     {"path": "2024/model.xlsx", "kind": "model_excel", "as_of": "2024-06-30",
      "author": "human:analyst", "source_note": "还在维护的那版"},
     {"path": "memo-2023.docx", "kind": "memo", "as_of": "2023-11-02",
      "author": "human:pm", "source_note": ""}
   ]}
   ```

   `kind` 五选一：`initial_screen` / `memo` / `notes` / `model_excel` / `other`。
   `as_of` 必填，`YYYY-MM-DD`；**没有日期的条目会被拒绝并在 wire 上说明**，你会在 summary 里看到它。
   `author` / `source_note` 可以留空字符串，但要在。

3. **声明目录并安装**：

   ```sh
   DALTON_PRIOR_RESEARCH_DIR=~/Documents/dalton-prior-research bash deploy/macos/install.sh
   ```

   这一块要么把两条治理记录 + 语料软链 + feed plan 全放下，要么一样都不放并打印
   `note: set DALTON_PRIOR_RESEARCH_DIR ...`。软链而不是拷贝：你还会往这个目录里加东西，拷贝会是第二份陈旧的真相。

4. **批两条记录**（都以 `proposed` 出厂）：

   ```sh
   dalton-connector-governance approve \
     ~/Library/Application\ Support/Dalton/connector-governance/prior-research-list-documents-v1.json \
     --approved-by human:coverage-owner
   dalton-connector-governance approve \
     ~/Library/Application\ Support/Dalton/connector-governance/prior-research-get-document-v1.json \
     --approved-by human:coverage-owner
   ```

   一次审批只覆盖一个 operation：「列出我们写过什么」和「打开其中一份」是两种权限。

5. **发一版 mission，授予的词**：读这条 feed 需要 `source_discovery` + `observation`
   （live mission v4 已经有）；把旧 screen 导成 v0 还需要 `deliverable`。
   两者分开检查，所以只授予前两个的 mission 照样会把 notes 与 memo 读进来，
   screen 导入等下一版 mission。

6. **要装 Excel 支持**：`pip install -e ".[prior-models]"`（或把 `prior-models` 加进 install.sh 的 extras 串）。
   不装的话工作簿会带理由被拒绝，其它文档照读。

---

## 4. 验收

全量：`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`

```
Ran 4454 tests in 666.119s

OK (skipped=1)
```

本线新增 `tests/test_prior_research.py`，60 项。逐条：

```
ManifestTests
  test_an_entry_with_no_as_of_is_refused_with_its_reason
  test_a_bad_entry_does_not_take_the_folder_with_it
  test_a_manifest_that_will_not_parse_is_not_partial
  test_the_kinds_are_the_owner_s_five
  test_a_duplicate_path_is_refused_rather_than_read_twice
  test_the_root_is_declared_by_an_environment_variable
  test_age_is_counted_in_whole_months
CorpusTests
  test_a_window_enumerates_the_dated_and_refuses_the_rest
  test_a_document_reads_back_verbatim_under_its_own_id
  test_a_path_that_leaves_its_company_folder_is_refused
  test_an_unreadable_format_is_refused_with_its_suffix
GovernanceTests
  test_the_committed_records_are_proposed_and_match_the_builder
  test_one_approval_covers_one_operation
ChildTests
  test_a_listing_lands_with_its_tier_its_as_of_and_its_refusals
  test_a_document_acquires_with_a_manifest_dated_by_the_owner
  test_an_unapproved_or_wrong_capability_record_stops_the_run
ImportanceTests
  test_internal_prior_sits_between_management_and_the_sell_side
  test_the_feed_spec_carries_the_tier
  test_a_prior_view_is_downgraded_exactly_at_the_threshold
  test_only_the_prior_tier_ages
  test_an_undated_prior_claim_is_not_aged
  test_the_rule_tagger_reads_the_threshold
GradeTests
  test_the_prior_grade_exists_and_is_never_a_figure
  test_it_still_tells_a_reader_what_it_is
PriorModelTests
  test_a_workbook_keeps_its_formulas_verbatim_and_its_values_as_text
  test_no_float_survives_into_the_record
  test_every_imported_cell_is_prior_human_and_nothing_else
  test_the_chain_reads_back_and_an_unchanged_workbook_is_a_duplicate
  test_a_band_is_a_range_with_its_rows_and_its_unit
  test_a_label_nothing_carries_is_an_empty_band_with_a_reason
  test_a_band_across_units_refuses_rather_than_averaging
  test_the_prior_model_is_not_reachable_from_the_number_paths
  test_decimals_are_exact_text_and_a_boolean_is_not_a_number
  test_a_unit_it_cannot_be_sure_of_is_no_unit
DeliverableV0Tests
  test_imported_prior_is_in_the_vocabulary_and_only_on_a_v0
  test_a_v0_needs_the_import_reason
  test_a_prior_screen_becomes_v0_and_dalton_writes_v1
  test_a_chain_can_only_start_once
  test_the_v0_gate_marks_every_item_imported
  test_a_prior_figure_is_a_recorded_gap_not_a_refusal
  test_an_imported_v0_still_counts_as_no_screen_yet
  test_a_real_v1_does_stop_the_next_draft
  test_the_drafting_context_carries_the_prior_block_and_the_instruction
  test_no_prior_version_means_no_block_at_all
  test_the_exit_gate_reports_the_delta_and_never_fails_on_it
  test_a_gate_with_no_prior_says_so_rather_than_guessing
  test_the_imported_gate_names_the_document_it_did_not_check
  test_a_screen_with_no_headings_arrives_as_one_section
PriorViewTests
  test_a_debate_map_reads_back_without_the_field
  test_a_dated_prior_view_is_accepted_and_an_undated_one_is_not
  test_the_field_is_attached_when_there_is_material_and_absent_otherwise
  test_no_prior_material_on_this_core_is_an_empty_list
  test_the_reflection_accepts_a_dated_prior_view_and_refuses_a_stray_ref
LaneTests
  test_the_lane_is_registered_at_order_thirty_two
  test_the_grants_it_needs_are_words_a_mission_can_hold
  test_it_is_in_the_budget_pool_and_the_cockpit_label_map
  test_the_schema_is_bootstrapped_and_rehearsed
  test_install_seeds_the_lane_all_or_nothing
  test_the_lane_refuses_until_the_authority_knows_the_source
  test_the_feed_plan_names_this_source
```

改到的既有测试，三处，都是「精确集合」型断言：

- `tests/test_model_forecast_driver.py` 的 `test_the_change_reasons_are_the_owner_s_five`
  改名为 `..._plus_the_import`，同时钉住前五个仍是 ADR-0008 的五个词、顺序不变。
- `tests/test_connector_inventory.py` 的连接器 slug 集合、`tests/test_connector_quota_policy.py` 的配额清单
  各加两行（配额清单是按字母序的）。
- `tests/test_service.py` 的 `LANE_SEEDS` 加一条 `prior_research`，走的是同一个「放全就亮、少一个就灭」的断言。
- `tests/test_s1_human_feeds.py` 的 feed-source 形状断言加了一个显式的
  `AWAITING_AUTHORITY_ROW = {"source:prior-research"}`——见 §5。

---

## 5. 集成时要接的线

1. **`coverage_mission.DISCOVERY_SOURCES` 缺一行**（本线不得改这个文件）。需要：

   ```python
   "source:prior-research": MappingProxyType({
       "connector_source_ref": "source:prior-research",
       "operation": "get_document",
       "document_ref_prefix": "prior-research-doc:sha256:",
   }),
   ```

   在此之前 lane 每个 tick 返回 `{"status": "unconfigured", "reason": "... not registered in
   coverage_mission.DISCOVERY_SOURCES ..."}`——缺席而不是半接。
   合入这一行之后，把 `tests/test_s1_human_feeds.AuthoritySeamTests.AWAITING_AUTHORITY_ROW`
   里的名字删掉（那个测试会告诉你该删）。

2. **cockpit**：`REGISTRY_LANE_LABELS` 已加（「读我们自己以前写过的东西」）。
   `cockpit_control.html` 本线未碰。值得给 owner 看的两样东西还没有面板：
   一条链上的 v0 与 v1 并排，以及某公司 prior-research 文档的拒绝清单（缺日期的那些）。

3. **`REQUIRED_WRITE_SCOPES`（`scripts/rehearse_deploy.py`）** 未加行：本线用的三个词
   （`source_discovery` / `observation` / `deliverable`）live mission 已经授予，没有新词要 owner 发版本。

4. **`document_provenance.TIER_BY_SPEC` 与 `deploy/phase9/p12c-debate-policy-v1.json` 的 `spec_tier`**
   都没有 `prior-research` 条目，因此抽取队列会把它当 `other`（最后读）、DebateMap 的独立性阶梯也不认它。
   前者是个可以商量的排序问题；后者是**对的**——`prior_view` 是我们自己的观点，
   本来就不该计入「几家独立来源在争」。建议只补前者，且由 W2 抽取那条线定值。

5. **`install.sh` 的 extras 串**没加 `prior-models`（改它要同步改 `tests/test_service.py:107` 的断言）。
   现状：Excel 导入在 live 上会带理由拒绝。要不要装由 owner 定——见开放问题 3。

---

## 6. 只读实况抽样（company-wiki，S1）

按 owner 要求数了一遍：company-wiki 语料里，五家覆盖公司有多少份能算作 `prior-research` 的 kind。
只读目录树，不读内容。

| 公司 | wiki 文件夹 | 非 index 文档 | 算作 prior-research kind 的 |
| --- | --- | --- | --- |
| ACN | 有 | 1（`2026-08-21-专家访谈`） | **0** |
| EPAM | 无 | — | 0 |
| CTSH | 无 | — | 0 |
| DXC | 无 | — | 0 |
| IBM | 无 | — | 0 |

**五家一共 0 份。** ACN 唯一那份是专家访谈——那是 S1 的领域（`expert` 层级），不是我们自己写的东西。

对照：整个 wiki 语料 135 家公司里，研究笔记 / 季度研究笔记 / Investment-Memo / 买方类文档共约 **105 份，分布在 46 家公司**
（另有 428 份 Flomo 笔记、64 份管理层纪要、52 份专家访谈、46 份券商研报）。
也就是说**素材是存在的，只是不在这五家**。

这正好是这条设计的论据：owner 对这五家的既有资料不在 wiki 里（在他自己的盘上 / Excel 里），
所以「声明一个目录 + 一份 manifest」是对的形状，而不是「再爬一个语料库」。
同时也意味着 **W3 上线后短期内不会有输入**，直到 owner 完成 §3 的第 1、2 步。

---

## 7. 没做的事

- **不导入 wiki 里的 memo**。company-wiki 是另一条已合入的 feed，有自己的层级映射；
  把它的文档改判成 `internal_prior` 会改动一条在跑的线的语义。要做的话应该是 wiki 侧加一条
  `doc_type_key → internal_prior` 的映射，由那条线的 owner 决定。
- **不做「旧模型 vs actual 的自我校准对账」**（计划 §4 提到的后半句）。
  `prior_assumption_bands` 是原料；对账要读 `forecast_reconciliation`，那是 M3 的读侧，
  而本模块的依赖箭头是单向的（M3 可以 import 本模块，反过来不行）。
- **不让模型来填 `prior_view`**。契约、校验、材料读取都在了；
  debate map drafter 与 reflection 的提示词都会拿到材料，但 `attach_prior_views` 的确定性版本
  挂的是「整家公司最新那一条」，不做逐条 debate 的匹配（理由见 §1.7）。
- **不改 cockpit HTML、不发 mission 版本、不部署、不写 live 状态目录。**

---

## 8. 开放问题（要 owner 或主 agent 裁决）

1. **过期降到 `sell_side` 对不对？** 现在是降一格。另外两个可选值：降到 `news`（更狠，
   等于说「过期的内部观点不如一条新闻」），或者不降只标 `may_be_stale`（更温和，
   把判断完全留给判断层）。表在 `STALE_DOWNGRADE`，改一行。
2. **180 天对不对？** 按「两个季度 = 两次财报」定的。对财报驱动的公司合适；
   对一年只说两次话的公司（或者行业框架类文档）可能太短。要不要按 kind 分档？
3. **live 上要不要装 `openpyxl`？** 不装则 Excel 导入在 live 上永远是带理由的拒绝。
   装的话 `install.sh` 的 extras 串要加 `prior-models`，同时会引入 `et_xmlfile`。
   我倾向于装——旧模型的假设区间是这条线里最有价值的产出，而拒绝一个工作簿并不能提示 owner 去装库。
4. **旧 memo / notes 要不要也进版本链？** 现在只有 `initial_screen` 会成为 v0；
   memo 与 notes 只作为 Claim 进来。如果 owner 有维护中的 investment memo，
   `investment_memo` 那条链也应该有 v0——机制已经在了（`publish(as_version_zero=True)` 不认 kind），
   缺的是 `prior_screen_import` 里的一个分支和一个模板 ref。
5. **同一家公司有多份旧 screen 怎么办？** 现在「一条链只能开始一次」，所以只有最早/被选中的那一份能当 v0，
   其余以 Claim 形式进来。也可以让它们按 `as_of` 成为 v0、v-1……但负数版本号会把
   `version_number >= 0` 这条刚放宽的约束再撕开一次。建议保持现状：最老的那份是链的起点，其余是证据。
6. **`prior_view` 要不要逐条 debate 匹配？** 见 §1.7。要做的话是一次模型调用，
   应该在 debate map drafter 的契约里加一个 key，并让 verifier 也看到它——那是 P12c 那条线的改动。

---

## 9. 文件清单

新增：
`src/dalton_core/prior_research_core.py`、`prior_research_cli.py`、`prior_research_launcher.py`、
`prior_model_import.py`、`prior_model_schema.sql`、`prior_screen_import.py`、
`mission_prior_research_lane.py`、`tests/test_prior_research.py`、
`deploy/connector-governance/prior-research-{list-documents,get-document}-v1.json`、
`src/dalton_core/connector_inventory/{profiles,fixtures,proposals}/prior-research.json`。

共享文件的增量改动：
`connector_inventory.py`（一个 profile + 一段 output schema）、`connector_quota_policy.py`（两行）、
`connector_governance.py`（两个 kind）、`feed_acquisition.py`（一个 source ref、一个 tier）、
`mission_feed_lane.py`（`FEED_IDENTITY` / `FEED_DISCOVERY_SOURCES` 各一行）、
`claim_index_authority.py`（一个 tier + 一个迁移）、`claim_index_schema.sql`、
`claim_index_tagging.py`（一个 spec 行 + 时效规则）、`document_figure_grade.py`（一个 grade）、
`debate_map.py` / `event_judgement.py`（`prior_view`）、
`model_forecast_driver.py` + 三份字面副本 + `cockpit_plane.CHANGE_REASON_LABELS`（`imported_prior`）、
`mission_deliverable.py` / `mission_deliverable_schema.sql`（v0）、
`initial_screen.py` / `initial_screen_cli.py`（上一版块、`delta_vs_prior`、选择规则）、
`lane_registry.py`、`budget_pools.py`、`cockpit_plane.py`、`bootstrap.py`、
`scripts/rehearse_deploy.py`、`deploy/macos/install.sh`、`deploy/phase9/p9-us-it-services-feeds-v1.json`、
`pyproject.toml`。

未碰（禁区）：`writer_server.py`、`coverage_mission.py`(+schema)、`bounded_planner_driver.py`、
`macos_launchagent.py`、`cockpit_control.html`、`PROJECT_STATUS.md`。
