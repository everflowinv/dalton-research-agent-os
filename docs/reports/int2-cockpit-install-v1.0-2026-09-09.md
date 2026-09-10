# INT2：把第二批线接到驾驶舱和安装脚本上 v1.0

日期：2026-09-09
分支：`int2-cockpit-install`（worktree `~/Projects/dalton-int2-cockpit-install-worktree`），基线 main `8110b23`（3,620 项），
中途合并 main `6da8f82`（C2 预算池 + tick 账本）
角色：并行开发计划第 5 节的集成位，第二批。九条线（P14a 日常跟踪、C1 催化日历、S1 投喂、S2 Guidepoint、
S3 大众源、S4 中国基本面、P14e 专项研究、P14-M 模型路由、Q2 周期反思）各自在报告里写了「集成时要接的线」，
然后一个字都没动驾驶舱和安装脚本——那两个文件不归任何一个 lane agent。这一片就是那些线。

---

## 0. 一句话

公司卡现在会说**今天发生了什么**（按种类，每条带它值多少）、**大脑怎么判断的**（五个决定词之一、一句理由、
真正落了什么、独立复核过没有）、**它事后怎么反思的**（我们当时以为 / 实际发生 / 可能漏了哪个争论）、
**下一个催化剂还有几天**（日期是 vendor 猜的就写「日期未确认」）、**我们多久看它一次**和**正在专项研究什么**；
新开两个整页读物——「每个来源能取到什么」（含模型链与目录一致性）与「每周回头看」；
安装脚本按「一条 lane 要么装全要么不装」的规矩种下七条线，S1 与 S3 由环境变量决定装不装。
全量 3,731 项，1 项失败且是合并进来的 main 上原有的（见第 8 节）。

---

## 1. 为谁接了什么

### P14a（日常跟踪、事件、判断、反思）

- **今日事件**：`research_events` 按公司分组，`kinds` 按 `EVENT_KINDS` 的顺序给出十二个种类的计数，
  `latest` 八条各带 `kind_label`、`tier_label` 与一句 `summary`。**层级是随事件走的**：一份 filing 和
  一条推特都是事件，不是一回事，而卡片上它们占的像素一样多。`summary` 按 kind 从 payload 的具名字段生成
  （异动是「9 月 8 日涨 4.0000%，收 104」，不是把 payload 打印出来）。
- **大脑的判断**：`event_judgements` 的 `decision` / `action` 各自有中文（`THESIS_WEAKENED` →「论点被削弱了」，
  `note` →「写一段短报告」），加上那一句 `because`、`effect` 的**实际状态**（`published` / `queued` 与它的
  `reason`）、以及 verifier 的结论。**`no_change` 也照样列出来**——周会开头那句「为什么没改主意」只有在
  账本里留着「不用改主意」这条决定时才答得上。
- **反思**：`thesis_reflections` 的四问（当时以为 / 实际发生 / 为什么 / 可能漏掉的争论）、两类跟进，
  以及一句固定的话：「跟进项只是候选：它没有改任何频率，也没有开任何任务」。有测试跑完之后去查
  `TrackingCadenceAuthority.latest(...)` 仍是 `None`，钉住这句话是真的。
  `market_view_vs_ours.available` 为 false 时卡片写「这个 Core 里还没有街上的看法可比」，不是留白——
  留白会被读成「我们和市场一致」。
- **频率**：policy 里十个来源**全部**列出来（基线 + 它自己的 `because`），大脑发过版本的那几行换成
  大脑的值和大脑的理由，并标上「大脑调过」。只列版本会让九个没人重新计时过的来源从页面上消失，
  而那九个频率照样在生效。`adjustable: false` 的（股价、SEC、日历）写「固定，大脑不能调」。
- **`ungranted` 与 `gated:same_family` 可见**：这两个词已经在 INT1 的 lane 面板词表里（「缺授权，一次也没跑」），
  本片新增的是 C2 的 tick 账本读数（第 4 节），它把「这条 lane 每 tick 都在报 unconfigured」变成一个可以数的事实。
- **`SourceCapabilityMap`（owner 点名要的那张表）**：见第 3 节。

### C1（催化剂日历）

`下一个催化剂 T−N 天` 直接来自 `CatalystCalendarAuthority._reader_view`（**复用 lane 自己的读者视图，
不在驾驶舱里第二次推 caveat**），卡片上是 `headline` + 事件种类 + 日期 + `date_caveat`。
两个来源日期不一致时把不一致的日期列出来。已经发生过的日历不显示。

### S1 / S2 / S3 / S4（来源）

