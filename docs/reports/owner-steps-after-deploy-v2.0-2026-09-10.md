# 部署之后你要做的事，按顺序 v2.0

日期：2026-09-10 · 生成自 deploy-rehearsal-2（分支 `ops-rehearsal-2`，base main `8717de0`）·
配套报告：[部署演练 #2](ops-deploy-rehearsal-2-v1.0-2026-09-10.md) · 机械步骤：[部署 runbook](deploy-runbook-v1.0-2026-09-09.md)

**本文取代 [v1.0](owner-steps-after-deploy-v1.0-2026-09-09.md)。** v1.0 是 INT2 那天的清单，
从那以后 main 又合入了大约二十片。凡两份说法冲突的地方，以本文为准；第 20 节列出
「v1.0 说过而现在不对」的每一条，免得你按记忆行事。

这份清单是「跑完 `deploy/macos/install.sh` 之后，还差哪些**只有你能做**的动作」。
安装脚本从不批准任何东西、从不签任何策略、从不发任何版本——它只是把要批的东西放到你面前。
没做的那些，驾驶舱会如实说出来（「等你批准数据源」「缺授权，一次也没跑」），不会装作在跑。

文中带「演练实测」或 live 行数的数字都是 2026-09-10 部署演练的历史快照；截至本次修订，
新集成仍未部署，不能把这些数字读成当前 live 已经运行新代码。

约定：

```sh
export DALTON_ROOT="$HOME/Library/Application Support/Dalton"
export STATE="$DALTON_ROOT/state/dalton-core"
export GOV="$STATE/connector-governance"
export DECISIONS="$STATE/governance-decisions"
export CONFIG="$DALTON_ROOT/config/service.json"
export VENV="$DALTON_ROOT/runtime/venv"
export REPO="$HOME/Projects/dalton-research-agent-os"
export DOMAIN="gui/$(id -u)"
export OWNER=human:lumos      # 换成你自己的主体
```

**三件事必须在服务停着的时候做**：备份（第 2 节）、日账本迁移（第 5 节）、以及
`thesis_impact.enabled` 翻成 true 之后的重装（第 13 节）。其余全部可以在服务跑着的时候做。

**八件事只有你能做，跳过任何一件都有代价，代价写在每一节里**：批准 26 份治理记录、
发一版 mission、放宽 mandate、发一份回答充分性策略、重签 figure 准入策略、
重指 verifier 的 phase pin、确认十条 IR 网址、把 live 的 tracking policy 从 v1 明确换到 v2。

---

## 1. 停服务（顺序不能反）

```sh
for label in space.lumos.dalton.thesis-impact space.lumos.dalton.control space.lumos.dalton.controller; do
  launchctl print "$DOMAIN/$label" >/dev/null 2>&1 && launchctl bootout "$DOMAIN/$label"
done
"$VENV/bin/python" "$REPO/src/dalton_core/launch_drain.py" --state-dir "$STATE" --timeout 600 && \
  launchctl bootout "$DOMAIN/space.lumos.dalton.writer"
launchctl list | grep space.lumos.dalton      # 期望：没有输出
```

期望：drain 打出一行 JSON，包含 `"drained": true`。若退出非零或 `"drained": false`，停止后续部署，待列出的子进程完成再重试；不要继续停止 writer。**controller 先走，writer 最后走**：反过来 controller 会在 drain 背后
继续起子进程，drain 白等十分钟。

`install.sh` 现在会先停止控制器、读取所有 lane ticket 等待子进程、等待 writer 完全停止，再升级 pip/运行时代码；drain 超时直接中止。这里提前停服务，是为了让后面的完整备份与部署对应同一静止状态。

---

## 2. 备份，以及一次部署前自检

```sh
"$VENV/bin/dalton-backup" create --backup-root "$STATE/backups" \
  --database core=$STATE/core.sqlite \
  --database scheduler=$STATE/scheduler.sqlite \
  --database model-router=$STATE/model-router.sqlite \
  --database thesis-impact-budget=$STATE/thesis-impact-budget.sqlite \
  --database projection=$STATE/dashboard-projection.sqlite
cp -R "$GOV" "$STATE/backups/connector-governance-$(date -u +%Y%m%dT%H%M%SZ)"
cp "$CONFIG" "$DALTON_ROOT/config/service.pre-8717de0-$(date -u +%Y%m%d).json"
```

期望：stdout 上一个快照 id，记下来，第 21 节要用。

治理目录那份 `cp -R` 要留：它是这台机器**给过哪些批准**的唯一记录，而种子是「只种一次」——
已经在盘上的记录永远不会被覆盖，所以丢一份的代价很高。

**部署前自检（新增，一行）**：

```sh
ls "$STATE/tick-ledger.sqlite" 2>/dev/null && echo "!! 见下方说明" || echo "ok: 没有 tick-ledger.sqlite"
```

期望：`ok`。这个文件应当由**第一次真实的 tick** 创建。演练 #2 的第一次运行因为演练脚本
「失败之后继续跑」而在 live 上建过一个，里面只有一行演练 tick；它已经被挪到
`/tmp/dalton-quarantine-20260910/tick-ledger.sqlite.rehearsal-stray`（挪走，没有删）。
如果这里还看得到这个文件，先把它挪走再装，否则真实账本的第一行是一次演练。
演练脚本已经修好（报告第 9 节），不会再有第二次。

---

## 3. 重载 OpenClaw 网关

```sh
openclaw gateway restart
```

期望：网关起来，`~/.openclaw/dalton-model-broker.sock` 重建。

为什么在装之前：便宜档 fallback 链的第二环是 `zai/glm-5.3-flash`，broker 要重读插件配置
之后才提供它。`~/.openclaw/openclaw.json` 里**已经**有这个 profile（演练确认，目录同步会
把 `profile:zai-glm-5-3-flash` 登记进 router），差的只是运行中的网关有没有重读。

不做的代价：链条的第二环是死重量——记一笔「这一环没服务」然后落到
`google/gemini-3.5-flash-lite`，**不丢东西**，但你以为的三环其实是两环。

装完之后核对，不写任何东西：

```sh
cd "$REPO" && PYTHONPATH="$REPO/src" "$VENV/bin/python" \
  scripts/sync_openclaw_model_catalog.py --check-only \
  --openclaw-config ~/.openclaw/openclaw.json \
  --model-router-db "$STATE/model-router.sqlite"
```

期望：`"catalog_in_sync": true`，退出 0。退出 2 表示还不一致。

---

## 4. 跑安装脚本（环境变量必须写在这一行上）

```sh
cd "$REPO" && git log --oneline -1
deploy/macos/install.sh
```

期望：pip 输出 → 种子块（全都已在盘上时它一声不吭）→ 目录同步的 JSON 报告 →
plist 路径 → `launchctl` 输出 → `dalton-health` 退出 0。

目录同步会**登记 5 个、退休 6 个**（演练实测）：登记
`profile:claude-fable-5-1`、`profile:gemini-3-8-flash`、
`profile:qwen-deepseek-v4-flash-0731-low-calibration`、`profile:zai-glm-5-3`、
`profile:zai-glm-5-3-flash`；退休 `profile:gemini-3-7-flash`、`profile:gemini-flash-latest`、
`profile:glm-5-2`、`profile:gpt-5-5`、`profile:openrouter-ox-alpha`、
`profile:qwen-deepseek-v4-pro`。第二次跑什么都不写。**同步失败会让安装失败**，不是警告。

### 4.1 环境变量：写在这一行上，事后 export 没有用

plist **只在安装时渲染一次**。一条「因为某个文件出现才存在」的 lane，必须在文件已经在盘上
的那次安装里才会进 argv。

| 变量 | install.sh 拿它做什么 | 不设的后果 |
| --- | --- | --- |
| `DALTON_OPENCLAW_WORKSPACE` | 默认 `~/.openclaw/workspace`。有 `skills/market-digest/output` 目录就种 sales-notes 两份记录 + feed plan，并把该目录 `ln -s` 到 `$STATE/feeds/market-digest-output`（**软链，不是拷贝**）；有 `wiki-index.sqlite` 就种 company-wiki 两份记录，并把**整个工作区** `ln -s` 到 `$STATE/feeds/company-wiki`（语料里的路径是相对工作区根的） | 打印 `note: no market-digest output at …` / `note: no wiki index at …`，两条投喂 lane 不装 |
| `DALTON_AGENT_REACH_TOOL`<br>`DALTON_XUEQIU_HOT_RANK_TOOL`<br>`DALTON_XREACH_TOOL` | 三个都要是可执行文件，**缺一个整块不装**（7 份大众源记录 + 每公司映射）。装上后软链到 `$STATE/host-tools/{agent-reach,xueqiu-hot-rank,xreach}` | 演练实测：`gate shut, 8 seed(s) not installed -- crowd-tools: not set to an executable: …`。热榜**不是** agent-reach 的子命令，要一个 shim 补上 `<tool> hot-rank --limit N --stock-type T --json`；另两个是 xreach 自己的 |
| `DALTON_EVENT_JUDGEMENT_MODEL_TIER=brain`<br>`DALTON_EVENT_VERIFIER_MODEL_TIER=verifier` | 成对写出 `$STATE/event-judgement-model-config.json` 与 `$STATE/event-verifier-model-config.json` | **只设一个 `exit 2` 且两个都不装；两个设成同一个值也 `exit 2`**——一个模型自己核验自己不是核验，路由会以 `gated:same_family` 拒绝，一次调用都不花。都不设：`event_judgement unconfigured`（演练实测） |
| `DALTON_ZERO_BASE_REVIEW_MODEL_TIER=brain`<br>`DALTON_ZERO_BASE_REVIEW_VERIFIER_MODEL_TIER=verifier` | 成对写出 `$STATE/zero-base-review-model-config.json` 与 `$STATE/zero-base-review-verifier-model-config.json` | **只设一个或显式 pin 相同会 `exit 2`**。运行时还按实际 route family 检查两者不同；无法解析或同 family 都拒绝，该公司复盘不发布 |
| `DALTON_CLAIM_INDEX_MODEL_TIER=cheap` | 写出 `$STATE/claim-index-model-config.json` | `claim_index unconfigured`（演练实测）。落 C2 的 maintenance 池，是四个池里最紧的（5%） |
| `DALTON_PLANNER_MODEL_PROFILE` / `_TIER` | 重写 `$STATE/research-planner-model-config.json`，**并把 `budget_db` + `budget_policy_ref` 写进去** | W3：Tier-1 规划调用不进日账本，op 结果一直是 `budget: {"status": "unbudgeted"}`，**你设的 25% `adhoc` 池对它管不着**。这台机器已经有这个文件，但要重跑一次才带上账本字段 |
| `DALTON_DELIVERABLE_MODEL_PROFILE` / `_TIER` | 写出 `$STATE/initial-screen-model-config.json` | 保持原有 pin。**注意这一份现在被四条 lane 共用**：initial screen、debate map、industry framework、conviction call（演练读 plist 确认） |
| `DALTON_EXTRACTION_MODEL_TIER` | 默认就是 `cheap`（链：`deepseek-v4-flash → zai-glm-5-3-flash → gemini-3-5-flash-lite`）。设成**空**才回到单一 pin | 默认已是链 |
| `DALTON_EXTRACTION_MAX_WINDOWS`（1–50）<br>`DALTON_EXTRACTION_NUMERIC_WINDOWS`（0–50）<br>`DALTON_EXTRACTION_DISCOVERY_WINDOWS`（0–50） | 校验后写进 `service.json`，并作为 `--extraction-max-windows` 传下去。**设了后两个而不设第一个是硬错误** | live 现值 30/10/10 —— 见第 19.2 节，这个组合超了 mission 的调用上限 |
| `DALTON_ALPHAENGINE_OWNER_CALL_CAP`（1–2000） | 校验后写进 `service.json` | 上限不变 |
| `DALTON_PRIOR_RESEARCH_DIR` | 指向团队既有研究根目录；目录下每家公司一层并带 `manifest.json`。安装脚本据此种两份 prior-research 治理记录、v2 feed plan，并软链到 `$STATE/feeds/prior-research` | 不设或目录不存在：打印 note，prior-research lane 不安装；不会猜盘上哪个目录属于团队研究 |

一个典型的完整安装命令：

```sh
DALTON_OPENCLAW_WORKSPACE=~/.openclaw/workspace \
DALTON_EVENT_JUDGEMENT_MODEL_TIER=brain \
DALTON_EVENT_VERIFIER_MODEL_TIER=verifier \
DALTON_ZERO_BASE_REVIEW_MODEL_TIER=brain \
DALTON_ZERO_BASE_REVIEW_VERIFIER_MODEL_TIER=verifier \
DALTON_CLAIM_INDEX_MODEL_TIER=cheap \
DALTON_PLANNER_MODEL_TIER=brain \
  deploy/macos/install.sh
```

跳过这些模型 lane 开关是**受支持的部署**：`claim_index`、`event_judgement` 与
`zero_base_review` 停在 `unconfigured`，一分钱不花。

### 4.2 tracking policy：v1 保留，live 已有文件不会被 install 覆盖

仓库同时保留 `deploy/phase9/p14a-tracking-policy-v1.json` 与新文件
`p14a-tracking-policy-v2.json`。v2 在 v1 基础上加入固定每日一次的 `sec-ownership`，并把 `sec`
的理由写清到 filing / issuer-purchases 路径。`install.sh` 只有在 `$STATE/tracking-policy.json`
不存在时才复制 v2；live 已有 v1 时重跑安装**不会覆盖**。

先读当前版本：

```sh
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["policy_ref"])' \
  "$STATE/tracking-policy.json"
```

若输出正是 `tracking-policy:p14a:v1`，服务仍停着时保留旧文件并显式换成 v2：

```sh
cp "$STATE/tracking-policy.json" "$STATE/tracking-policy.v1.pre-v2.json"
install -m 600 "$REPO/deploy/phase9/p14a-tracking-policy-v2.json" \
  "$STATE/tracking-policy.json"
```

再读一次，期望 `tracking-policy:p14a:v2`。仓库没有 tracking policy 的独立签名/发布命令；这里的
控制是版本化文件、copy-once 安装和 owner 明确替换，不能套用 figure policy 或 mission 的签名流程。
若当前既不是 v1 也不是 v2，先停下比较内容，不要覆盖未知的 owner 修改。

---

## 5. 迁移日账本——服务仍然停着

`install.sh` 不做这件事，writer 也不会做：日账本属于 thesis-impact 那个 agent。
`ALTER TABLE ADD COLUMN` 是即时的，但要一把写锁，而 live 的 writer 长期持有这个库的连接。

```sh
PYTHONPATH="$REPO/src" "$VENV/bin/python" -c \
  "from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore as S; S('$STATE/thesis-impact-budget.sqlite').close()"
sqlite3 "$STATE/thesis-impact-budget.sqlite" \
  "SELECT COUNT(*) FROM pragma_table_info('thesis_impact_day_admissions') WHERE name='pool';"
```

期望：第一条无输出、退出 0；第二条打印 `1`。
（`scripts/raise_day_budget_cap.py --apply` 也会顺带迁移；它的 dry run **不会**——dry run 是只读打开，
而只读打开一个 WAL 库需要 `-wal`/`-shm` 兄弟文件，服务停着时它会明说打不开，而不是顺手建出来。）

**头一天的数会对不上，这是对的。** 四个新列都可空，**一个 content hash 都没动**（演练在 live 副本上
重算过：10,035 行日账本，零移动）。但迁移之前写下的每一笔准入都是 `pool IS NULL`，
`pool_status` 把它们记在 `unpooled` 而不是猜一个池。演练实测 **5,042 / 5,046**。
随着这些准入结清，它自己会退。

**重放兼容**：迁移前记下的准入，其 `mission_binding` 没有池字段；迁移后重放同一次调用会带上池字段。
比较时「已存绑定完全没有池字段」按「同一次准入多了一个维度」处理（`_POOL_BINDING_KEYS`），
真改了别的字段仍然是冲突。部署当天看到重放被拒，先看这条。

跳过的代价：C2 整个是空转——每一分钱都落在 `unpooled_micros`，`pool_status` 什么都报不出来，
`skipped:pool_exhausted` 永远不触发。

---

## 6. 起服务，看第一个 tick

```sh
for label in space.lumos.dalton.writer space.lumos.dalton.controller space.lumos.dalton.control; do
  launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$label.plist"
  launchctl enable "$DOMAIN/$label"
  launchctl kickstart -k "$DOMAIN/$label"
done
"$VENV/bin/dalton-health" --config "$CONFIG" --max-age-seconds 45
sleep 20 && python3 -m json.tool "$STATE/run/heartbeat.json" | head -80
```

期望：`dalton-health` 30 秒内退出 0；心跳里**没有任何以 `unavailable:` 开头的 lane**。
每条 lane 应当是 `idle` / `launched` / `ungranted` / `unconfigured` / `held` / `deferred` 之一，
`tick_ledger` 应当是 `recorded`。演练对这份代码、这份状态的完整表在
[演练报告第 1 节](ops-deploy-rehearsal-2-v1.0-2026-09-10.md)，**31 条 lane，零逃逸**。

`launched` 只表示起了子进程，**不表示它做成了什么**——结果在下一个 tick 的 `settled` 里。
一条记录还是 `proposed` 的 lane 读作 `launched`，是子进程 fail-closed，看上去和成功一模一样。

```sh
sqlite3 "$STATE/tick-ledger.sqlite" \
  "SELECT day, COUNT(*), SUM(idle) FROM tick_ledger_ticks GROUP BY day ORDER BY day DESC LIMIT 3;"
```

期望：今天一行，计数每五秒涨一次。

---

## 7. 批准治理记录——26 份，按 lane 分组

命令都是同一个形状（`show` 先看一眼，`approve` 落下去）：

```sh
"$VENV/bin/dalton-connector-governance" show    --path "$GOV/<记录>.json"
"$VENV/bin/dalton-connector-governance" approve --path "$GOV/<记录>.json" --approved-by "$OWNER"
```

期望：`show` 打出 `"status": "proposed"`，`approve` 打出带 `"status": "approved"` 和你的
`approved_by` 的记录。全部由 `install.sh` 种成 `proposed`、`chmod 600`、**只种一次**。

**批准是 append-only 的**：没有「取消批准」，只有再发一版记录。

### 7.1 市场层（P11a / P11b / C1）——先批这三份

| 记录 | 批了之后 | 不批的代价 |
| --- | --- | --- |
| `yfinance-daily-prices-v1.json` | 每日 K 线开始入库 | **价格、异动、背离、估值分位四件事共同的前提。** 没有它 P14a 的价格事件恒为零、P11c 没有估值快照、P12a 的「价格驱动史」永远 `no_market_data`、P13-M3 桥不了目标价 |
| `yfinance-calendar-v1.json` | 每天每公司查一次日历；公司卡出现「下一个催化剂 T−N 天」；P14f 的 T−30 / T+0..T+2 窗口才有东西可开 | lane 照样每 tick 起子进程，子进程在碰网络之前以 `yfinance calendar governance record is not approved` 拒绝。**tick 里读作 `launched`，不是 `unconfigured`**——拒绝在下一层 |
| `yfinance-analyst-estimates-v1.json` | P11b 路线 2（vendor 一致预期）开始出 `ConsensusEstimateVersion`，日上限 50 单位 | 只有路线 2 停。**路线 1 照跑**：研报已经在库里、已经带哈希，读它既不花配额也不碰网络 |

（v1.0 说这份可以先留 `proposed`。现在不对了——P11b 已经合入，它就是消费者。）

### 7.2 专家访谈（S2）

| 记录 | 批了之后 | 不批的代价 |
| --- | --- | --- |
| `guidepoint-search-library-v1.json` | 专家访谈摘录进发现队列（日 25 次搜索上限） | 演练实测 `guidepoint_discovery idle / all_grants_refused`——lane 装上了，一次调用都不发 |

`guidepoint-get-transcript-v1.json` **不要批**，见 7.7。

### 7.3 人工投喂（S1，两两成对）

| 记录 | 批了之后 |
| --- | --- |
| `sales-notes-list-notes-v1.json` | 枚举卖方 note |
| `sales-notes-get-note-v1.json` | 读 note 正文 |
| `company-wiki-list-documents-v1.json` | 枚举公司维基 |
| `company-wiki-get-document-v1.json` | 读维基正文 |

**必须成对批**：只批枚举等于每 tick 列一遍然后什么都不读。S1 实测正文读取把可归属的 note
从 15 份抬到 231 份（15 倍）。

company-wiki 这一对**在这台机器上还没被种下来**（演练实测：`gate shut, 2 seed(s) not installed --
company-wiki: no wiki index at ~/.openclaw/workspace/wiki-index.sqlite`）。先补第 17.1 节那个软链，
重跑一次 `install.sh`，才有东西可批。

### 7.4 大众源（S3，七份 + 一份映射，全有或全无）

| 记录 | 批了之后 |
| --- | --- |
| `xueqiu-search-posts-v1.json` | 雪球搜帖 |
| `xueqiu-get-post-v1.json` | 雪球读帖（批了搜不等于批了读） |
| `xueqiu-hot-rank-v1.json` | 雪球热榜。**今天没有任何 coordinator 排它**——lane 问的是每公司的问题，热榜是全市场的 |
| `x-xreach-user-timeline-v1.json` | X 时间线 |
| `x-xreach-search-v1.json` | X 搜索 |
| `x-xreach-thread-v1.json` | X 线程展开 |
| `employee-reviews-blind-v1.json` | Blind 员工评价 |

这七份**在这台机器上也还没种**（第 4.1 节三个工具变量没设）。而且这条线是
**按状态开的，不是按文件存在开的**：批准之后要**再跑一次 `install.sh`**，plist 里才会出现
`--crowd-source-map`（第 18 节）。

⚠️ **这条线还欠一行代码，不是你的动作**：`mission_crowd_source_lane.argv_fragment` 今天只吐
map 与治理目录，**不吐三个工具路径**，所以在那一行补上之前，即使记录批了、工具装了，
子进程也会以「没有配置工具」拒绝。`install.sh` 已经把三个工具按名字放在
`$STATE/host-tools/`，等那一行去读。

### 7.5 SEC 持股与内部人（S5，四份，全有或全无）— **v1.0 没有这一组**

| 记录 | 批了之后 |
| --- | --- |
| `sec-form4-transactions-v1.json` | 内部人 Form 3/4/5 |
| `sec-beneficial-ownership-v1.json` | SC 13D/G |
| `sec-form144-notices-v1.json` | Form 144 |
| `sec-form13f-holdings-v1.json` | 13F-HR |

**按 op 批，不是按 lane 批**：`SecOwnershipLauncher.approved_operations()` 只返回有批准记录的 op。
只批 Form 4 的 Core 就只读 Form 4，其余在 tick 摘要里算 `unapproved`——lane 不会因此拒绝启动。

这条 lane（order 88，每 tick 一个子进程、一家公司）从**已经落盘的 `list_filings` 字节**里挑候选，
**挑候选零 SEC 调用**。只产事件，**永远不产数字**（`OWNERSHIP_GRADE = "regulatory-ownership-filing"`）。
live 的 spool 已经有料：五家十年 3,541 份 Form 3/4/5、105 份 SC 13D/G、178 份 Form 144、7 份 13F-HR。

这条 lane 还要 mission 同时授予 `market_event` **和** `observation`，两个都在才起子进程。

### 7.6 IR 页面监视（S5，两份）— **v1.0 没有这一组**

| 记录 | 批了之后 |
| --- | --- |
| `ir-page-watch-list-watches-v1.json` | 列出本地 changedetection.io 有哪些 watch |
| `ir-page-watch-get-diff-v1.json` | 读某一页的变更内容（两者里更重的一份） |

**批这两份本身什么都不打开。** 这条线的真开关是第 15 节那个网址声明文件。

### 7.7 A 股 / 港股（S4，六份）——批了也不会有东西跑

`cn-hk-findata-financial-statements-v1.json`、`-shareholders-`、`-buybacks-`、
`-margin-balance-`（交易所汇总，证据层级比另外几个高）、`-northbound-flow-`、`-ah-premium-`。

**本波没有对应的 lane**（`lane_registry` 一行都没加），而且 mission universe 全是美股。
批它们是为了把六个 schema 哈希分开看一遍，为以后留档。**不要**把
`source:cn-hk-findata` 写进 mission 的 `source_plan`——没有 lane 会读它。

### 7.8 一份要读、不要批的裁决材料

`$DECISIONS/guidepoint-get-transcript-narrowing-v1.json`。Guidepoint 上游没有「读全文」这个操作，
而 P13ae 时批过一份指向它的记录。三个可选动作写在它自己的 `owner_actions` 里：撤回
`approval:connector-governance:guidepoint-get-transcript:v1`、或明知故犯让它继续休眠、
或确认 `capability:dalton:connector:guidepoint-get-transcript` 保持不发布。
**没有任何 lane 会加载这份记录**，所以怎么选都不会让什么东西停下来。
它被 `install.sh` 刻意种在 `governance-decisions/` 而不是 `connector-governance/`：
一份永远 `proposed` 的记录放在驾驶舱会看的目录里，等于给你看一条「在等一个关于无的批准」的 lane。

### 7.9 三份刻意不种的记录

`roic-list-transcripts-v1.json`、`roic-get-transcript-v1.json`（roic.ai 自 2026-08-29 起全站 403，
writer 里没有任何东西加载这两份记录）、以及 7.8 那份。
`install.sh` 用 `DELIBERATELY_UNSEEDED` 数组点名，并有测试保证「每一份已提交的记录要么被种、
要么被点名」。你什么都不用做，知道这三处缺席是故意的就行。

---

## 8. 发一版 mission——**只发这一版**

live 是 `coverage-mission-version:us-it-services:13`。它授了 11 个词，
缺 11 个；checkpoints 有 7 个，缺 3 个。全部在一版里补齐。

### 8.1 先跑参数生成器（只读 live Core，什么都不写）

```sh
cd "$REPO"
PYTHONPATH="$REPO/src" "$VENV/bin/python" scripts/build_mission_v2_params.py \
  --source-core "$STATE/core.sqlite" \
  --mission-ref coverage-mission:us-it-services \
  --add-scope market_price \
  --add-scope market_event \
  --add-scope consensus_estimate \
  --add-scope valuation \
  --add-scope claim_index \
  --add-scope research_task \
  --add-scope dossier \
  --add-scope debate_map \
  --add-scope forecast_revision_proposal \
  --add-scope thesis_revision_candidate \
  --add-scope conviction_call \
  --add-checkpoint thesis_revision_candidate \
  --add-checkpoint gate_reopen \
  --add-checkpoint conviction_call \
  --set-source-status source:guidepoint=connected \
  --set-source-status source:company-ir=connected \
  --set-source-status source:sales-notes=connected \
  --set-source-status source:company-wiki=connected \
  --set-source-status source:xueqiu=connected \
  --set-source-status source:x=connected \
  --set-source-status source:blind=connected \
  --output /tmp/us-it-services-mission-v14.params.json
```

演练跑过这一条（对着 live 的只读副本），输出的 `autonomy.may_write` 逐字是：

```
evidence, claim, forecast_line, model_run, research_question, observation,
stage_record, forecast_reconciliation, source_discovery, claim_challenge,
deliverable, market_price, market_event, consensus_estimate, valuation,
claim_index, research_task, dossier, debate_map, forecast_revision_proposal,
thesis_revision_candidate, conviction_call
```

`version_id: coverage-mission-version:us-it-services:14`，
`prior_version_ref: coverage-mission-version:us-it-services:13`。
这 22 个词就是 `AUTOMATION_WRITE_SCOPES` 的全部——加完之后没有第 23 个词可加。

十一个新词，每一个缺席时会发生什么：

| 词 | 谁要 | 缺了会怎样 |
| --- | --- | --- |
| `market_price` | P11a | 价格 lane 每 tick `ungranted`，一次网络调用都不发（演练实测） |
| `market_event` | P14a / C1 / S5 / P14f | 跟踪 lane 一个事件都不写，判断层与反思整条链是空的；持股 lane 不起子进程；日历 lane 报 `events_ungranted`（且**不消耗那家公司的重试预算**）；业绩校准一分钱不花 |
| `consensus_estimate` | P11b | 两条路都静默（演练实测 `mission_consensus ungranted`） |
| `valuation` | P11a | 没有估值快照，桥不了目标价 |
| `claim_index` | P12b | 索引 lane `held: not_authorized`，一次模型调用都不花（刻意的，ADR-0004）。**今天它会退回用 `claim` 跑**，加了这个词之后 `FALLBACK_WRITE_SCOPES` 那一行就可以删 |
| `research_task` | P14e / P15a | 专项研究入口答 `mission_does_not_grant_research_task`；ask v2 的「补一次」按钮是灰的 |
| `dossier` | P12a | 档案 lane `held: not_authorized`，**一分钱不花**；下游 P12d 报 `held: no_dossier_authority`，连草稿都不起 |
| `debate_map` | P12c | 争论图 lane `held: not_authorized` |
| `forecast_revision_proposal` | P14a / P14f | 事件永远提不出预测修订。（人闸那一侧的词是 **`forecast_overturn`**，live 已经有了） |
| `thesis_revision_candidate` | P14a / P14b / P14f | 见下，**还要一个同名的 checkpoint** |
| `conviction_call` | P15d | 判断 lane 停住 |

`consensus_estimate` / `valuation` / `dossier` / `debate_map` 今天的消费者还在长，
一起授予是为了少发几次版本。

### 8.2 核对自动生成的 checkpoints

持续开发修复后，`build_mission_v2_params.py --add-checkpoint` 会校验封闭词表、追加缺项并去重；8.1 已包含三个 flag，不再需要手改 params。该历史快照生成后应当是：

```
deep_insight_gate, investment_memo, thesis_admission, thesis_revision,
forecast_overturn, scope_expansion, budget_expansion,
thesis_revision_candidate, gate_reopen, conviction_call
```

| 新 checkpoint | 谁要 | 缺了会怎样 |
| --- | --- | --- |
| `thesis_revision_candidate` | P14a / P14b / P14f | `revise_thesis` 只记 `queued` 并点名 ADR-0007。**范围和 checkpoint 两个都要**，缺任一个都是这个结果 |
| `gate_reopen` | P14d 重开 lane | 演练实测：`mission_reopen ungranted / autonomy.human_checkpoints 里没有 gate_reopen`。**它没有、也不该有 `may_write` 词**——提案没人裁决就一文不值，所以要看的是 checkpoint |
| `conviction_call` | P15d | 只给范围会让自动化提一条没人答应裁决的提案，子进程会拒绝起草并报 `no_checkpoint` |

`deep_insight_gate` **已经在里面**（Playbook 强制），不用动。

F18 已修：8.1 的五个 `--set-source-status source:<name>=connected` 会在缺行时按打包
connector inventory 补建合法的三字段行，`role` 写明匹配到的 connector 和
`created_by=set-source-status`。inventory 不认识的 source 仍然 fail-closed；不再手改 JSON。

三个新 checkpoint 由生成器加入，并已通过真实 CoverageMissionAuthority 发布回归。
`source:cn-hk-findata` **不要加**（7.7）。

`source:alphaengine` 已经是 `connected`——v1.0 说它是 `probe_only`，那句话现在不对了。

### 8.3 发布

```sh
"$VENV/bin/dalton-gov" \
  --token-config "$STATE/writer-tokens.json" \
  --socket "$STATE/run/writer.sock" \
  --actor "$OWNER" \
  --operation create_coverage_mission \
  --params /tmp/us-it-services-mission-v14.params.json
sqlite3 "$STATE/core.sqlite" \
  "SELECT mission_version_id FROM coverage_mission_versions ORDER BY created_at DESC LIMIT 1;"
```

期望：一条带 `"version": 14` 的 JSON 记录；查询打出
`coverage-mission-version:us-it-services:14`。需要 writer 在跑，所以这一步在第 6 节之后。

⚠️ **发版之前先把手上悬着的深度认知门裁决掉。** 阶段账本按 mission 版本记，
`carry-forward` 只把 `entered` 带到新版本，所以一份绑在第 13 版上的门草稿，在第 14 版发出去之后
按钮就没了（`decidable: false` 并说明理由）。这是 P12d 报告点名的耐久缺陷，修法在别人手上；
今天的对策就是**先裁决，再发版**。

（v1.0 §2 说用 `dalton-coverage-mission`。**没有这个入口。** 用上面这两步。）

---

## 9. 放宽 mandate 的范围（P14e）

live 的第 7 版 mandate，`scope_refs` 只有 `industry:us-it-services` 与 ACN。
最新计划里的三条 inquiry 全是 **EPAM / IBM / CTSH**，现在一条都进不来——每条都答
`out_of_mandate_scope`。

发一版 mandate，`scope_refs` 覆盖 universe 五家（ACN / CTSH / EPAM / IBM / DXC）。

期望：P14e 的准入模拟对三条 inquiry 全部放行，各 `max_rounds 2 / max_cost_units 2 /
max_seconds 240`，每条预留 $1.00，合计 $3.00，占当日 `adhoc` 池的 12%。

跳过的代价：专项研究（第 14 节）即使把三步都做完也一条都进不来；
ask v2 的「补一次」也一样。

顺带：第 10 节那份回答充分性策略是按「覆盖这家公司的 mandate」找的——
放宽之后只有一份活跃 mandate，规则仍然成立。

---

## 10. 发一份回答充分性策略（ask v2 的「补一次」）

live 现在**一份都没有**，所以 `active_policy_for_company` 返回 `policy_unavailable`，
ask v2 的刷新按钮是灰的。

对象是 **AnswerSufficiencyPolicyVersion**，契约在
`contracts/answer-sufficiency-policy-version.schema.json`，要发 `schema_version: "0.2"`。
writer op 是 `publish_answer_sufficiency_policy`，字段：

```
policy_ref, mandate_version_ref, mandate_version_hash, thresholds,
refresh_route, adhoc_research_route, effective_from, effective_until,
actor_ref, version_id, prior_version_ref, idempotency_key
```

`actor_ref` 必须是 `human:`。要打开的那一处是
`adhoc_research_route.enabled = true`，并配**正的** `max_cost_units` 与 `max_rounds`——
校验器两边都卡：启用了却给零预算报「enabled ad-hoc research requires a positive day budget
and rounds」，停用了却留着预算报「disabled ad-hoc research cannot retain budget or rounds」。
`schema_version: "0.1"` 的策略仍然会被拒（那一版已经发布并被读过，禁令保留）。

期望：`active_policy_for_company(...)` 返回 `active`（不是 `unavailable` / `stale`），
答案里带 `answer_policy.state = active`，按钮亮起来。

一次问答最多花：一次发现调用（占 AlphaEngine 的 130/24h 或 web-search 的配额）
+ 一次额外模型调用，都在 mission 日预算与 `adhoc` 池（$25/天）之内，**同一个问题只补一次**。

⚠️ **发之前要先裁一件事。** P15a 把 Python 校验器放开了，但两份 JSON 契约还钉着旧禁令：
`contracts/answer-sufficiency-policy-version.schema.json` 里
`adhoc_research_route.enabled` 仍是 `{"const": false}`（`max_cost_units` / `max_rounds` 同样是
`{"const": 0}`），`contracts/answer-route-decision.schema.json` 里
`adhoc_research_route_available` 仍是 `{"const": false}`。
要么把 policy schema 升到 0.3 并允许 `enabled`，要么明确「专项研究永远只从 planner 侧进入」。
**在裁掉这件事之前发布，契约一致性检查会和运行中的代码互相矛盾。**

不发也没关系：答案照样会说「它想去哪取什么」，那句话本身就是让你决定要不要发这两版的依据。

---

## 11. 重签 figure 准入策略（ADR-0007）

发布并签署一版治理 policy，其 `research_candidate_auto_commit.rules` 列出
`research-auto-commit:mission-verified-figure:v1`。常量在
`src/dalton_core/research_verification.py`（`MISSION_VERIFIED_FIGURE_RULE_REF`），
做法照 ADR-0005 的 `research-auto-commit:mission-document-qualitative:v1`。

同时把促进器的调用方显式传 `figure_admission_policy="verified_figure"`——
`promote_figure` 已经这样传了，`stage()` 的默认值不动（默认仍是
`reject_cited_quantitative`，改变「Ledger 认什么是数字」是治理决定，应当是有人按下去的）。

```sh
sqlite3 "$STATE/core.sqlite" \
  "SELECT id FROM governance_policy_versions ORDER BY created_at DESC LIMIT 1;"
```

跳过的代价：数字只能走人工审阅那条路（`commit_reviewed_candidate`，已在 live 副本上跑通）。
**那是一条能走的路，不是坏掉的路**，所以这一步可以晚一天。
注意本波实现的是人工审阅那条；policy 自动准入（`commit_policy_candidate` /
`research_auto_commit`）要在这条 rule ref 生效之后再接一次。

---

## 12. 重指 verifier 的 phase pin

`VERIFIER_POLICY_REF`（`model-routing-policy-version:dalton-openclaw-verifier:1`）钉的是
`profile:gemini-3-7-flash`。broker 已经很久不提供它，而第 4 节的目录同步会给它一个
`retired` 版本——演练在副本上确认了这一点。

后果是一个**更好的失败**：verifier 路由在 router 那里就被 `profile_retired` 拒绝，
而不是到 broker 那里才炸。但它仍然是失败，每一次 verifier 调用都会吃到。

要换的目标是 `verifier` 那一档的 fallback 链：

```
profile:claude-fable-5-1 -> profile:zai-glm-5-3 -> profile:gemini-3-5-flash-lite
```

做法：先跑第 4 节的目录同步（否则 `independence_report` 会报
`profile:zai-glm-5-3 is not registered with the router`），然后跑一次

```python
ensure_reopened_verifier_policy(router, created_at=now)
```

它会追加一版 `model-routing-policy-version:dalton-openclaw-verifier:2`（链在 v1 之后），
允许的 profile 是 `profile:zai-glm-5-3`（zhipu-glm-5.3）与
`profile:gemini-3-5-flash-lite`（google-gemini-3）；`profile:claude-fable-5-1` 刻意不在里面。

期望：`independence_report(router)` 返回 `live: True`。

**改一个不可变的 phase pin 是你的决定**（常量在 `src/dalton_core/model_deployment.py:27`）。
今天 `thesis_impact.enabled` 是 `false`，所以没有人在调 verifier——这一步可以推迟，
但**不能被忘掉**：不做这一步就把 thesis-impact 打开，每一次调用都 fail closed。

---

## 13. thesis-impact：先跑一次实盘 canary，然后才翻开关

顺序是硬的：**第 4 节目录同步 → 第 12 节 pin v2 → 本节 canary → 翻 `enabled`**。

```sh
# 指向 REOPENED_VERIFIER_POLICY_REF
PYTHONPATH="$REPO/src" "$VENV/bin/python" scripts/run_real_thesis_impact_canary.py ...
# 或 thesis_impact_calibration_runner.run_live_calibration
```

把输出喂给 `score_verifier_outputs`，要求 **`automation_eligible=True`**：
30 例全评、检出率 ≥ 90%、零高危漏检。成本是 30 次 verify 调用
（`profile:zai-glm-5-3` 约 1.4 / 4.4 USD 每百万 token）。

**没跑之前不要把 `thesis_impact.enabled` 翻成 true。** `flag_state` 只检查「钉是活的」
与「mission 授了权」，它检查不了「这个模型能不能干这活」。

确认之后：`service.json` 里 `thesis_impact.enabled` 置 `true`，`config` 块原样保留，
**重跑一次 `install.sh`**（plist 由 `macos_launchagent` 按这个标志渲染或删除），然后重启。

跳过：thesis-impact 继续停着，其余一切照跑。

---

## 14. 专项研究（P14e）：三步缺一不可

| 步 | 动作 | 命令 / 文件 |
| --- | --- | --- |
| (a) | mission 授 `research_task` | 第 8 节已经做了 |
| (b) | **发布 ProbeTemplate** | 清单在 `$STATE/phase8/p14e-adhoc-probe-templates-v1.json`（`install.sh` 种的）。writer op 是 `publish_probe_template`，`actor_ref` **必须**是 `human:` |
| (c) | 写下 lane 开关 | `echo '{"max_admissions_per_tick": 1, "retired_templates": []}' > "$STATE/research-task-lane.json"` |

**只发 `status: active` 的那一个**：`probe-template:adhoc-sec-filings-index:v1`
（`operation: get_company_facts`，`permission_scope: public_sec_read`，成本 1 / 2 / 120）。
另外两个（`adhoc-alphaengine-search-library:v1`、`adhoc-web-search:v1`）在清单里就是
`retired`，`retired_reason: no executor: ...`——**不要发**，目录不该宣称一个执行不了的能力。

期望：`bindable_templates()` 非空；驾驶舱 `research_task_view.templates` 里正好那一个；
tick 里 `research_task` 从 `unconfigured`（演练实测）变成 `idle` / `launched`。

跳过 (b)：`no_executable_adhoc_template_published`。
跳过 (c)：**lane 整个不存在，LaunchAgent 的 argv 一字不变**。
这三步之外不需要别的：ProbeTemplate 发布本身就是「policy version enables it」那一腿
（人签、版本化、append-only），没有第四个开关。撤销有三条路：用一个执行器不接受的
`(operation, permission_scope)` 对重发、目录层改 `status: retired`、
或者本机层把模板 ref 写进 `research-task-lane.json` 的 `retired_templates`（今晚生效，不用发版）。

预算：live 日上限 $100，`adhoc` 池 25% = **$25/天** ÷ $1.00 一条 = 一天最多 25 条专项研究，
而 extraction 的那 $55 一分不动。

⚠️ **打开这道闸之前先修三处已知缺陷**（W2 报告点名，不是你的动作，但会在授权当天咬人）：
`research_task.py:553` 拼 SEC companyfacts URL 时没有把 CIK 补零，
而 DXC 的 `company:sec-cik:001688568` 是九位——live 五家里唯一一个（一行 `zfill(10)`）；
`agenda_control.py:1117` 构造 `AgendaControlPlane(config)` 时没给 grant resolver，
`adhoc_research_enabled()` 是一个恒 `False` 的表达式；以及上面那两个绑不上的模板。

---

## 15. IR 页面：确认十条网址（S5）

`install.sh` **刻意不做这件事**，理由写在它自己的注释里：那十条网址要有人先确认，
一条填错就把一家公司的新闻记到另一家名下。

```sh
cp "$REPO/deploy/phase9/p9-us-it-services-ir-pages-v1.json" "$STATE/ir-pages.json"
chmod 600 "$STATE/ir-pages.json"
```

**十条网址** = 五家 × 两页（`press_releases` + `events`）。文件形状：

```json
{"schema_version": "0.1", "id": "ir-page-map:us-it-services:v1",
 "industry_ref": "industry:us-it-services", "note": "...",
 "companies": [{"company_ref": "company:sec-cik:0001467373", "ticker": "ACN",
   "pages": [{"url": "https://investor.accenture.com/news-and-events/news-releases",
              "page_kind": "press_releases", "label": "..."},
             {"url": "...events-and-presentations", "page_kind": "events", "label": "..."}]}]}
```

**你的动作是确认那十条，不是写那个文件**——仓库里已经有一份填好的。
S5 报告点名 **IBM 与 DXC 最可能写错**，先看这两家。

拷完要**重跑一次 `install.sh`**，argv 才会长出
`--ir-page-declaration "$STATE/ir-pages.json"`。

前置：changedetection.io 要跑在 `127.0.0.1:5055`，并且**已经为这十条网址建好 watch**
（`--base-url` 在代码里限死回环）。没建就是 `undeclared_count: 0`、`changes: []`、不报错。

跳过的代价：IR 监视整条不开——`argv_fragment` 不吐这个参数，lane 报 watcher `unconfigured`
（不是错误）。**范围由这个文件定，不由本地工具定**：没声明的 watch 会被列出来但永远不读
（tick 摘要里的 `undeclared_count`），`get_watch_diff` 对一个没声明的 watch 会带理由拒绝。

---

## 16. 13F 持有人名单：一份要你从零写的文件（S5）

**这个文件今天不存在。** 建议路径
`deploy/phase9/p9-us-it-services-13f-holders-v1.json`，形状是**每家公司一组管理人 CIK 与理由**。

为什么是你写而不是工程师写：13F 是**机构**报的，不是发行人报的，所以「谁在买 ACN」
不在发行人的索引里。跟前十大持有人？跟几家有观点的长线基金？跟维权方？
这是研究范围的决定，不是工程决定。机器已经建好（`compare_holdings`、缺上一季时的
`prior_absent`），**没人决定要跟谁**。

证据：live 五家发行人索引里唯一的 13F-HR 是 **Accenture 自己作为管理人报的 7 份**，
lane 对这些把 `holder_cik` 设成公司自己的 CIK。

不写这份名单会有两件事一直空转：

- **CUSIP 过滤**：子进程支持 `--company-cusips`（有测试），但 lane 不传——因为没有
  `company_ref → CUSIP` 映射。没有它，一家大管理人的持仓表会吐出最多
  `MAX_EVENTS_PER_RUN = 40` 条不相关的仓位变动。（这张 CUSIP 表**大概率长在已经在读的
  SC 13D/G 封面页里**，而不是手写出来的。）
- **上一季对账**：`--prior-file` / `--prior-accession` 都实现并测过了（包括绑到上一季文件的
  `exit` 事件），但 lane 不传——那本账只有在管理人名单存在之后才有意义。
  在那之前**每一次 13F 都是 `prior_absent` 的第一次读，这是正确的默认，不是缺陷**。

顺带还有三个 S5 留给你的决定：回补多深（lane 默认窗口 90 天；十年 = 3,541 份 Form 4 + 105 份
13D/G，按每天 20 份要跑半年；12 个月 ≈ 400 份 Form 4 ≈ 两周；只回补 13D/G ≈ 105 份 ≈ 一周；
或者不回补）；Form 3 与 Form 5 要不要继续和 Form 4 共用 `form4_transactions`
（收窄 = 从 `FORMS_BY_OPERATION` 删两个字符串 + 改一个测试）；
SC 13D 的 Item 4 要不要留全文（今天只留 `purpose_text_hash`）。

---

## 17. 还要手写的几个文件

`install.sh` 今天不写这些，也没有对应的环境变量。

### 17.1 公司维基的软链（S1）

```sh
ln -s wiki/vectors.db ~/.openclaw/workspace/wiki-index.sqlite
```

真索引在 `~/.openclaw/workspace/wiki/vectors.db`（183 MB）；
`skills/company-wiki/wiki.db` 是一个 **0 字节的占位**。语料里的 `filepath` 是相对工作区根的，
所以语料根必须**就是**工作区，而索引要能在工作区根上按名字找到。
**`install.sh` 从不往你的 OpenClaw 工作区里写东西**，所以这一行由你来跑，然后重跑 `install.sh`。

跳过：演练里那句 `gate shut, 2 seed(s) not installed -- company-wiki: no wiki index at …`，
company-wiki 两份记录不种、lane 不装。

### 17.2 公司档案 lane 的两个文件（P12a），也是深度认知门的前置

lane 的 `argv_fragment` 两个文件都在才吐参数：

```
--company-dossier-policy              $STATE/p12a-dossier-policy-v1.json
--company-dossier-verifier-model-config  $STATE/dossier-verifier-model-config.json
```

policy 从 `$REPO/deploy/phase9/p12a-dossier-policy-v1.json` 拷过去（源码 checkout 里的默认路径
在安装环境不成立）。verifier 那一份要**新写**，而且它必须能路由到与起草**不同的 model family**
——一份配置跑两次调用必然同 family，事后拒绝等于花十二次调用的钱得到同一个答案，
所以 lane 在第一次起草**之前**就 `held`。

演练实测：这两个文件都不在，所以 plist 里**根本没有** `--company-dossier-model-config`，
`company_dossier` 与 `deep_insight_gate` 双双 `unconfigured`。
缺 policy 报 `held / no_policy`；缺 verifier 报 `held / no_verifier`；两种都不花钱。

### 17.3 业绩季 lane 的两份模型配置（P14f）

```
$STATE/earnings-season-model-config.json
$STATE/earnings-season-verifier-model-config.json
```

两者缺一 `argv_fragment` 返回空，lane 不装（演练实测 `earnings_season unconfigured`）。
**今天没有 `DALTON_EARNINGS_*` 变量**——照 `initial-screen-model-config.json` 的写法手写，
或者等有人给 `install.sh` 补一个块。两个 purpose（`earnings_preview` / `earnings_calibration`）
在 `model_fallback_chain` 里已经是 `brain` 档，不用另外登记。

### 17.4 四条 lane 共用一份模型配置——知道就好

演练读 plist 确认：`--initial-screen-model-config`、`--debate-map-model-config`、
`--industry-framework-model-config`、`--conviction-call-model-config` **指的是同一个文件**
`$STATE/initial-screen-model-config.json`。同一条路由链、同一本日账、同一个 broker。

这件事对 P12c 有直接后果：争论图的 verifier 必须落到**与起草不同的 family**，
否则这条 lane 会永远停在 `not_independent`（这是**正确**的停法，但要有人知道为什么停）。
一条只有一个 family 的链做不到这件事。要分开限流就各写一份配置，
并把对应的 `LaneSpec` 指过去。

---

## 18. 改完之后再跑一次 install.sh

```sh
deploy/macos/install.sh
```

**plist 只在安装时渲染一次。** 凡是「因为某个文件出现 / 因为某份记录变成 approved 才存在」
的 lane，都要重装一次才会进 argv。至少这几件事之后要重装：

- 第 7.4 节批准大众源七份记录之后（那条线**按状态**开，不按文件存在开）
- 第 15 节拷了 `ir-pages.json` 之后
- 第 14(c) 节写了 `research-task-lane.json` 之后
- 第 17 节写了任何一个模型配置 / policy 文件之后
- 第 13 节把 `thesis_impact.enabled` 翻成 true 之后

脚本是幂等的，plist 按盘上现有的东西重新渲染。

---

## 19. 两个要你回答的问题

### 19.1 AlphaEngine 的日上限

live mission 的 `budget.max_alphaengine_calls_24h` 是 **130**（演练直接从 live 的
mission 第 13 版读出来的；**v1.0 说的 30 是 manifest 里的旧值，已经被抬过了**）。

而 W2 在 09-09 21:34 的心跳上量到 **`spent 133 / cap 130`——已经打满**。
ACN 的 9 份卖方研报、7 场电话会，DXC 的 17 份研报，全都卡在 `discovered` 上。
`get_document` 另有大约每会话 20 次的上限，一份多页研报要好几次调用——那才是「一份研报」的真实单价。

接上这一批之后抢这个额度的东西更多了：既有的搜索发现与研报获取；**新增**跟踪 lane 按 policy
基线每天拉 AlphaEngine 2 次 × 每家公司（覆盖薄的公司大脑会自己拉长到 2–3 天一次）；
**新增**价格异动触发后 6 小时内脱离基线立刻可拉。四家过闸公司 × 2 = 8 次/天只是跟踪那一半。

**P11b 把这个问题问得更具体了**：现行 130 篇/24h 下，五家里只有 **ACN（4 家独立券商）**
与 **EPAM（5 家）** 越过「两家独立券商」这条线，**CTSH / DXC / IBM 各只有 1 家**
（分别是 Wells Fargo、TD Cowen 两篇、RBC）。而且这三家的缺口**不是抽取率问题**
——单一发行人研报里凡有目标价的都抽出来了——是**素材里就只有那一家券商**。
spool 里 227 篇研报，只有 68 篇名下有覆盖池内的公司，其中 50 篇是单一发行人；
16 篇名下 5 家以上的行业回顾对这件事**零产出且必然零产出**，花在它们身上的配额是纯损耗。

**问题：要把 CTSH / DXC / IBM 也抬过「两家独立券商」这条线，配额该提到多少，
以及是否同意把检索明确偏向「单一发行人的公司更新」？**

三条路都合理：

- **抬上限**：`scripts/raise_day_budget_cap.py --config "$CONFIG" --cap-usd <N>`（不带 `--apply`
  是 dry run，会打印按默认分法四个池各是多少），然后发一版 mission。
- **偏向检索**：把配额从行业回顾挪到单一发行人更新，不动上限。
- **不抬**：把 `alphaengine` 的基线频率从 12 小时改成 24 小时——`$STATE/tracking-policy.json`
  里的一行，不用改代码、不用发版本，代价是研报晚半天进来。

驾驶舱「今天的钱花在哪四件事上」会告诉你哪一池先空。

### 19.2 抽取吞吐——现配置超了 mission 的调用上限

`service.json` 里 `document_extraction.max_windows_per_tick` 现在是 **30/10/10**。
30 + 10 + 10 = 50 次/tick × 288 tick/天 = **14,400**，而 mission 的
`max_daily_paid_calls` 是 **9,000**。只是因为队列一直没满才没炸。

W2 给了推导函数，没有改 `service.json`——那是部署动作。按日上限 $100、9,000 次付费调用、
288 个 tick 推出来的正确边界：

```
reservation floor (rate card): 3080 micros
reservation used  (route-estimate max × headroom): 10992 micros   (原来是一个平的 50000)
  passes=1 share=0.5: windows/tick=15  bound_by=paid_calls
  passes=3 share=0.5: windows/tick=5   bound_by=paid_calls
  passes=1 share=1.0: windows/tick=31  bound_by=paid_calls
```

**正确的边界比现配的 30 小，不是大。** 31（整条 lane 独占份额、只跑散文那一遍）
是 live 那个 30 唯一站得住的依据。要改就用 `DALTON_EXTRACTION_MAX_WINDOWS` 等三个变量
在第 4 节那一行上改。

顺带一件部署当天要盯的事：**`document_extraction` 的准入现在带池了**，
池空时在调用之前拒绝，lane 状态读作 `skipped:pool_exhausted`。
`unpooled_micros` 从此归零，coverage 上限第一次真正约束抽取。按 C2 的实测（当天 18.81 / 55 USD）
不会打爆，但这是 live 行为改变。

---

## 20. v1.0 说过、现在不对的每一条

| v1.0 | 现在 |
| --- | --- |
| §2 用 `dalton-coverage-mission` 发版 | **没有这个入口。** 用 `scripts/build_mission_v2_params.py` + `dalton-gov --operation create_coverage_mission`（第 8 节） |
| §2.1「加九个词」 | **十一个**：多了 `forecast_revision_proposal` 与 `conviction_call` |
| §2.3「checkpoints 加一个」 | **三个**：`thesis_revision_candidate`、`gate_reopen`、`conviction_call` |
| §2.2「`source_discovery` live 第 13 版没有」 | **有。** 演练直接读出来的 11 个词里就有它 |
| §2.4「`source_plan` 把这些改成 `connected`」 | F18 后可直接对缺行使用五个 `--set-source-status source:<name>=connected`，生成器按 connector inventory 补行（第 8.1 节）。`source:alphaengine` **已经**是 `connected`，不是 `probe_only` |
| §4.4 / §4.5 手写三份模型配置 | event judgement 两份、zero-base review 两份、claim index 一份都由对应 `DALTON_*_MODEL_PROFILE/TIER` 写出；两个 producer/verifier 对都有安装时成对检查，运行时再按实际 family fail-closed（第 4.1 节） |
| §6「上限是 30」 | **130**，而且 W2 量到已经打满（第 19.1 节） |
| §1 二十份记录 | **二十六份**：多了 S5 的四份持股 + 两份 IR 页面（第 7.5 / 7.6 节） |
| — | 新增：C2 日账本迁移（第 5 节）、W3 重跑 install 让 planner 进日账本（第 4.1 节）、IR 十条网址（第 15 节）、13F 名单（第 16 节）、回答充分性策略（第 10 节）、thesis-impact canary（第 13 节）、抽取吞吐（第 19.2 节） |
| — | `DALTON_PRIOR_RESEARCH_DIR` 已接入 install：声明既有研究根目录，种 prior-research 两份治理记录与 v2 feed plan，并建立受控软链（第 4.1 节） |

---

## 21. 回滚：能退代码，不能退批准

停服务（第 1 节）→ 用第 2 节的快照恢复 → 把旧 plist 放回去 → 重启：

```sh
"$VENV/bin/dalton-backup" verify --backup-root "$STATE/backups" \
  --snapshot-id "$SNAPSHOT" --restore-root /tmp/dalton-rollback-check
# 只有 verify 过了之后：
cp /tmp/dalton-rollback-check/*.sqlite "$STATE"/
cp "$DALTON_ROOT/config/service.pre-8717de0-"*.json "$CONFIG"
cd "$REPO" && git checkout <上一版 main> && deploy/macos/install.sh
```

**回滚退不掉的：mission 版本与每一条治理批准。两者都是设计上 append-only 的。**

- 想撤销一个授权，**发第 15 版把那个词去掉**，不要试图删第 14 版。
- 想撤销一条连接器批准，**发一份新的记录版本**，没有「取消批准」这个动作。
- 一条 `gate_reopen` 批准只买**一版**重出——重出之后许可就被消费掉了。

带着 v14 回滚代码是安全的（mission 可以授一个没有 lane 认领的词），
但 `git checkout` 到一个 `AUTOMATION_WRITE_SCOPES` 里没有 `conviction_call` 的提交，
会**拒绝读** v14，mission 整个 fail closed。**第 8 节之后再回滚，就发 v15 去掉那些词。**

---

## 22. 做完之后应该看到什么

打开驾驶舱：

- **公司卡**：股价与估值、今日事件（按种类，每条带证据层级）、大脑的判断
  （五个决定词之一 + 一句理由 + 真正落了什么 + 独立复核结论）、
  反思（我们当时以为 / 实际发生 / 可能漏掉的争论 / 想跟进什么）、
  下一个催化剂 T−N 天（vendor 猜的日期会写「日期未确认」）、我们多久看它一次、正在专项研究什么。
- **「系统此刻在做什么」**：31 条 lane，每条一个中文名和一个词的状态。
  仍写着「等你批准数据源」的，是第 7 节漏掉的那一条；写着「缺授权，一次也没跑」的，
  是第 8 节漏掉的那个词。**`ungranted` 必须看得见**——一条因为缺授权而永远沉默的 lane，
  看起来和一条健康的空闲 lane 一模一样。`gated:same_family` 同理。
- **「看每个来源能取什么」**：每条 connector 的内容 / 可信度 / 接没接上 / 剩多少额度 / 多久看一次；
  通用源单独标出。下半页是模型：每一层的链、上一次是链上第几个模型服务的、
  以及这台机器的模型目录和网关一不一致（不一致时两个方向的差集都列名字）。
- **「每周回头看」**：上一周「我们把时间花在哪」的散文与每池一行的表，加上下周的问题候选。
  一个用来校准直觉的数：上一个完整周（2026-W36）系统一共花了 **$0.026 / 19 次计价调用**，
  不到周预算上限的 0.1%。**预算不是瓶颈。**
- **待你裁决**：`thesis_revision_candidate` 与 `gate_reopen` 会带中文标题和它的反思一起列出来，
  但**没有按钮**——裁决那两个的入口在另一条分支上，还没接。列出来是为了让你知道它们在排队。
  今天要裁决只能走 writer op：`decide_thesis_revision_candidate`、`decide_gate_reopen`、
  `decide_conviction_call`、`decide_deep_insight_gate`，每一个都要一句会进正式记录的理由。

有几件事**做完也还是空的，而且那是诚实的**：

- **P15d 判断 lane 今天一条都提不出来**：五家全部在确定性预检就停住，理由
  `no_debate_map_and_no_consensus, no_variant_material`。授那两个词是对的、也便宜，
  但要等 P12c 在 live 上真发出一张争论图。
- **P12c 今天开不出任何一条 debate**：五家一共 6 个 variant seed、0 个 production seed，
  因为我们还分不出两份卖方报告是不是同一家券商写的。
- **IBM 的 consensus 会一直是 `fiscal_calendar_unknown`**：live 有 33 份 10-Q、0 份 10-K，
  财年末从季度网格推不出来。**等它第二、第三份 10-Q 入库就自己解决了**，不需要任何人做决定。
- **P14f 的第一份 preview 会很薄**：日历零行、`forecast_model_versions` 零行、
  claim 索引不在 live、没有 consensus 权威。四个缺口各自属于 C1 部署、P13-M2 跑一次、
  P12b 部署、P11b——不是 P14f 的缺陷。
