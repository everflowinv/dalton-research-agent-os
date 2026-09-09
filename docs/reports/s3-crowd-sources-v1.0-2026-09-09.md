# S 线 S3：大众源三条连接器（雪球、X、员工评价）v1.0

日期：2026-09-09
分支：`s3-crowd-sources`（基于 main `88c040b`，已 cherry-pick `99f6a9b`、已 merge main `888a814`），未 push
执行：Opus 5 subagent，worktree `~/Projects/dalton-s3-crowd-sources-worktree`
验收：见第 9 节（全量测试原文）

---

## 1. 一句话

三条大众源接上了：雪球帖子、X（走 `xreach`）、Blind 员工评价。**都是新连接器，不是把 shadow 模板改宽**——
`xueqiu` 和 `x-xreach` 的 2026-08-14 哈希是 owner 已经看过的，就地加 operation 会让那个哈希在别的批准底下移动。
所以走 `sec` / `sec-financials` 的形状：同一个 source ref，两个 connector，各自的合同与批准。

三条源产出的东西**永远不能是一个数字**。这一条不是靠新加一个 grade 实现的，而是靠**缺席**：
`document_figure_grade.GRADE_BY_SPEC` 里没有的文档类型就是不能取数的文档类型，测试把这个缺席钉住了。

## 2. 改了什么（文件清单）

**新增（我的）**
- `src/dalton_core/xueqiu_core.py` / `xueqiu_cli.py`
- `src/dalton_core/xreach_core.py` / `xreach_cli.py`
- `src/dalton_core/employee_reviews_core.py` / `employee_reviews_cli.py`
- `src/dalton_core/crowd_credential_grants.py`（**计划外的第七个模块**，见第 4 节）
- `src/dalton_core/crowd_source_launcher.py`
- `src/dalton_core/mission_crowd_source_lane.py`
- `deploy/connector-governance/{xueqiu-search-posts,xueqiu-get-post,xueqiu-hot-rank,x-xreach-user-timeline,x-xreach-search,x-xreach-thread,employee-reviews-blind}-v1.json`（七条，全部 `proposed`）
- `deploy/phase9/p9-us-it-services-crowd-sources-v1.json`（每 mission 的 handle / 查询词 / 雇主 slug 映射）
- `tests/crowd_fixtures.py`、`tests/test_crowd_source_connectors.py`、`tests/test_crowd_source_children.py`、`tests/test_crowd_source_lane.py`
- `docs/reports/s3-crowd-sources-v1.0-2026-09-09.md`（本文）

**追加（共享文件，块自包含）**
- `connector_inventory.py`：`PROFILE_DEFINITIONS` 三条新定义；`_output_schema` 四个新分支加两个小构造器（`_crowd_post_schema` / `_crowd_rating` / `_crowd_counter`）
- `connector_inventory/{index.json,profiles,fixtures,proposals}`：**用 `scripts/build_connector_inventory.py` 再生，没有手写一个哈希**。`--check` 干净；`index.json` 里只有我这三条的哈希动了
- `connector_governance.py`：七个 `_KindSpec`、对应的惰性哈希闭包、`build_governance_record` 三个分支、`XUEQIU_KINDS` / `XREACH_KINDS` / `EMPLOYEE_REVIEWS_KIND`
- `connector_quota_policy.py`：七条日配额
- `lane_registry.py`：`LANE_MODULES` 加一行
- `tests/test_connector_inventory.py`、`tests/test_connector_quota_policy.py`、`tests/test_lane_registry.py`：这三份都有「恰好是这些」的字面量断言，加了我的条目（与其它 Wave 1 分支会在同一处冲突，合并时取任一侧再跑 `build_connector_inventory.py` 即可）

**没碰**：`writer_server.py`、`coverage_mission.py`、`bounded_planner_driver.py`、`macos_launchagent.py`、
`deploy/macos/install.sh`、`cockpit_*`、`pyproject.toml`、`docs/PROJECT_STATUS.md`、`tests/test_service.py`。