四条线在驾驶舱里的落点是同一个：**来源面板**（第 3 节）与 lane 状态面板里的中文名（INT1 已建，
本片没有新 lane 要起名——`test_every_registered_lane_is_named_in_the_owner_s_words` 仍然绿）。
S4 没有 lane，它在来源面板上以 `in_inventory` / `undeclared` 的形态出现，安装脚本种它的六份记录（第 5 节）。

### P14e（专项研究）

公司卡新增「正在专项研究」：问题原文、`0/2 轮` 的预算、状态词，以及**结论或缺口，二者必居其一**——
一个还没跑完的任务留空，和一个查遍了什么都没找到的任务留空，长得一模一样。
读的是 `bounded_planner_loop_versions` 里 `admission.source == "inquiry"` 的那些，
**没有走 `research_task_view`**：那个读者要 `BoundedPlannerAuthority`，而它在构造时会跑 schema 脚本，
只读连接做不到，驾驶舱也不该为了读几行已经在那里的数据去要一个写句柄。

### P14-M（模型路由）

来源面板下半页：每一层（要动脑的 / 量大而便宜的 / 独立复核的）的**链**按顺序列出、链上哪个模型这台机器
没有档案、**上一次是链上第几个模型服务的**（第一个就是第一选择，不是第一个就说明第一选择当时没答上）、
以及自上次服务以来被跳过的链环和跳过的理由。`catalog_in_sync` 不一致时**两个方向的差集都列名字**——
「不一致」本身不告诉任何人该做什么，「哪一边少了什么」才告诉。

**一个现场发现**：`event_judgement` 与 `thesis_reflection` 两个 purpose **没有层级**
（P14a 用的是 `register_purpose` 而不是 `register_purpose_tier`）。今天无害（没有链的 policy 按老路走），
但哪天有人把它们的 policy 钉到某一层的链上，两者会被**拒绝**而不是回落到默认模型。
面板把它们列在「这些用途还没有指定层级」里，有测试逐名断言。这正是这块面板存在的理由。

### Q2（每周回头看）

新读物：最新一周的 `narrative.prose` 与 `narrative.table` **原样呈现**（两者都是对已在库的行做算术，
没有模型参与，所以可以逐字给人看）、`backlog_candidates`、`policy_suggestions`，
外加记录自己的 `authority_note`（「这条记录不改 policy，也不登记问题」）。

### C2（预算池与 tick 账本，合并 main `6da8f82` 之后加的）

- **「今天的钱花在哪四件事上」**：四池各自的上限 / 已花 / 还剩 / 借了多少 / 借自哪一池，
  加上「今天有 N 次请求因为池子空了被拒」。一个总数分不清「系统停了」和「系统里便宜的那一半停了」，四个池分得清。
  `caps_defaulted` 为真时页面写「这四个上限是默认分法，研究目标里没有自己的分法」——一个没人选过的分法不是 owner 的分法。
- **闲置率**：Q2 报告里那句「闲置率今天不可算，而且这是本片最有价值的一个发现」现在可算了。
  读 `TickLedger.idle_ratio()` 与 `lane_stalls()`，**不走 `summarise`**（那个还会返回整本账的每一条 tick
  和每一行 lane，而这是一个每次刷新都拉的页面）。卡住的 lane 用中文名列出来。
  没有账本时是 `None` 而不是 0——「0 次闲置」和「没有账本」是相反的答案。

---

## 2. 待审批页：两类新检查点

`thesis_revision_candidate` 与 `gate_reopen` 在有行的时候渲染，中文标题，**没有按钮**。
理由写在页面上那一行里：裁决它们的 ops 在 `w3-revision-loop` 分支上，本片不依赖它；
一个按下去没反应的按钮比一条写着「这一项要人裁决，而裁决入口还没有接上」的条目更坏。

读法是一张两列表（`CHECKPOINT_TABLES`）：候选表 + 决定表，决定表**存在时**才用来过滤已裁决的行。
这是本仓库里每一对检查点都在用的命名（`*_candidates` / `*_decisions`），所以 w3 落地时不需要改这里。
`thesis_revision_candidates` 今天就有写入者（P14a 的判断层），`gate_reopen_proposals` 还没有——
有一条测试手工建那张表，证明它出现的那天页面不用改。

候选旁边**直接展开它的反思**（`reflection_ref` → 正文），不是一个 ref：ADR-0007 的候选和「我们可能漏了什么」
并排看才有意义。P14a 的 `forecast_revision_proposals` 也一起列出来，标题是「预测行想改，但研究目标没有授权自动改」。

---

## 3. 来源面板（owner 2026-09-09 晚点名的那件事）

`/v1/cockpit/sources`。每条 connector 一行：

