# P12b：给 Claim 建索引，一个 Claim 字节都不改；已核验的数字有了进 Ledger 的门

*2026-09-09* · 分支 `wave1b-claim-index`，基线 main `08c66d0`
· 依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 B 线、
[能力差距分析 v1.0](analyst-onboarding-gap-analysis-and-roadmap-v1.0-2026-09-09.md) 5.2 P12b、
ADR-0003 / ADR-0004 / ADR-0005、ADR-0007（owner 已接受）

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
  集成时一行就能接上（见第五节）。

## 三、ADR-0007：已核验的数字进 Ledger

**没有重开 ADR-0003 关上的那扇门。** Ledger 的数字入口是 JSON pointer 复算，
文档正文没有可复算的 payload，而"写个正文抽数器"正是 ADR-0003 选项 C 拒绝的东西。

这里用的是另一件事实：**figure 行本身已经是一个确定性数值权威**。它的数字与
as-reported 标签在写入前就已经在它引用的那段原文里核对过，而原文、manifest 哈希、
行自身的 content_hash 全都存着，谁都可以重跑。`claim_index_figures.py` 就是重跑它：

1. 按 figure_id **从 Core 读回**这一行（不看调用方给的那份）；
2. 用它自己的列重算 `content_hash`；
3. 查撤回；
4. 用写入时同一个确定性校验器 `verify_numeric_candidate` 重跑数字与标签；
5. **最后**才把调用方那份逐字节比对。

产出是 staging 本来就要的那个 `numeric` VerificationBundle。于是 figure 行站在
NumericVerificationSpec 的位置上：`numeric_spec_ref` = figure_id，
`numeric_spec_hash` = figure 的 content_hash。**CandidateClaim 契约一个字段都没改，
审阅与裁决路径一点没改**，figure ref 因此从正式 ClaimVersion 经
`candidate_origin_ref`（逐跳带哈希）一路可回指。

**默认仍是老规则。** `figure_admission_policy` 默认 `reject_cited_quantitative`，
只有调用方明确要 `verified_figure` 才放行。改变"Ledger 认什么是数字"是治理决定，
应当是有人按下去的，不应当是一次合并带进来的。两条路径都有测试。

**促进器不做去重。** 三份文档报同一个季度的收入就是三条候选；索引把它们放进一个组、
留一个 canonical。拒绝第二第三条，等于扔掉"两份文档互相印证"这个信息。

`candidate_figures`（staging schema 新表，immutable）把重核过的 figure 行存在候选旁边：
staging 没有 Core 句柄，否则驾驶舱会显示一条它打不开数值权威的 Claim。
`HumanReviewAuthority.staged_figure(ref)` 是单独一个读，**不是** `candidate_authority_bundle`
上的新字段——那个 bundle 被四个调用方原样 splat 进 `commit_policy_candidate`，
加一个键就是给四个函数加一个关键字参数（这是全量测试抓到的）。

## 四、验收结果（原文）

```
Ran 2130 tests in 269.258s
OK (skipped=1)
```

基线 2,034，本片新增 96 项。命令：
`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`
（注意 `PYTHONPATH` 必须是**绝对路径**：子进程 cwd 是 state dir，相对的 `src`
会解析不到，于是子进程用回 venv 里指向主 checkout 的可编辑安装。）

### 冒烟：今日 live Core 只读副本（`/tmp` 拷贝，规则跑，不调模型）

```
mission: coverage-mission-version:us-it-services:13   scope: claim
pending: 2170 in 0.54s
rule-settled: 22, left for the model: 2148
importance: {'filing': 22, 'management_statement': 571, 'sell_side': 217, 'news': 1360}
as_of_basis: {'period_end': 23, 'period_label': 685, 'evidence_retrieved_at': 1462}
rule aspects: {'segments_and_mix': 22}
dedupe groups >1: 4   duplicates collapsed: 5
   3x quant|company:sec-cik:0001467373|quarterly_revenue_yoy_growth|2025-11-30|percent
   2x qual|company:sec-cik:0001352010|eff31965...
   2x qual|company:sec-cik:0001058290|1287e75e...
   2x qual|company:sec-cik:0001058290|1f774f3d...
first model batch: 40 claims, 14830 prompt bytes
batches needed for the backlog: 54
wrote 22 entries in 0.01s  {'entries': 22, 'versions': 22}
canonical: {True: 20, False: 2}
live figures: 5 (12 recorded, 7 retracted) — all 5 re-verify against their stored citation
figures with an admitted citation: 0 of 5
```

