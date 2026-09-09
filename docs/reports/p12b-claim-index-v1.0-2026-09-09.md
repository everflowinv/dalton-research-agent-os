# P12b：给 Claim 建索引，一个 Claim 字节都不改；已核验的数字有了进 Ledger 的门

*2026-09-09* · 分支 `wave1b-claim-index`，已合并 main `888a814`（Wave 0 在内）
· 依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 B 线、
[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) 5.2 P12b、
ADR-0003 / ADR-0004 / ADR-0005、ADR-0007（owner 已接受）

## review 之后改了什么（v1.1，合并 main `888a814` 之后）

| review 项 | 改动 |
| --- | --- |
| **B1** figure 路径与 ADR-0007 冲突 | 恢复 `transcript_core_authority` 的原拒绝；新增 provenance mode `mission_figure_authority`（material = figure 行 + 其 Core 链）；只收 `company-filed-document`，spoken 在 store / promoter / sweep 三处各自拒绝；rule ref 常量落地。**报告里的阻塞点因此消失**，live 五条 figure 全部促进、一条进了 Ledger。见第三、四节 |
| **B2** scale-less figure 崩溃 / 基类异常逃逸 | 数值字段统一走 `figure_candidate_numerics()`；sweep 捕获 `ResearchVerificationError`；百分比 figure 有测试 |
| **B3** 定量去重过度合并 | 键改为 `period_key` + `basis`，绝不用回退的 as_of；期间解析不出来的 Claim 自成一组；FY-vs-Q4、basis A/B、不可解析期间三种情况都有测试 |
| (a) 组的结算 | 重打标签会结算它**离开**的那个组；`duplicate` 短路不再跳过结算；新增 `settle_group()` 公开入口 |
| (b) 引文绑定复核 | 不再适用：新模式里没有引文绑定。等价检查落在 `verify_source_material` 的 `review_binds_document` / `envelope_names_the_document` / `quote_names_a_span` 三条 finding 上 |
| (c) PURPOSES | 撤回对 `cockpit_model.py` 的改动，改为 `claim_index_tagging` import 时 `register_purpose("claim_index")` |
| (d) 瞬时状态 | `busy` / `model_unavailable` 不再进 `self._failed` |
| (e) `_order` 泄漏 | 改名 `index_order`，**每一行都有**（未标注的排在最后），投影层剥掉 |
| nits | `--dry-run` 一个字节都不写（连规则标签也不写，`index_status = dry_run`）；sweep 返回 `truncated` |

---

## 这一片回答的问题

live 有 2,170 条 Claim。它们**每一条的出处都无可挑剔**，但堆在一起没法用：

- `metric_or_aspect` 是自由文本，"demand environment""Demand environment"
  "AI demand outlook""discretionary project demand" 是四个字符串、一件事；
- 没有时效。2023 年那条 DXC 人事新闻和今天的电话会原话在读取路径上一样新；
- 没有重要性。一手 filing 的数字和一篇网页新闻并排；
- 没有去重。同一个季度的收入在 ACN 的 Initial Screen 里作为三条 Claim 并排引用。

同时，12 条已核验数字停在 `coverage_mission_document_figures`（现存活 5 条，
7 条已撤回），因为 `CandidateStagingStore.stage` 明确拒绝 cited-original 的定量候选。
这是 PROJECT_STATUS 待办第 6 条。

## 一、Claim 索引：一层单独的、append-only 的投影权威

**不碰 Claim。** ClaimVersion 契约冻结、Ledger append-only，而"这条 Claim 是关于什么的"
"它讲的是哪一天""它的来源值多少""它是不是三份里的一份"全都是**关于** Claim 的判断，
不是 Claim 的属性。写到 Claim 上，就意味着每次判断变好都要改历史。

所以它们住在自己的权威里（`claim_index_schema.sql` + `claim_index_authority.py`），
按 `claim_version_ref` 编键，规矩照 `statement_snapshot.py`：append-only、`content_hash`、
三个 `dalton_authorized()` 触发器、版本链、写后读回校验、内容没变就是 `duplicate`。

**`status` 故意不存。** 它由裁决投影出来（`DaltonStore.project_claim_status`），
存一份副本就是给一个已经有答案的问题造第二个答案——正是 ClaimIndex 0.1 当年
停止接受调用方 bundle 时修掉的错误。

一条 `ClaimIndexEntryVersion` 说这些：