## 3. 三条连接器的合同

| slug | connector_ref | transport / target | auth | operation（source_method） | completeness | 配额 |
| --- | --- | --- | --- | --- | --- | --- |
| `xueqiu-posts` | `connector:xueqiu-posts` | `host_tool` → `host-tool:agent-reach-xueqiu-channel` | host_owned | `search_posts`(search_posts) / `get_post`(get_post) / `hot_rank`(**get_hot_stocks**) | ranked / enumerated / ranked | 50 单位/日，各 5 / 1 / 1 次物理调用 |
| `x-xreach-crowd` | `connector:x-xreach-crowd` | `host_tool` → `host-tool:xreach` | host_owned | `user_timeline`(**tweets**) / `search`(search) / `thread`(thread) | enumerated / **ranked** / enumerated | 50 单位/日，各 5 次 |
| `employee-reviews` | `connector:employee-reviews-blind` | `public_https` → `transport:public-http:0.1`，host allowlist `www.teamblind.com` | **none** | `blind_reviews`(blind_reviews) | **partial** | 50 单位/日 × 20 页 |

几处值得单独说的：

- **`source_ref` 与 shadow 模板共用**（`source:xueqiu`、`source:x`），connector_ref 不共用。协议里 `source_method`
  本来就允许和 Dalton 的 operation 名不同，`hot_rank`→`get_hot_stocks`、`user_timeline`→`tweets` 用的正是这条。
- **`hot_rank` 的 fallback 原样继承**：`host-tool:cn-hk-findata-xq-hot-rank`，`provenance_label` 仍是
  `xueqiu_hot_stock_rank_fallback`，且**只绑这一个 operation**。它不是雪球帖子正文，输出 wire 里带 label。
- **forbidden route**：雪球加了 `route:xueqiu-web-front-end`（主站在 WAF 挑战页后面，只拿得到壳）；
  X 保留 `route:last30days-x` 并加 `route:agent-reach-twitter`；Blind 写了
  `route:firecrawl-indeed` / `route:firecrawl-glassdoor` / `route:credential-channel`。
- **`x_search` 明确没做**，`xreach_core.NOT_BUILT_OPERATIONS` 把这件事写进了代码而不是只写在报告里。
  理由是 owner 已经知道的那条：`x_search` 合成、不可枚举，不能用来证明「没有」。
- **`search` 的 completeness 恒为 `ranked`**，无论 cursor 说什么；timeline / thread 翻到底才是 `enumerated`，
  还有 cursor 时是 `partial`。这个词写在 wire 上，后面的读者没法悄悄升级它。
- **Blind 的 ceiling 是 `partial` 不是 `enumerated`**：库可以翻到底，但第 31 条起正文被占位文本替换，
  一个正文被替换过的响应就是截断响应。

**输出 wire 不是只有 record refs。** 雪球 / X 的 shadow 模板用的是最小三字段输出（只有
`source_record_refs` / `next_cursor` / `provider_status`）。我给三条新连接器写了各自的闭合输出 schema，
因为 lane 要把帖子本身变成证据；只校验 refs 等于把会变成证据的那部分放过去。

## 4. 凭证槽：child 怎么知道槽绑了，而不去读它

| 逻辑槽 ref | host 自己的名字 | 谁需要 |
| --- | --- | --- |
| `credential-slot:xueqiu-cookie` | `xueqiu_cookie`（agent-reach 配置里） | `search_posts`、`get_post`（`hot_rank` **不需要**，它的 fallback 路线免凭证） |
| `credential-slot:x-auth-token` / `credential-slot:x-ct0` | `TWITTER_AUTH_TOKEN` / `TWITTER_CT0`（xreach 自己的 store） | X 的全部三个 operation |
| （无） | — | Blind，auth boundary 就是 `none` |