| 列 | 来自 | 页面上是什么 |
| --- | --- | --- |
| 能取什么 | `CAPABILITIES[slug].content_kinds` | 「卖方研报、电话会纪要、管理层会议纪要」 |
| 层级 | `evidence_tier` | 「公司报表原文」→「网上的议论」十档 |
| 状态 | mission 的 `source_plan` | 已接上 / 还没接上 / 只允许试读 / **研究目标里没提过它** |
| 配额 | `governed_daily_quotas()` | 每个 operation 一行「25/天」 |
| 频率 | tracking policy 的基线 | 「每天 2 次」「每 7 天一次」；没装 policy 时这一列是空的并说明原因 |

**通用源单独标出来并排在最后**：`gemini-web-search` 与 `web-fetch` 什么都能问，所以它们是「没有专门来源时才用」
而不是「先想到的」——这正是 `sources_for()` 交给研究决策的顺序，页面用同一个顺序。
`in_inventory: false` 的来源标「这条来源还没装进目录」。

投影用 `source_capability_map.build_map(mission=..., cadences=baseline_cadences(policy))`，
policy 优先读 `<state>/tracking-policy.json`（lane 真正跑的那一份），读不到才回落到仓库里打包的那份。
装好的那份优先是刻意的：页面显示仓库基线而机器跑着另一份，是一种安静的谎话。

---

## 4. 路由与只读纪律

- **`ModelRouter(path, read_only=True)`**，不是可写句柄。所有新读取器同样：`ResearchEventAuthority`、
  `EventJudgementAuthority`、`CatalystCalendarAuthority`、`BoundedPlannerAuthority`、
  `ResearchCycleReflectionAuthority` **一个都没有被构造**——它们都要 `DaltonStore` 并在 `__init__` 里跑 schema 脚本。
  能复用的纯函数照旧复用（`CatalystCalendarAuthority._reader_view`、`source_capability_map.build_map`、
  `tracking_cadence.load_policy`、`budget_pools.pool_status`、`tick_ledger.TickLedger(read_only=True)`）。
- **不再去 host 的家目录里找 OpenClaw 配置**。第一版写的是 `Path.home() / ".openclaw" / "openclaw.json"`，
  `tests/test_isolation.py` 当场抓住——那条规则是对的，控制进程不该自己去翻宿主的配置。
  改成 `CockpitConfig.openclaw_config_path`（可选，加法），由 `install.sh` 在网关确实装了的时候写进 `service.json`
  的 cockpit 块。没配置时目录那一块是 `None` 加一句说明，链和「上次谁服务的」照常显示。
- 新路由两个：`/v1/cockpit/sources`、`/v1/cockpit/reflection`。都是 GET，都在既有的 Tailscale + session 壳里。
  **没有新增任何 writer op**，也没有新增任何写路径。

---

## 5. install.sh：一条 lane 要么装全，要么不装

INT1 立的规矩，本片按它办。每一块**要么把这条 lane 需要的全部东西放下，要么一样都不放并说明为什么**。

| 线 | 种了什么 | 开关是什么 |
| --- | --- | --- |
| C1 | `yfinance-calendar-v1.json` | 就这一份记录（`--catalyst-calendar-governance`） |
| S2 | `discovery-plans/us-it-services-guidepoint-v1.json`（记录 P13ae 已在种） | 记录 + plan 两个文件都要 |
| S1 sales-notes | feed plan + 两份记录 + `feeds/market-digest-output` 链到工作区 | `DALTON_OPENCLAW_WORKSPACE`（默认 `$HOME/.openclaw/workspace`）下 digest 目录存在 |
| S1 company-wiki | feed plan + 两份记录 + `feeds/company-wiki` 链到工作区 | 工作区根上要有 `wiki-index.sqlite`（见下） |
| S3 | 七份记录 + `phase9/` 映射文件 + `host-tools/` 三个工具链接 | 三个工具环境变量都可执行 |
| S4 | 六份记录 | **没有 lane**，所以种了也开不了任何东西 |
| P14a | `tracking-policy.json` | 就这一份文件 |
| P14e | `phase8/p14e-adhoc-probe-templates-v1.json`（**发布清单，给人读的**） | 不种 lane 的开关文件 |
| Q2 | 无 | lane 的开关是「state 里有 core.sqlite」，本来就成立 |

**三处刻意不装**，各自的理由写在脚本里：

1. **事件判断 lane 的两份模型配置**——要指向不同 family 的 routing policy，而「哪两个」是 owner 的决定；
   一个模型自己核验自己不是核验。