| 字段 | 由谁决定 | 说明 |
| --- | --- | --- |
| `aspect` | 规则 / 模型 | 封闭词表，见下 |
| `aspect_source` | — | `rule` 或 `model`，永远记着这条标签是怎么来的 |
| `as_of` + `as_of_basis` | 规则 | 日期，**以及这个日期是从哪读出来的** |
| `importance` + `importance_basis` | 规则 | 五档，附推导依据 |
| `dedupe_group_ref` + `dedupe_group_key` | 规则 | 分组键与其内容寻址的 ref |
| `is_canonical` + `revision_reason` | 权威 | 组内代表，改变时**追加新版本**而非编辑 |
| `tagger_ref` + `tagger_hash` | — | `rule:<name>` + 规则表哈希，或 `model:<work_order_ref>` + 任务与 prompt 哈希 |

### 封闭 aspect 词表（`claim_aspect_vocabulary.py`）

十节与蓝图 P12a `CompanyDossierVersion` 的分节**逐字一致**，这样 Wave 2 的档案
第 k 节就是"aspect = k 的 Claim"，不需要第二套映射：

`business_model`、`segments_and_mix`、`demand_drivers`、`supply_and_cost`、
`competitive_position`、`management_and_capital_allocation`、`guidance_style`、
`kpi_dictionary`、`catalyst_calendar`、`history_of_price_drivers`
——加上 `industry`（主体是行业、公司档案没有这一节）与 `other`。

每个词带一行定义，**这行定义就是给模型看的原文**，所以任务哈希 `TASK_HASH` 是对这张
表算的：改一个词就是改了问题，标签会因此重新版本化。

`other` 不是"拿不准"的垃圾桶：拿不准的整批 refused、保持未标注、下次再看；
`other` 是一个断言。

### 四件事里只有一件是判断

- **`importance` 不是判断**，是文档种类，Core 已经精确知道。链路是
  Claim → EvidenceRelation → Evidence.`source_lineage` → 更正集 → `document_ref`
  → `coverage_mission_discovered_documents` → 发现记录的 `spec_ref`。
  映射直接复用已冻结的 `document_figure_grade.GRADE_BY_SPEC`
  （10-K / 10-Q / 新闻稿 = 公司发布 → `filing`；电话会 → `management_statement`），
  再补 `sell-side-reports` → `sell_side` 与三条 web 检索 spec → `news`。
  **spec 是唯一能把同一个连接器取回的电话会与券商报告区分开的东西**：两者的
  `source_type` 都是 `authenticated_transcript`。链路走不到 spec 时退回
  `source_type`，并在 `importance_basis` 里如实写明退了。
  web 文档两种 ref 形状（`public-web-url:sha256:<url>` 与
  `public-web-document:url-sha256:<url>:body-sha256:<body>`）按 url 摘要对上。
- **`as_of` 不是判断**，是"这句话说的是哪一天"，按顺序读：结构化 period 的 `end`
  → 能解析的 period 标签 → 文档 `published_at` → evidence `retrieved_at`。
  **每个答案都带 `as_of_basis`**，因为"这个季度截止于 09-08"和"我们 09-08 抓的页"
  不是同一件事，当成同一件事比较，就是三年前的人事新闻排到 DXC 档案顶上的原因。
  live 的 `published_at` 一条都没有（894 个 SourceEnvelope 全空），所以退到
  `retrieved_at` 的比例很高——如实标出来，不假装。
  财年标签（`FY2025`）解析到**日历年末**：ACN 的财年 8 月结束，这是近似，
  `as_of_basis = period_label` 已经说清它是标签解析。P12a 拿到公司自己的报表日历
  可以收紧；在这里逐公司猜一套财年，等于往仓库里放第二本不可回指的日历。
- **`dedupe_group_ref` 不是判断**：数字按 subject × metric × period × unit，
  文字按同一主体的同一句话（折叠大小写与空白，仅此而已）。
  **没有 embedding**——相似检索是仓库的冻结项，留在精确匹配里才解释得清。
  两句不同措辞的同义陈述仍是两条，这是保守方向的错。
- **只有定性 Claim 的 `aspect` 是判断**，只有它去问模型。

### 模型这一跳：整批核验，越界整批拒绝

一次调用带一批（默认 40 条），**表格而非 JSON**，照 `company_model_spec` 的教训
（同样内容用对象编码要多几倍字节，而路由是按 prompt 字节预留预算的）。
用途 `claim_index`（`cockpit_model.PURPOSES` 加了这一个词）。