**child 从不读 `~/.agent-reach`，也不知道它在哪。** 用的是协议里已经为此存在的对象：
`CredentialGrantEnvelope`。它按构造只装 ref、两个时间戳、槽列表和调用上限，装不下任何材料，
`from_dict` 会拒绝任何不是这个闭合形状的东西。child 拿到 `--credential-grant <path>`，
检查槽是否被覆盖、operation 是否被允许、target 是否一致、是否过期，然后**在 spawn 之前、取一个字节之前**
按名字拒绝。拒绝语句本身就是接口：coordinator 读它，人读它就知道该去绑哪一把。

新模块 `crowd_credential_grants.py` 是计划外的第七个文件。三个 child 都要这段逻辑，塞进任何一个 core 里
都会让另外两个 import 它。它只新增、不改动别人的文件。

`redacted()` 是保险丝：host tool 的响应在进 wire 之前先剥掉任何 cookie / token 形状的顶层键。
这些工具现在不回显 cookie，但一个开始回显的工具不该能借这条 lane 把它写进 spool。

## 5. child 的顺序与冻结产物

`sec_financials_cli` 的次序原样照抄，每一步都是比下一步更便宜的拒绝：

1. **批准在先**——记录必须 approved，capability 必须对得上，`expected_source_hash` /
   `expected_schema_hash` 必须仍然描述打包的合同（漂移在网络调用前拒绝，不是中途发现）；
2. **凭证槽其次**（Blind 跳过这步，它没有槽）；
3. **产物永远有**——host tool 打印的原始字节先 sha256、先进 RawSpool，再读出一个字段；
   `source_record_refs` 指回 `raw-sink:<hash>`；
4. **合同最后**——归一化后的 wire 过 `_schema_matches` 冻结输出 schema，描述不了的观测直接拒绝。

失败不抛出，写 `summary.json`（`status: failed` + `failure_reason: "TypeName: message"`），退出码 1。
`--emit-wire` 会把校验过的 wire 作为**一份 JSON 打到 stdout**——这是给 host-tool runner 用的口子（见第 7 节）。

**smoke 时发现的一个真 bug**：`lane_child_launcher.write_owner_only` 不建父目录（`sec_financials_cli`
自带的那份会建）。launcher 路径下 ticket 目录总是先存在，所以从没被踩到；手工用 `--summary-dir`
指向一个不存在的目录时，child 崩在「写拒绝理由」那一步，于是没有任何理由被写下来。
三个 child 现在自己 `mkdir` summary 目录。共享文件没动。

## 6. lane、映射文件与 LaneSpec

- **coordinator**：`MissionCrowdSourceLaneCoordinator`。每 tick 先结算、再每条源最多起一个 child，
  轮转 mission 的五家公司；`observation` + `source_discovery` 两个 grant 缺一即 `gated`；
  mission `source_plan` 里没有 `connected` 的源报 `held`。
- **去重**：`CrowdObservationLedger`，append-only、0600、按 `(source_ref, record_id)` 去重，重启后仍然认得。
  它**不是 authority**，没有版本链，只是这条 lane 自己的记忆；集成时接到真正的证据 authority 上之后它就该消失。
- **LaneSpec 已登记**（Wave 0 合并后补的）：`dispatch_mission_crowd_sources`，`order=120`（**排在最后**，
  大众源是最弱的证据，tick 要是没时间了应该在这里没时间，不是在 filing 之前），
  `driver_key="mission_crowd_sources"`，`init_kwarg="crowd_source_launcher"`，
  `lane_registry.LANE_MODULES` 加了一行 `"dalton_core.mission_crowd_source_lane"`。
- **一条 lane 只有一个 init_kwarg，我有三个 launcher**，所以 `CrowdSourceLaunchers` 把三个装成一个对象，
  writer 存它、关它。
- **默认关**：`argv_fragment` 要求 `deploy/phase9` 的映射文件和至少一条治理记录同时在 state 目录里才返回参数。
  七条记录全是 `proposed`，所以在 owner 批准之前任何一台 Core 上这条 lane 都是关的，
  `test_service` 的 launchagent argv 断言不受影响。
