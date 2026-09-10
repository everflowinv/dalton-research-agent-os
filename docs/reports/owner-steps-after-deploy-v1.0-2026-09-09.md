# 部署之后你要做的事，按顺序 v1.0

日期：2026-09-09 · 生成自 INT2（`int2-cockpit-install`）· 配套报告：[INT2](int2-cockpit-install-v1.0-2026-09-09.md)

这份清单是「跑完 `deploy/macos/install.sh` 之后，还差哪些**只有你能做**的动作」。
安装脚本从不批准任何东西、从不签任何策略、从不发任何版本——它只是把要批的东西放到你面前。
每一行一个动作，带上命令或文件。没做的那些，驾驶舱会如实说出来（「等你批准数据源」「缺授权，一次也没跑」），
不会装作在跑。

约定：

```
STATE="$HOME/Library/Application Support/Dalton/state/dalton-core"
GOV="$STATE/connector-governance"
VENV="$HOME/Library/Application Support/Dalton/runtime/venv"
OWNER=human:lumos      # 换成你自己的主体
```

---

## 0. 先做的两件事（不做后面很多步都会白跑）

| # | 动作 | 命令 / 文件 |
| --- | --- | --- |
| 0.1 | **重启 OpenClaw 网关**，broker 才认得新登记的模型档案（cheap 链第二环 `zai/glm-5.3-flash` 在重启前一直是死重量） | `openclaw gateway restart` |
| 0.2 | **跑安装脚本**（它会顺带把模型目录和网关对齐，无漂移时什么都不写） | `deploy/macos/install.sh` |

装 S1 投喂与 S3 大众源需要在跑脚本时带上环境变量，见第 4 节；不带就是这两条线不装，脚本会把原因打出来。

---

## 1. 批准数据源（每条一次 `approve`，记录路径 → 命令）

命令都是同一个形状：

```
"$VENV/bin/dalton-connector-governance" approve --path <记录> --approved-by "$OWNER"
```

按「批了这条会立刻开始动」的顺序排：

| # | 记录 | 批了之后会怎样 |
| --- | --- | --- |
| 1.1 | `$GOV/yfinance-daily-prices-v1.json` | 每日股价开始入库。**这是价格、异动、背离、估值分位四件事共同的前提** |
| 1.2 | `$GOV/yfinance-calendar-v1.json` | 催化剂日历开始每天查一次，公司卡上出现「下一个催化剂 T−N 天」 |
| 1.3 | `$GOV/guidepoint-search-library-v1.json` | 专家访谈摘录开始进发现队列（日 25 次搜索上限） |
| 1.4 | `$GOV/sales-notes-list-notes-v1.json` | 卖方 sales note 的枚举 |
| 1.5 | `$GOV/sales-notes-get-note-v1.json` | 读 note 正文。**1.4 与 1.5 要一起批**：只批枚举等于每 tick 列一遍然后什么都不读 |
| 1.6 | `$GOV/company-wiki-list-documents-v1.json` | 公司维基的枚举 |
| 1.7 | `$GOV/company-wiki-get-document-v1.json` | 读维基正文。同样成对 |
| 1.8 | `$GOV/xueqiu-search-posts-v1.json` | 雪球搜帖 |
| 1.9 | `$GOV/xueqiu-get-post-v1.json` | 雪球读帖（批了搜不等于批了读） |
| 1.10 | `$GOV/xueqiu-hot-rank-v1.json` | 雪球热榜（走的是 shim，见 4.3） |
| 1.11 | `$GOV/x-xreach-user-timeline-v1.json` | X 的时间线 |
| 1.12 | `$GOV/x-xreach-search-v1.json` | X 的搜索 |
| 1.13 | `$GOV/x-xreach-thread-v1.json` | X 的线程展开 |
| 1.14 | `$GOV/employee-reviews-blind-v1.json` | Blind 员工评价 |
| 1.15 | `$GOV/cn-hk-findata-financial-statements-v1.json` | A 股 / 港股报表。**本波没有对应的 lane**，批了也没有东西会跑；批它是为了以后 |
| 1.16 | `$GOV/cn-hk-findata-shareholders-v1.json` | 同上 |
| 1.17 | `$GOV/cn-hk-findata-buybacks-v1.json` | 同上 |
| 1.18 | `$GOV/cn-hk-findata-margin-balance-v1.json` | 同上（交易所汇总，证据层级比另外四个高，见 S4 报告第 6 节第 3 条） |
| 1.19 | `$GOV/cn-hk-findata-northbound-flow-v1.json` | 同上 |
| 1.20 | `$GOV/cn-hk-findata-ah-premium-v1.json` | 同上 |

**可以先不批的**：`$GOV/yfinance-analyst-estimates-v1.json`（Wave 2 才有消费者）、
`$GOV/guidepoint-get-transcript-v1.json`（见 1.21）。

| # | 动作 | 文件 |
| --- | --- | --- |
| 1.21 | **读一份裁决材料，然后决定要不要撤回一个批准**：Guidepoint 上游没有「读全文」这个操作，而 P13ae 时批过一份指向它的记录 | `$STATE/governance-decisions/guidepoint-get-transcript-narrowing-v1.json`（三个可选动作写在它的 `owner_actions` 里；撤回或让它继续休眠都行，**没有 lane 会加载这份记录**） |