2. **`research-task-lane.json`**——它是 P14e 的开关，在任何模板被发布之前打开它，得到的是一条每 tick 答
   `no_executable_adhoc_template_published` 的 lane。
3. **Guidepoint 的收窄记录**——放在 `$STATE/governance-decisions/` 而不是 `connector-governance/`。
   没有任何东西加载它，它是 owner 读了之后决定要不要撤回一个批准的材料；一份永远 `proposed` 的记录
   躺在运行时目录里，就是一个关于「无」的批准动作。

**S1 公司维基那半条还差 owner 一行。** 索引在 `wiki/vectors.db`，而 `documents.filepath` 是相对工作区根的，
所以语料根必须**就是**工作区（链到子目录会让每条路径都被判成逃出根而被拒），
而 lane 写死的索引路径是 `<state>/feeds/company-wiki/wiki-index.sqlite`，也就是工作区根上的
`wiki-index.sqlite`。**安装脚本不往 owner 的 OpenClaw 工作区里写任何东西**，所以这一个软链由 owner 跑，
脚本在缺它时打印原因并且不装这条 lane。写在 owner 步骤文档 4.2。

---

## 6. 全量验收（原文）

`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`：

```
Ran 3731 tests in 499.811s

FAILED (failures=1, skipped=1)
```

唯一那条失败是**合并进来的 main 上原有的**，不是本片引入的（第 8 节有复现）：

```
FAIL: test_an_exhausted_pool_is_a_skip_with_the_name_c2_will_generalise
(tests.test_mission_research_task_lane.LaneTests)
AssertionError: 'launched' != 'skipped:pool_exhausted'
```

本片自己的两个文件：

| 文件 | 项数 |
| --- | --- |
| `tests/test_cockpit_int2.py`（新） | 42 |
| `tests/test_service.py::InstallerSeedTests`（改写） | 10（原 3） |

`tests/test_cockpit_int2.py` 覆盖的：六块面板**逐个**在有表和无表两种 Core 上（无表一律 `None`，
页面还是那张页面）；事件按种类计数且每条带层级；异动的一句话是「涨 4.0000%」而不是 payload；
一家公司的事件不出现在另一家卡上；决定词与动作词各有中文；`effect` 的 `queued` 与 `published` 可区分；
`no_change` 照样入账并显示；反思四问齐全、跟进项显示为候选且跑完之后 cadence 真的没动、
没有街上看法时如实说；催化剂 T−N 与「日期未确认」、已确认时 caveat 为空、过去的日历不显示；
频率在没有 policy 时整块消失、有 policy 时十个来源全在、大脑发过版本的那行换成大脑的值和理由、
没人重新计时过的公司仍显示基线；待审批的两类新检查点（有表才出现、中文标题、反思并排、没有按钮、
有决定表时已裁决的行消失）与 forecast 提案；来源面板的能取什么 / 层级 / 状态 / 配额 / 频率、
通用源标记且排最后、`undeclared` 不被读成「被拒」；模型面板在没有路由库时说明原因、链按顺序、
缺档案的链环被点名、没有层级的 purpose 被点名、有无网关配置时目录块的两种形态且不一致时两个方向都列名字；
每周回头看在无表时说明原因、有表时散文与表逐字呈现；四个池的上限与余额、未迁移的日账本不出面板；
tick 账本的闲置比例与卡住的 lane（用中文名）、没有账本时是 `None`；专项研究的问题 / 预算 / 缺口。

`tests/test_service.py::InstallerSeedTests` 的写法换了个方向：**不再只读脚本的散文，而是拿 lane 自己的
`argv_fragment` 去验**——照某一块种下的文件建一个临时 state，那条 lane 必须被打开；抽掉其中任意一个文件，
那条 lane 必须消失。另有：七份大众源记录是 `proposed` 所以种下去也开不了（改一份为 `approved` 才开）；
S4 六份种下去一条 lane 都不开；三处刻意不装；两个环境变量门与三句说明；收窄记录不落在运行时目录；
模板清单是发布材料（脚本里不出现 `publish_probe_template`）；`bash -n` 与 `zsh -n` 都过。

**只读冒烟**（`cp /private/tmp/dalton-ro/core.sqlite /tmp/int2-smoke/`，没有碰 live）：

```
overview in 0.37s
  ACN   events=None judgements=None reflections=None catalyst=False tasks=False cadence=10
  CTSH  events=None judgements=None reflections=None catalyst=False tasks=False cadence=10
  EPAM  events=None judgements=None reflections=None catalyst=False tasks=False cadence=10
  IBM   events=None judgements=None reflections=None catalyst=False tasks=False cadence=10
  DXC   events=None judgements=None reflections=None catalyst=False tasks=False cadence=10
pools: None
ticks: None
sources: 17 rows, policy=tracking-policy:p14a:v1, generic=['gemini-web-search', 'web-fetch']
routing: False 这台机器上还没有模型路由库，所以没有可读的路由
reflection: False 这个 Core 还没有写过每周回头看
approvals: 0 []
```