- **映射文件** `p9-us-it-services-crowd-sources-v1.json`：ACN / CTSH / EPAM / IBM / DXC，每家给
  `xueqiu_query`、`x_handles`、`employer_slug`。字段缺省表示「这条源对这家公司没有东西」，
  而不是「猜一个」——猜错的后果是把别家公司的议论堆进这家公司的档案。
  **首字母缩写型公司（IBM、DXC）的 Blind slug 最可能是错的，先核这两个。**

## 7. 集成时要接的线

1. **host_tool runner（S1 在做）**。仓库今天没有 host_tool runner，所以我的 child 自己 `subprocess`
   调用 host tool。接口已经按 runner 的口径留好了：child 认 `--emit-wire`（一份 JSON 打 stdout）、
   summary.json 写在 ticket 目录、原始 stdout 已经进 spool、输出已经过冻结 schema。
   coordinator 侧的接缝叫 `runners`（构造参数），它只要求每个条目有
   `SOURCE_REF` / `start(operation=..., actor_ref=..., **params)` / `status(ticket_ref)` 三样；
   今天传的是 `crowd_source_launcher` 里的 launcher，测试里传的是 fake。
   **S1 的 runner 落地后，换掉的是 `CrowdSourceLaunchers` 里装的东西，coordinator 一行不用改。**
   我没有 cherry-pick S1 的分支（交付时它还没落）。
2. **claim index 的一行**（Agent B）：`mission_crowd_source_lane.CROWD_GRADE`
   （`"crowd-anonymous-post"`）映射到最低 importance，本模块给的词是 `CROWD_IMPORTANCE = "background"`。
   在这行存在之前，claim index 根本不认识这个 grade，所以它排不到任何东西之上——失败方向是安全的。
   **`document_figure_grade.GRADE_BY_SPEC` 不要加条目**：那里没有条目正是「不能取数」的实现方式。
3. **install.sh 种子**：七条治理记录拷进 `<state>/connector-governance/`，映射文件拷进 `<state>/phase9/`。
   还要给 writer 传 host tool 路径：`--crowd-source-xueqiu-tool`、`--crowd-source-xueqiu-fallback-tool`、
   `--crowd-source-xreach-tool`、`--crowd-source-credential-grant`。
   **雪球那条需要一个 shim**：host 侧的读取脚本是一个 `.py`，我的 argv 契约是
   `<tool> search <query> --pages N --json` / `<tool> post <id> --json` / `<tool> hot-rank --limit N --stock-type T --json`，
   前两个正是 host 现有脚本的子命令，`hot-rank` 需要 shim 补（或者只配 `--fallback-tool`）。
   `xreach` 不需要 shim，`--json tweets|search|thread` 就是它自己的子命令。
4. **mission 新版本**（owner 发）：
   - `autonomy.may_write` 要加 **`source_discovery`**（现在只有 `observation`，lane 会 `gated`）；
   - `source_plan` 要新增三条并置 `connected`：`source:xueqiu`、`source:x`、`source:blind`
     （现在这三个 source ref 在 mission 里根本不存在，lane 会对三条都报 `held`）；
   - 角色描述建议写明「只作趋势与情绪，不作任何定量 Claim 的来源」。
5. **cockpit**：没做。要展示的话，最小有用面是「每家公司近 30 天的 Blind 六维评分序列 + 被锁行数」。
6. **owner 批准**：七条 `proposed` 记录，`connector_governance_cli approve` 逐条批。
   批准是按 operation 的：批了雪球搜帖不等于批了读帖。

## 8. smoke（只读，各一次，临时 state 目录）

三条都跑了。**为了跑通 transport，在临时目录里造了一次性的 approved 记录**；
仓库里的七条和 live state 里的任何东西都没有被批准，也没有被写。