---

## 2. 发一版新的 mission（一次发完，不要发九次）

用 `dalton-coverage-mission` 从当前版本派生一版，改三处：

| # | 改哪里 | 具体加什么 |
| --- | --- | --- |
| 2.1 | `autonomy.may_write` **加九个词** | `market_price`、`market_event`、`consensus_estimate`、`valuation`、`claim_index`、`research_task`、`dossier`、`debate_map`、`thesis_revision_candidate` |
| 2.2 | `autonomy.may_write` 确认已有 | `source_discovery`（S1 / S2 / S3 三条线都要它；live 第 13 版没有）、`observation`、`deliverable`、`forecast_line` |
| 2.3 | `autonomy.human_checkpoints` 加一个 | `thesis_revision_candidate`（缺它时判断层的 `revise_thesis` 只会记 `queued` 并点名 ADR-0007） |
| 2.4 | `source_plan` 把这些 `status` 改成 `connected` | `source:guidepoint`、`source:sales-notes`、`source:company-wiki`、`source:xueqiu`、`source:x`、`source:blind`、`source:web-search`；`source:alphaengine` 从 `probe_only` 改成 `connected` |
| 2.5 | `budget` 复核一次 | 见第 6 节的 AlphaEngine 上限问题 |

每个词缺席时会发生什么（驾驶舱会照实说）：

- 缺 `market_price` → 价格 lane 每 tick `ungranted`，一次网络调用都不发。
- 缺 `market_event` → 跟踪 lane 每 tick `ungranted`，**一个事件都不写**，于是判断层和反思整条链是空的。
- 缺 `claim_index` → 索引 lane 每次 `held: not_authorized`，一次模型调用都不花（刻意的，ADR-0004）。
- 缺 `research_task` → 专项研究入口答 `mission_does_not_grant_research_task`。
- `consensus_estimate` / `valuation` / `dossier` / `debate_map` 今天没有消费者，一起授予是为了少发几次版本。

---

## 3. 三份策略 / 版本（都不是连接器批准）

| # | 动作 | 怎么做 |
| --- | --- | --- |
| 3.1 | **重签 figure 准入策略**：发布并签署一版治理 policy，其 `research_candidate_auto_commit.rules` 列出 `research-auto-commit:mission-verified-figure:v1` | 常量在 `research_verification.MISSION_VERIFIED_FIGURE_RULE_REF`；做法照 ADR-0005 的 `research-auto-commit:mission-document-qualitative:v1`。**在这之前只有人工审阅那条路是通的，自动准入分支没接** |
| 3.2 | **把 verifier 的 phase pin 指到别处**：`VERIFIER_POLICY_REF`（`model-routing-policy-version:dalton-openclaw-verifier:1`）钉的是 `profile:gemini-3-7-flash`，网关早就不提供了；目录同步之后它是 `retired`，verifier 路由会被 **拒绝**（`profile_retired`）而不是在 broker 那里失败 | 换成 `verifier` 那一层的 fallback 链。**改一个不可变的 phase pin 是你的决定，INT2 没有替你做**（`src/dalton_core/model_deployment.py:27`） |
| 3.3 | **放宽 mandate 的范围**：live 第 7 版的 `scope_refs` 只有 `industry:us-it-services` 与 ACN，而最新计划里的三条 inquiry 全是 EPAM / IBM / CTSH，现在一条都进不来（每条都答 `out_of_mandate_scope`） | 发一版 mandate，`scope_refs` 覆盖 universe 五家 |

---

## 4. 装线（跑 install.sh 时带上，或事后补）

| # | 动作 | 命令 / 文件 |
| --- | --- | --- |
| 4.1 | **S1 投喂**：把 OpenClaw 工作区指给脚本（默认就是这个值，装在别处才要写） | `DALTON_OPENCLAW_WORKSPACE=~/.openclaw/workspace deploy/macos/install.sh` |
| 4.2 | **公司维基还差一个指针**：索引在 `wiki/vectors.db`，而语料里的路径是相对工作区根的，所以语料根必须**就是**工作区；索引要能在工作区根上按名字找到。安装脚本不往你的 OpenClaw 工作区里写东西，所以这一行由你来跑 | `ln -s wiki/vectors.db ~/.openclaw/workspace/wiki-index.sqlite`，然后重跑 install.sh |
| 4.3 | **S3 大众源的三个工具路径**（缺任一个就整条不装，脚本会打出提示） | `DALTON_AGENT_REACH_TOOL=$(which agent-reach) DALTON_XUEQIU_HOT_RANK_TOOL=<热榜 shim 的绝对路径> DALTON_XREACH_TOOL=$(which xreach) deploy/macos/install.sh`。热榜不是 agent-reach 的子命令，需要一个小 shim 补上 `<tool> hot-rank --limit N --stock-type T --json`；另两个子命令是 agent-reach 自己的 |
| 4.4 | **事件判断 lane 的两份模型配置**（成对，缺一则整条 lane 不装）。**两份必须指向不同 family 的 routing policy**，否则每次判断都会 `gated:same_family` 并且一次调用都不花——一个模型自己核验自己不是核验 | `$STATE/event-judgement-model-config.json` 与 `$STATE/event-verifier-model-config.json`，照 `initial-screen-model-config.json` 的写法 |
| 4.5 | **claim 索引的模型配置**（没有它整条索引 lane 不装） | `$STATE/claim-index-model-config.json` |
| 4.6 | **P14e 专项研究，三步缺一不可**：(a) 第 2 节已经授予 `research_task`；(b) 按清单逐个发布三个 ProbeTemplate，`actor_ref` 必须是 `human:`；(c) 写下 lane 的开关文件 | (b) 清单在 `$STATE/phase8/p14e-adhoc-probe-templates-v1.json`，用 `publish_probe_template`；(c) `echo '{"max_admissions_per_tick": 1, "retired_templates": []}' > $STATE/research-task-lane.json` |
| 4.7 | 装完 4.4–4.6 之后**再跑一次 install.sh**：plist 只在安装时渲染一次，一条因为文件出现才存在的 lane 要重装才会进 argv | `deploy/macos/install.sh` |