三件事值得单独说：

1. **ACN 那三条并排引用被抓到了**，`quarterly_revenue_yoy_growth`、
   2025-09-01..2025-11-30、percent，一个组、一条 canonical、另两条追加了
   `recanonicalised` 版本。另外还找出 3 组定性重复（EPAM 一组、CTSH 两组）。
2. **period 解析的唯一真错误在冒烟里现形并修掉了**：live 的定量 period 形状是
   `2026-03-01..2026-05-31`，第一版没有区间规则，退到"取字符串里第一个年份"，
   于是 ACN 三个不同季度全被合并成 "2026"。现在区间是一条规则，裸年份规则改成
   **整串锚定**（`revenue in 2025 and 2026` 因此不再产生日期）。
3. **14,830 字节的 prompt**：远低于 `company_model_cli` 记录的 43KB 被拒线，
   按它 24KB / $0.24 的量级推算，2,148 条定性 Claim 需要 54 批，一次性补完
   大约在个位数美元。`MAX_COST_USD = 0.60`（预留，不是价格）。

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

### 3. `cockpit_model.PURPOSES` 加了一个词 `claim_index`

Wave 0 正在把 PURPOSES 改成可登记的注册表；这里是一行冲突，取 Wave 0 的形状、
把 `claim_index` 登记上即可。同时建议 `scripts/raise_day_budget_cap.MODEL_CONFIG_NAMES`
加一个 `claim-index` 模型配置名（本片没碰那个文件）。

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

1. 在 ADR-0007 正文里写明 `FIGURE_ADMISSION_VERIFIED_FIGURE` 是被接受的那条规则；
2. 若治理 policy 的哈希覆盖候选准入规则，**重新签一次**并记录；
3. 把促进器的调用方（CLI 或将来的 lane）显式传 `figure_admission_policy=
   "verified_figure"`——`promote_figure` 已经是这样传的，`stage()` 的默认值不动。

**未接线**：`claim_index_cli.py --promote-figures --staging-db <path>` 是手跑入口，
没有 lane、没有 launcher、没有写进 driver。

## 六、没做的、以及发现的缺口

- **live 那 5 条 figure 今天一条都促进不了**，原因不是策略而是链路：它们的
  `document_ref` 全是 `sec:filing:…`，而 ADR-0007 的 staging 走的是 cited-original
  路径（`transcript_core_authority` / `public_web_core_authority`），它要一条
  claim-eligible 的引文绑定，而引文绑定来自 transcript 更正权威——SEC filing 文档
  根本没有更正集。**要么**给 SEC filing 正文建一条同样的引文权威，**要么**新增一个
  `sec_core_authority` provenance mode。这是把数字接进 Ledger 的下一块，
  已在 AlphaEngine / public-web 文档上端到端跑通并测过（`test_figure_admission.py`
  从 staging 一路到 `commit_reviewed_candidate`，落成一条 quantitative ClaimVersion）。
- **`coverage_mission_statement_lines`（live 11,831 行）只做了核验、没做准入**：
  `DocumentFigureResolver.verify_statement_line` 重算 filing 行的哈希、检查
  accession 与原始产物记录、产出 numeric bundle；但报表行没有正文引文，走不了
  cited-original 路径，它的链路是 SEC 连接器权威。与上一条是同一个设计问题
  （PROJECT_STATUS 待办第 6 条末尾自己也这么说）。
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
2. **ADR-0007 的策略开关谁来打开、什么时候？** 本片默认关闭。
   打开之前，第六节第一条（SEC filing 引文链路）不解决的话，live 上打开也没有数字能进。
3. **定性去重要不要更进一步？** 现在只合并"同一主体的同一句话"。live 只找到 3 组。
   更强的合并需要相似度模型，那是冻结项。
4. **补完 2,148 条存量的一次性开销**（54 批，约个位数美元）走 mission 日预算，
   会在某一天占掉相当一块。要不要限速（比如每 tick 一批）？
   现在 lane 就是每 tick 一批一家公司，天然限速；一次性补完需要手跑。