回复必须是"每行 `<行号><TAB><aspect>`"：**只能给它看过的行号、每行一次、只能用词表里的词**。
多一行、少一行、答两次、用了词表外的词、夹一句散文——**整批 refused，永不修补**。
一个凭空造出行号的回复，说明它不是在读那张表，那么它碰巧答对的行也是同一个回复的产物。

## 二、读取侧

`query_company_research` 新增 `index_aspect` / `as_of_from` / `as_of_to` /
`importance` / `canonical_only`（**默认 true**）。

- `aspect` 参数含义**不变**（仍过滤 Ledger 的自由文本 `metric_or_aspect`），
  封闭词表走新参数 `index_aspect`。合并成一个参数会悄悄改变每个现有调用方的语义。
- **老 Core 逐字节退化**：没有索引表时，返回的行连一个新字段都不带，和 P12b 之前完全一样。
  有索引表时永远带（未标注的 Claim 带 `None`），这样"还没标"和"这个 Core 根本没索引"
  能分开。
- **需要索引才能回答的过滤器**（aspect / 日期 / importance）在没有索引的 Core 上
  **明确报错**，而不是安静地返回空——"这里没有索引"和"没有匹配"是两个答案。
- `canonical_only` 只会去掉索引**正面标记为重复**的行；未标注的 Claim 永不因它消失。
- 另导出 `annotate_with_index(connection, rows, ...)`：驾驶舱的问答上下文
  （`cockpit_plane._claims`）是直接读 `claim_versions` 的，不走这个投影，
  集成时一行就能接上（见第五节）。它给**每一行**都带上 `index_order` 排序键
  （未标注的 Claim 排在所有已标注之后），因为只在部分行上出现的排序键排不了序；
  `query_company_research` 把它剥掉，投影的形状不变。

## 三、ADR-0007：已核验的数字进 Ledger

**ADR-0003 B 的拒绝原样不动。** 第一版把拒绝**收窄在 `transcript_core_authority` 里面**，
这正是 ADR-0007 §Decision 明确不许做的事。ADR-0003 的判断是关于逐字稿的，而且对逐字稿是对的；
错的是它当时说不出来的那句话——**"原文被引用"不是数字不能从它来的理由**。

所以两条 cited-original 模式（`transcript_core_authority`、`public_web_core_authority`）
**逐字恢复原状**，只收定性候选；已核验数字走一条**新的 provenance mode
`mission_figure_authority`**。

**它的 material 就是 figure 行本身**，周围的一切从 Core 重新推导：

```
figure → mission document review → discovered document → 找到它的那次 discovery
       → 枚举出该文档的 connector SourceEnvelope → 原始 ArtifactVersion
```

十五条 finding，全部在 staging 时重算、不接受调用方断言：figure 行哈希由自己的列重算、
未撤回、grade 是 filed、数字与 as-reported 标签在它引用的原文里、`citation_hash` 相符、
`quote_id` 命名一个 span、review 指向这份文档、discovered document 指向这份文档、
SourceEnvelope 是精确 Core 权威且枚举了这份文档、artifact ref/hash 精确、
`raw_response_hash` 等于 artifact 字节哈希、lineage 精确。

**整条链里没有任何 citation binding**——这正是 SEC filing 的 figure 能动起来的原因：
一份 filing 没有更正集，也永远不会有。**第一版报告里的阻塞点就此消失。**

**只有 company-filed 的 figure。** `earnings-call-transcript` 的 figure 保持定性
（ADR-0007 §Decision 4）。这条拒绝写在三处——staging store（合同）、promoter（给调用方读的句子）、
sweep（列进 `skipped` 并写明理由，绝不静默跳过）——因为只写在一处的规则是会被绕开的规则。

figure 行站在 NumericVerificationSpec 的位置上：`numeric_spec_ref` = figure_id，
`numeric_spec_hash` = figure 的 content_hash。**CandidateClaim 契约一个字段都没改，
审阅与裁决路径一点没改**，figure ref 从正式 ClaimVersion 经 `candidate_origin_ref`
（逐跳带哈希）一路可回指。

**默认仍是老规则。** `figure_admission_policy` 默认 `reject_cited_quantitative`。
改变"Ledger 认什么是数字"是治理决定，应当是有人按下去的。两条路径都有测试。