---

## 5. 批准之后再跑一次

S3 的七份记录是**按状态**而不是按文件是否存在开的：批准之前那条 lane 在任何机器上都是关的。
所以第 1.8–1.14 批完之后要重跑一次 `deploy/macos/install.sh`，plist 里才会出现 `--crowd-source-map`。

**这条线上还欠一行代码**（不是你的动作，记在这里以免被当成配置问题）：
`mission_crowd_source_lane.argv_fragment` 目前只输出 map 与治理目录，不输出三个工具路径，
所以在那一行补上之前，即使批准了记录、装好了工具，child 也会以「没有配置工具」拒绝。
安装脚本已经把三个工具按名字放在 `$STATE/host-tools/{agent-reach,xueqiu-hot-rank,xreach}`，
补的那一行从这里读即可。详见 INT2 报告第 7 节。

---

## 6. 一个要你回答的问题：AlphaEngine 的日上限

live mission 的 `budget.max_alphaengine_calls_24h` 是 **30**（manifest 值；这台机器上可能已被抬过，
以驾驶舱「进展」卡上的数字为准）。接上这一批之后，同一个 24 小时里抢这个额度的东西变多了：

- 既有的搜索发现与研报获取；
- **新增**：跟踪 lane 按 policy 基线每天拉 AlphaEngine **2 次 × 每家公司**（覆盖薄的公司大脑会自己拉长到 2–3 天一次）；
- **新增**：价格异动触发后 6 小时内，AlphaEngine 脱离基线立刻可拉。

四家过闸公司 × 每天 2 次 = 8 次/天只是跟踪那一半，且异动日会更多。
问题是：**30 次还够吗，还是先抬到 60–130 再看一周？** 两条路都合理，取决于你更怕漏消息还是更怕花钱：

- 抬上限：`scripts/raise_day_budget_cap.py`（它现在还会打印按默认分法四个池各是多少），然后发一版 mission。
- 不抬：把 `alphaengine` 的基线频率从 12 小时改成 24 小时（`$STATE/tracking-policy.json` 里的一行，
  不用改代码、不用发版本），代价是研报晚半天进来。

驾驶舱「今天的钱花在哪四件事上」那一块会告诉你哪一池先空——第一个空的多半是覆盖池或维护池。

---

## 7. 做完之后应该看到什么

跑完上面全部，打开驾驶舱：

- **公司卡**上出现：股价与估值、今日事件（按种类，每条带证据层级）、大脑的判断（五个决定词之一 + 一句理由 +
  真正落了什么 + 独立复核结论）、反思（我们当时以为 / 实际发生 / 可能漏掉的争论 / 想跟进什么）、
  下一个催化剂 T−N 天（日期是 vendor 猜的会写「日期未确认」）、我们多久看它一次、正在专项研究什么。
- **「系统此刻在做什么」**下面每条 lane 都有中文名和一个词的状态。仍然写着「等你批准数据源」的，
  是第 1 节漏掉的那一条；写着「缺授权，一次也没跑」的，是第 2 节漏掉的那个词。
- **「看每个来源能取什么」**：每条 connector 能取什么内容、信到什么程度、接没接上、还剩多少额度、多久看一次；
  通用源（网页搜索 / 网页抓取）单独标出来。下半页是模型：每一层的链、上一次是链上第几个模型服务的、
  以及这台机器的模型目录和网关一不一致（不一致时两个方向的差集都列名字）。
- **「每周回头看」**：上一周「我们把时间花在哪」的散文与每池一行的表，加上下周可以研究的问题候选。
- **待你裁决**：多出来的两类（`thesis_revision_candidate` 与 `gate_reopen`）会带中文标题和它的反思一起列出来，
  但**没有按钮**——裁决那两个的入口在另一条分支上，还没接。列出来是为了让你知道它们在排队，
  而不是给你一个按下去没反应的按钮。