live Core 上这些表一张都没有（九条线都还没部署），页面 0.37 秒出来，每一块都退化成 `None`，
只有频率一列有内容——那十行是 policy 的基线，它们对每家公司都成立，不因为没人重新计时过就消失。

---

## 7. 还欠一行代码（不是 owner 的动作）

**S3 的三个工具路径到不了 writer。** `mission_crowd_source_lane.argv_fragment` 只输出
`--crowd-source-map` 与 `--crowd-source-governance-dir`，不输出
`--crowd-source-xueqiu-tool` / `--crowd-source-xueqiu-fallback-tool` / `--crowd-source-xreach-tool`，
而 `build_launcher` 只从 argv 读它们。那个模块是 S3 的，本片只读；`macos_launchagent.py` 也不在本片的所有权里，
而且往它里面写三个 lane 专属的 flag 恰好是 Wave 0 建 registry 要终结的那种写法。

安装脚本已经把三个工具按固定名字放好（`$STATE/host-tools/{agent-reach,xueqiu-hot-rank,xreach}`），
所以缺的那一行是在 `argv_fragment` 里从这三个路径拼出三个 flag。**在那之前**：七份记录仍是 `proposed`，
lane 因此是关的，所以今天不会出现「装上了但每 tick 都拒」的状态；但 owner 一旦批准记录，就会出现。
owner 步骤文档第 5 节把这件事写在批准动作旁边。

---

## 8. 合并进来的 main 上原有的一处失败

```
$ git archive main | tar -x -C /tmp/int2mainchk
$ cd /tmp/int2mainchk && PYTHONPATH=$PWD/src python -m unittest tests.test_mission_research_task_lane
Ran 12 tests in 0.619s
FAILED (failures=1)
```

同一条。也就是说它随 main `6da8f82`（C2 预算池）进来，不是本片引入的，且不在本片的所有权范围内
（`tests/test_mission_research_task_lane.py` 是 P14e 的文件）。

症状：那条测试连admit 十条 inquiry 想把 `adhoc` 池花光，然后期望 lane 报 `skipped:pool_exhausted`，
实际报 `launched`——池没有被花光。`research_task.POOL_SHARE`（0.25）与
`budget_pools.DEFAULT_SHARES["adhoc"]`（0.25）是一致的，`pool_caps({"max_daily_cost_usd": 5.0})`
也给出 `adhoc: 1,250,000` 微元，所以分法没有分叉；问题应该在准入循环那一侧
（`entry["admissible"]` 提前变假、或 `day_reserved_micros` 的口径被 C2 改动过）。
**建议交回 C2 或 P14e 的作者定位**，本片没有改它。

---

## 9. 没做的 / 留给下一片的

- **裁决 `thesis_revision_candidate` 与 `gate_reopen` 的 ops**（`w3-revision-loop`）。本片渲染它们，
  不假设它们的裁决路径；`CHECKPOINT_TABLES` 已经按本仓库的命名约定留好了过滤位。
- **第 7 节那一行**（S3 的工具 flag）。
- **`event_judgement` / `thesis_reflection` 两个 purpose 没有层级**（第 1 节 P14-M）。面板会说，
  但真正的修法是在 `event_judgement.py` 里把 `register_purpose` 换成 `register_purpose_tier`，
  那是 P14a 的文件。
- **`research_task_view` 与 `pool_state` 没有被复用**（前者要写句柄，后者的口径已经由 `pool_status` 覆盖）。
- **模型表、结论浏览、来源面板、每周回头看四个读物都开在同一个 reader 浮层里**，
  ADR-0006 的五个视图仍然是五个，没有变成九个。
- 没有跑过任何真实模型调用；没有碰 live 状态目录、没有部署、没有发 mission 版本、没有批准任何记录。

---

## 10. 配套文档

[部署之后你要做的事，按顺序 v1.0](owner-steps-after-deploy-v1.0-2026-09-09.md)：
20 条数据源批准（记录路径 → `dalton-connector-governance approve`）、一版 mission（九个 `may_write` 词、
一个人类检查点、八个来源翻成 connected）、figure 准入策略重签、verifier phase pin 重指、
mandate 放宽、网关重启、四条装线动作，以及一个要 owner 回答的问题（AlphaEngine 的 24 小时上限）。
每行一个动作，带命令或文件。