**促进器不做去重。** 三份文档报同一个季度的收入就是三条候选；索引把它们放进一个组、
留一个 canonical。拒绝第二第三条，等于扔掉"两份文档互相印证"这个信息。

`candidate_figures`（staging schema 新表，immutable）把重核过的 figure 行存在候选旁边：
staging 没有 Core 句柄，否则驾驶舱会显示一条它打不开数值权威的 Claim。
`HumanReviewAuthority.staged_figure(ref)` 是单独一个读，**不是** `candidate_authority_bundle`
上的新字段——那个 bundle 被四个调用方原样 splat 进 `commit_policy_candidate`，
加一个键就是给四个函数加一个关键字参数（这是全量测试抓到的）。

**两个真崩溃**（review 指出，已修并各有测试）：百分比没有 scale 词、而合同没有空 scale，
直接读 `held["scale"]` 会在"研究里大多数数字"上崩；数值字段现在统一从
`figure_candidate_numerics()` 一处映射出来。`ResearchVerificationError` 是基类，
从 sweep 的 except 元组里漏出去，一行坏数据会中止整轮。

## 四、验收结果（原文）

```
Ran 2195 tests in 203.729s
OK (skipped=1)
```

合并 main `888a814` 后的基线是 2,064，本片新增 131 项。命令：
`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`
（注意 `PYTHONPATH` 必须是**绝对路径**：子进程 cwd 是 state dir，相对的 `src`
会解析不到，于是子进程用回 venv 里指向主 checkout 的可编辑安装。）

### 冒烟：今日 live Core 只读副本（`/tmp` 拷贝，规则跑，不调模型）

```
tagged 2170 in 2.24s
rule-settled 22 / model 2148
importance   {'filing': 22, 'management_statement': 571, 'sell_side': 217, 'news': 1360}
as_of_basis  {'period_end': 23, 'period_label': 685, 'evidence_retrieved_at': 1462}
dedupe groups >1: 4   collapsed: 5
   3x quant|company:sec-cik:0001467373|quarterly_revenue_yoy_growth|2025-09-01..2025-11-30|official-filing
   2x qual|company:sec-cik:0001352010|eff31965...
   2x qual|company:sec-cik:0001058290|1287e75e...
   2x qual|company:sec-cik:0001058290|1f774f3d...
ungrouped (unparseable period, quantitative): 0
batch 40 claims / 14830 bytes; 54 batches
wrote 22 entries in 0.01s  {'entries': 22, 'versions': 22}
canonical: {True: 20, False: 2}

--- ADR-0007 sweep over live figures ---
  company-filed-document metric:revenue           pass ()
  company-filed-document metric:net-income        pass ()
  company-filed-document metric:net-income        pass ()
  company-filed-document metric:net-income        pass ()
  company-filed-document metric:adjusted-eps      pass ()
promoted: 5 fresh    skipped: []    truncated: 0
  claim: quantitative 69.7 currency billion
         "$69.7B in revenues for Fiscal 2025 was USD 69.7 billion (currency),
          as published by the company in this document."
  numeric_spec_ref = mission-document-figure:a2d1c43eb64d9ae76d25914d06592b20
staging: {'candidate_numeric_specs': 0, 'candidate_figures': 5,
          'candidate_claim_versions': 5}

--- and one of them all the way through ---
COMMIT: fresh claim-version:c0a907f54b12e6e31cd6cd95ab922ad1e90c9820db2ee3e7f18bb4ddadc5c43d
```

四件事值得单独说：

1. **ADR-0007 在 live 数据上端到端跑通了。** 五条现存 figure 全部重核通过、全部促进为
   定量候选；其中一条经 `HumanReviewAuthority` 人工裁决进入 Ledger，成为一条
   quantitative ClaimVersion（ACN 2025 财年收入 69.7 亿美元口径的那条）。
   第一版报告里"live 五条一条也促进不了"的阻塞点，是那一版把路径挂在
   transcript 引文绑定上造成的，换成 ADR-0007 定义的 `mission_figure_authority` 之后消失。
   **这仍然没有绕过任何签名**：`figure_admission_policy` 默认关闭，
   `research-auto-commit:mission-verified-figure:v1` 仍待 owner 发布并签名。
2. **ACN 那三条并排引用仍被抓到**，而且现在的组键是**声明的期间**
   `2025-09-01..2025-11-30` 加 `basis`，不是解析出来的日期。另有 3 组定性重复
   （EPAM 一组、CTSH 两组）。