| 源 | 结果 |
| --- | --- |
| **Blind / Accenture** | `succeeded`，2 页 60 条，库内 **1,336** 条；**30 条 body_locked**，被锁行的六维评分与日期都是真的（抽样 overall `4 / 3 / 1 / 3 / 3`）；产物 1,156,434 字节，hash `6ba318dcca6381fd…`；**observation 里搜不到 "lorem"**。 |
| **雪球 / 「埃森哲」** | `succeeded`，1 页 4 帖，`provenance_label=xueqiu_agent_reach_channel`；产物 15,122 字节，hash `d9684f4b029fd2cb…`；最新一帖 2026-09-09 18:20，正文 1,640 字。cookie 槽由 host 的 agent-reach 配置持有，我只传了一个 grant envelope。 |
| **X / @Accenture** | 传输通了（认证通过、退出 0、返回 JSON），但 `xreach --json tweets Accenture` 返回 **`{"items": [], "hasMore": false}`**——空时间线。第一版归一化不认 `items` 这个信封键，于是报了「没有帖子列表」；已按实测修正（`items` 优先，`hasMore` 参与 completeness），fixture 也改成真实信封。**帖子归一化路径因此只有 fixture 验证过，没有 live 数据验证过**，这是这次交付里最该复核的一处。 |

## 9. 验收

全量：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`

```
Ran 2172 tests in 290.014s
OK (skipped=1)
```

（基线 Wave 0 合并后为 2,080；本片新增 92 项。这次运行在第 8 节的 `items` 修正之后。
另外 `PYTHONPATH=src .venv/bin/python scripts/build_connector_inventory.py --check` 输出
`packaged connector inventory matches the frozen definitions`。）

新增测试覆盖：批准拒绝（proposed / 错的 capability / 漂移的 schema hash）、
未绑槽拒绝且理由带槽名（缺 grant、缺其中一把、operation 不在 grant 里、target 不对、过期）、
原始字节哈希与 `raw-sink:` 回指、schema 拒绝（无 id / 无时间戳的帖子）、
去重（同一帖两次、重启后仍认得）、body_locked（评分留、正文置 null、占位文本不进 observation）、
grade 映射（三个 crowd spec ref 在 `GRADE_BY_SPEC` 里都不存在、`require_grade` 抛错、
`admissible_as_sole_quantitative_source(CROWD_GRADE)` 为 False）、
lane 门控（缺 grant / 缺 mission / 源未 connected / 每源每 tick 一个 child / 失败后换公司）、
lane 登记（在 tick 末位、空 state 目录下 argv 为空）。

## 10. 开放问题

1. **Indeed / Glassdoor 等 Firecrawl 额度**（当前 −3，约 09-26 恢复）。做了就是给
   `employee-reviews` 发新 profile 版本换 transport，要 owner 重新批准。现在两条路线在 forbidden list 里。
2. **`x_search` 有意不做**。要做的话它需要一个能看见 gateway 工具的新 host bridge（新 ref / 新 hash，
   不能复用 AlphaEngine 的），而且 completeness 上限只能是 `ranked`。
3. **@Accenture 时间线为空**是账号本身没有可见推文，还是 xreach 的 GraphQL query id 该刷新？
   一次调用分不出来。建议集成时先手工 `xreach query-ids` 看一眼，再决定 X 这条是否值得开。
4. **匿名员工评价的证据层级**已按 owner 方向定为「匿名，只作趋势」，落地成
   `CROWD_GRADE` + `CROWD_IMPORTANCE=background` + 「不能单独支撑定量 Claim」三条。
   要不要再细分（Blind 匿名 vs X 具名公司账号）留给 owner；现在三条源共用一个 grade，
   而 X 的公司官方账号其实比匿名帖强一档。
5. **`CrowdObservationLedger` 是临时的**。它现在是 JSONL，因为我不能改 `coverage_mission.py`
   加 dispatch 表。接到真正的证据写入口之后应该整个删掉。
6. **`hot_rank` 现在没有被 coordinator 调度**——lane 每 tick 每家公司只问「关于这家公司在说什么」，
   热榜是全市场的，不属于任何一家公司。它的 child 与治理记录都建好了，等一个市场层的调用方。