3. **去重不再过度合并**（review B3）。旧键用解析后的 as_of（`FY2025` 与 `Q4 2025`
   都落在 2025-12-31，是两个数）、不含 `basis`（GAAP 与 non-GAAP 同季营业利润率会被合并）、
   期间解析不出来时退到 `evidence_retrieved_at`（live 2,170 条里有 1,462 条），于是
   "同一家公司、同一个指标、同一天抓的"会变成一组——而 `canonical_only` 默认为真，
   那些互不相同的事实会直接从读取结果里消失。现在：键用 `period_key` + `basis`，
   期间解析不出来的 Claim **自成一组、永不合并**，as_of 只用于排序。
   live 的 22 条定量期间全部可解析，所以 `ungrouped` 为 0。
4. **period 解析的唯一真错误在第一轮冒烟里现形并修掉了**：live 的定量 period 形状是
   `2026-03-01..2026-05-31`，第一版没有区间规则，退到"取字符串里第一个年份"，
   于是 ACN 三个不同季度全被合并成 "2026"。现在区间是一条规则，裸年份规则整串锚定。

## 五、集成要接的线

### 1. lane registry 一行

Wave 0 的 `LaneSpec` 需要这些值：

| 字段 | 值 |
| --- | --- |
| `operation` | `claim_index_tick`（建议） |
| `core_only` | `True`（只读 Core 快照 + 写自己的表，不碰 Ledger） |
| `param_fields` | `frozenset({"company_ref"})` |
| `build_coordinator` | `MissionClaimIndexLaneCoordinator(store=…, launcher=…, mission=…)` |
| `launcher_factory` | `ClaimIndexLauncher(state_dir=…, model_config_path=…, scheduler_db=…)` |
| `argv_fragment` | `--claim-index-model-config <path>`（照 model spec lane 的样子） |
| `driver_key` | `claim_index` |

coordinator 的构造签名是 `store` / `launcher` / `mission`（**不是** `missions`
权威，它需要的是 `claim_index_snapshot`）。launcher 是 `LaneChildLauncher` 子类，
`start(company_ref=…, claim_version_refs=[…])`。

### 2. 词表：需要一个新词 `claim_index`

`AUTOMATION_WRITE_SCOPES` 里没有它，Wave 0 加的是 `dossier` / `debate_map` 等，
也没有它。**建议加**：索引条目严格弱于它指向的 Claim（改不了值、期间、状态，
也不写 Ledger），但它是一次自动化写入，按 ADR-0004 应该有自己的词。

在那之前，`claim_index_cli.granted_scope` 已经**先查 `claim_index`、再退回 `claim`**，
并在 summary 的 `write_scope` 里说明是哪个词放行的。集成时只要把词加进词表并在新版
mission 里授予，**代码不用改**；`FALLBACK_WRITE_SCOPES` 那一行连同它上面的 TODO
可以删掉。live mission v13 已有 `claim`，所以今天就能跑。

### 3. 模型用途：已按 Wave 0 的形状登记，无需接线

`claim_index_tagging` 在 import 时调用 `cockpit_model.register_purpose("claim_index")`。
`cockpit_model.py` 本身**未改**（合并 main 时取了 Wave 0 那一侧）。
仍建议 `scripts/raise_day_budget_cap.MODEL_CONFIG_NAMES` 加一个 `claim-index`
模型配置名（本片没碰那个文件）。

### 4. 打包

`claim_index_schema.sql` 依赖 Wave 0 把 `pyproject.toml` 的 package-data 改成
`*_schema.sql` 通配（本片没碰那个文件）。在那之前 `PYTHONPATH=src` 能找到它，
全量测试也过；部署前要确认它进了包。

### 5. 驾驶舱

- 问答上下文 `cockpit_plane._claims` 直接读 `claim_versions`，**没有走这个投影**。
  接线：把它取到的行交给 `company_research_view.annotate_with_index(core, rows,
  ref_key="ref")`，默认就得到 canonical-only；再按 `canonical_order_key` 排序，
  一手 filing 排在网页新闻前面。这会直接改善 P10c 的"同季重复 Claim 并列引用"。
- `writer_server.OPERATION_FIELDS["company_research_query"]` 仍是老五个字段，
  新参数进程外调不到；要在 registry 迁移时加上
  `index_aspect` / `as_of_from` / `as_of_to` / `importance` / `canonical_only`。
- 审阅页若要显示 figure 支撑的候选，读 `HumanReviewAuthority.staged_figure(claim_id)`。

### 6. ADR-0007 的策略签名

本片**没有伪造任何哈希**。规则藏在 `figure_admission_policy` 后面、默认是老行为，
两条路径都有测试。集成时要做的是：

1. **owner 发布并签署一版治理 policy**，其
   `research_candidate_auto_commit.rules` 列出
   `research-auto-commit:mission-verified-figure:v1`
   （常量在 `research_verification.MISSION_VERIFIED_FIGURE_RULE_REF`，
   ADR-0007 Decision 末段点名的就是它，做法与 ADR-0005 的
   `research-auto-commit:mission-document-qualitative:v1` 一致）；
2. 把促进器的调用方（CLI 或将来的 lane）显式传 `figure_admission_policy=
   "verified_figure"`——`promote_figure` 已经是这样传的，`stage()` 的默认值不动；
3. 注意本片实现的是**人工审阅**那条路（`commit_reviewed_candidate`，已在 live 副本上
   跑通）。走 policy 自动准入（`commit_policy_candidate` / `research_auto_commit`）
   需要上面那条 rule ref 生效之后再接一次，本片没有实现自动准入分支。

**未接线**：`claim_index_cli.py --promote-figures --staging-db <path>` 是手跑入口，
没有 lane、没有 launcher、没有写进 driver。

## 六、没做的、以及发现的缺口

- **`coverage_mission_statement_lines`（live 11,831 行）只做了核验、没做准入。**
  ADR-0007 §Decision 1 把报表行和 document figure 并列为可准入的两种数值权威；
  本片只实现了 document figure 那一半。`DocumentFigureResolver.verify_statement_line`
  已经按同样的方式重核（行有值、filing 有 accession、filing 记录了原始产物 refs，
  产出 numeric bundle），但 `mission_figure_authority` 的 material 走的是
  **mission 文档链**（review → discovered document → discovery → SourceEnvelope），
  而报表行没有文档链——它的链路是 SEC financials 的 dispatch / ingest 与那条
  连接器权威。要接上，需要给报表行建一份等价的 material（`ingest_id` 当
  envelope、`source_record_refs_json` 里的 `raw-sink:<sha>` 当 artifact，
  并校验 `governance_ref`/`governance_hash` 已批准，即 ADR-0007 §Decision 3 后半句）。
  估计一个人日以内，但它是 ADR 的另一半，值得单独一片。
- **自动准入分支没写。** 见五·6 第 3 条。
- **`build_claim_index`（ClaimIndex 0.1 wire）没有改。** 它是 agenda ContextPack
  里按 ref+hash 校验的冻结对象，往它的 closed entry 上加字段会改掉每一个已存在
  对象的哈希。索引通过 `company_research_view` / `annotate_with_index` 读，不经过它。
- **`aspect` 的模型标注没有在 live 上跑过**（要求如此：不对 live 调模型）。
  规则那半在 live 副本上跑过，见第四节。
- **财年日历近似**：`FY2025` → `2025-12-31`。ACN 的财年 8 月末结束。见第一节。
- **`other` 的定量比例**：live 22 条定量全部落在 `segments_and_mix`
  （都是 `quarterly_revenue_yoy_growth`）。规则表还没有被真实的多样性检验过；
  `quantitative_aspect` 返回 `other` 就是"该往表里加一行了"的信号。

## 七、开放问题（需要 owner 或主 agent 定）

1. **`claim_index` 要不要成为 `may_write` 的新词？** 建议要（见五·2）。
   不要的话，`claim` 授权已经够用，把 `claim_index_cli` 里的 TODO 删掉即可。
2. **ADR-0007 的策略开关谁来打开、什么时候？** 本片默认关闭，但链路已经通：
   live 副本上五条 figure 全部促进、其中一条经人工裁决进了 Ledger。
   打开需要 owner 签一版列出 `research-auto-commit:mission-verified-figure:v1`
   的 policy（五·6）。
3. **定性去重要不要更进一步？** 现在只合并"同一主体的同一句话"。live 只找到 3 组。
   更强的合并需要相似度模型，那是冻结项。
4. **补完 2,148 条存量的一次性开销**（54 批，约个位数美元）走 mission 日预算，
   会在某一天占掉相当一块。要不要限速（比如每 tick 一批）？
   现在 lane 就是每 tick 一批一家公司，天然限速；一次性补完需要手跑。
