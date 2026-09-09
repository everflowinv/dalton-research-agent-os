# S 线 S3：大众源三条连接器（雪球、X、员工评价）v1.0

日期：2026-09-09
分支：`s3-crowd-sources`（基于 main `88c040b`，已 merge main `d07ed8d`），未 push
执行：Opus 5 subagent，worktree `~/Projects/dalton-s3-crowd-sources-worktree`
验收：见第 9 节（全量测试原文）
修订：v1.0 交付后按 code review 修了 B1 / B2 / S1 / S2 / S4 与四条 nit，`CROWD_IMPORTANCE`
按合并进 main 的 claim index 改口径，lane order 120 → 140（120 归 S1）。
**S1 的 `host_tool_runner` 合进 main 之后，三个 child 已经接上去了**——配额从「声明」变成「真的在数」，
lane 从「起了再等下一 tick 结算」变成「一次 tick 内跑完并落账」。见第 11、12 节。

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
| `credential-slot:xueqiu-cookie` | `xueqiu_cookie`（agent-reach 配置里） | `search_posts`、`get_post` **总是**；`hot_rank` **看走的哪条路**（见下） |
| `credential-slot:x-auth-token` / `credential-slot:x-ct0` | `TWITTER_AUTH_TOKEN` / `TWITTER_CT0`（xreach 自己的 store） | X 的全部三个 operation |
| （无） | — | Blind，auth boundary 就是 `none` |

**child 从不读 `~/.agent-reach`，也不知道它在哪。** 用的是协议里已经为此存在的对象：
`CredentialGrantEnvelope`。它按构造只装 ref、两个时间戳、槽列表和调用上限，装不下任何材料，
`from_dict` 会拒绝任何不是这个闭合形状的东西。child 拿到 `--credential-grant <path>`，
检查槽是否被覆盖、operation 是否被允许、target 是否一致、是否过期，然后**在 spawn 之前、取一个字节之前**
按名字拒绝。拒绝语句本身就是接口：coordinator 读它，人读它就知道该去绑哪一把。

**决定要不要凭证的是路线，不是 operation。** 第一版把豁免挂在 operation 上：`hot_rank` 被列为免凭证，
因为它的 fallback 免凭证。可是 `hot_rank` 有两条路，只有一条是 fallback。配了主 tool 时
（这就是正常配置，因为另外两个 operation 必须有主 tool），`hot_rank` 走的是 host 的雪球 channel，
那条路要 cookie，而当时**既不查槽也不查到期**。现在 child 先解析路线、再按解析出的
`provenance_label` 决定要不要 `require_slots`：`xueqiu_agent_reach_channel` 要，
`xueqiu_hot_stock_rank_fallback` 不要。两条路各有一个测试。

新模块 `crowd_credential_grants.py` 是计划外的第七个文件。三个 child 都要这段逻辑，塞进任何一个 core 里
都会让另外两个 import 它。它只新增、不改动别人的文件。

`redacted()` 是保险丝：host tool 的响应在进 wire 之前先剥掉 cookie / token 形状的顶层键。
**按整个键比对，不按子串。** 第一版按子串，而 `auth` 是 `author` 和 `author_id` 的子串——
`get_post` 返回的单帖顶层就是帖子本身，于是作者被静默删掉了。一个会悄悄删数据的过滤器
比没有过滤器更坏：什么都没报错，帖子从此匿名。现在是一份显式的 `CREDENTIAL_SHAPED_KEYS` 集合。

## 5. child 的顺序与冻结产物

`sec_financials_cli` 的次序原样照抄，每一步都是比下一步更便宜的拒绝：

1. **批准在先**——记录必须 approved，capability 必须对得上，`expected_source_hash` /
   `expected_schema_hash` 必须仍然描述打包的合同（漂移在网络调用前拒绝，不是中途发现）；
2. **凭证槽其次**（Blind 跳过这步，它没有槽）；
3. **产物永远有**——host tool 打印的原始字节先 sha256、先进 RawSpool，再读出一个字段；
   `source_record_refs` 指回 `raw-sink:<hash>`；
4. **合同最后**——归一化后的 wire 过 `_schema_matches` 冻结输出 schema，描述不了的观测直接拒绝。

失败不抛出，写 `summary.json`（`status: failed` + `failure_reason: "TypeName: message"`），退出码 1。
`--emit-wire` 会把校验过的 wire 作为**一份 JSON 打到 stdout**——这是给 host-tool runner 用的口子（见 7.2）；
**拒绝时也打一份** `{"status":"failed","failure_reason":...}`，因为只给退出码等于不给理由。

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

### 7.1 配额现在是真的在数了

v1.0 时这里写的是「配额只是声明」：child 自己 `subprocess` 调 host tool，中间没有 reservation、
没有 physical attempt、没有 settlement，所以没有任何东西在数那 50 次。

**S1 的 `host_tool_runner` 合进 main 之后，这条已经修好了。** runner 用
`governed_daily_quota(connector_slug, operation)` + `apply_governed_quota_to_limits(...)`
注册 rate policy，而 `connector_slug` 取的是 `template_key`——我的三个 template key
（`xueqiu-posts` / `x-xreach-crowd` / `employee-reviews`）与我的配额键**逐字相同**，
所以配额表一个字没改就生效了。有一条测试专门钉这两串字符串必须相等，因为它们一旦分叉，
「声明了 50」和「执行 50」就会静默地变成两件事。

### 7.2 接上去之后长什么样（已完成）

- **`CrowdSourceExecution`**（`mission_crowd_source_lane.py`）：一个 launcher 管 ticket，
  一个 `HostToolRunner` 管进程与权威链。这是 S1 的拆法，也是对的——ticket 目录是「哪一次运行
  产出了哪些字节」的持久记录，而 runner 负责 call spec / invocation / reservation /
  physical attempt / raw artifact / SourceEnvelope。**仍然只有一个 child 进程。**
- **`child_command(operation=..., output_dir=..., context=..., **parameters)`**：与 spawn 用的是
  同一个 argv 构造器，末尾加 `--emit-wire`。runner 把 stdout 当原始响应，所以 child 只在 stdout
  打一份闭合 wire。
- **`prepare_run` / `settle_run` / `run_digest`** 加在 `CrowdSourceLauncher` 上（照 `feed_launcher`
  的样子），digest 与 spawn 路径同源，所以同一个请求还是同一个 ticket。
- **`build_crowd_source_runner(...)`** 把 template key、identity、argv 三样喂给通用 runner；
  `source_identity()` 直接用三个 `*_core` 模块本来就在产的 identity（`capability_id` /
  `source_hash` / `schema_hash` / `adapter_ref` / `operation`），一个字段都没有为此新增。
- **coordinator 变成同步的。** 原来是「这一 tick 起 child，下一 tick 结算」；现在是一次 tick 内
  跑完并落账。收回来的是一整类状态：没有 `_open`、没有孤儿 ticket、没有「起了但再也没被结算」的洞。
  receipt 回不来就是这次读没发生。接缝也随之收窄——coordinator 只问每个条目两件事：
  `SOURCE_REF` 和 `execute(operation=..., parameters=..., actor_ref=..., company_ref=...)`，
  测试里的 fake 也只实现这两样。
- **凭证模型两条并存，是有意的。** 我的 child 收 `CredentialGrantEnvelope`（只有 ref 与到期，
  看不见值）；runner 是由 writer 解析槽名、把值作为环境变量注进 child，且 child 拿不到
  `os.environ`，只拿到 profile 声明的那几个槽。前者是本地那道便宜的闸（在 spawn 前按名字拒绝），
  后者是让工具真的能跑。identity 里的 `credential_slot_refs` 直接喂给 runner 的
  `credential_slot_refs`。
- **重复的工作是幂等的，没有拆。** runner 自己也做批准与哈希校验、也 spool、也校 schema；child
  同样做。spool 是内容寻址的，同 hash 命中已存在对象，所以不会写两份。child 里那份留着，
  因为 child 是可以被人手工跑的，而手工跑的人也该被拒绝。
- **踩到一个真 bug**：`_validate` 现在会跑两次（coordinator 建 job 时一次，runner 要 argv 时一次，
  第二次跑在自己第一次的输出上）。空的 `since` 第一次输出成 `""`，第二次被当成「格式错的日期」
  拒掉——第一次刚接受的 job 第二次被拒。校验必须幂等，加了一条测试钉住。

### 7.3 其余

1. **claim index：不需要加任何一行。** `claim_index_tagging` 先按 discovery spec 取 importance，
   取不到再按 connector 的 `source_type` 取，都取不到落到默认 `other`——而 `other` 就是
   `IMPORTANCE_TIERS` 的最后一档。三个 crowd spec ref 不在 `SPEC_IMPORTANCE` 里，
   `social_search` / `social_enumeration` 不在 `SOURCE_TYPE_IMPORTANCE` 里，所以大众源**天然排在最底**。
   `CROWD_IMPORTANCE` 已改为 `"other"`（原来写的 `"background"` 不在这套词表里）。
   **往这两张表里加条目才是把它抬上去**，所以测试断言的是「这些不在表里」而不是「在表里」。
   `document_figure_grade.GRADE_BY_SPEC` 同理，不要加。
2. **install.sh 种子**：七条治理记录拷进 `<state>/connector-governance/`，映射文件拷进 `<state>/phase9/`。
   还要给 writer 传 host tool 路径：`--crowd-source-xueqiu-tool`、`--crowd-source-xueqiu-fallback-tool`、
   `--crowd-source-xreach-tool`、`--crowd-source-credential-grant`。
   **雪球那条需要一个 shim**：host 侧的读取脚本是一个 `.py`，我的 argv 契约是
   `<tool> search <query> --pages N --json` / `<tool> post <id> --json` / `<tool> hot-rank --limit N --stock-type T --json`，
   前两个正是 host 现有脚本的子命令，`hot-rank` 需要 shim 补（或者只配 `--fallback-tool`）。
   `xreach` 不需要 shim，`--json tweets|search|thread` 就是它自己的子命令。
3. **mission 新版本**（owner 发）：
   - `autonomy.may_write` 要加 **`source_discovery`**（现在只有 `observation`，lane 会 `gated`）；
   - `source_plan` 要新增三条并置 `connected`：`source:xueqiu`、`source:x`、`source:blind`
     （现在这三个 source ref 在 mission 里根本不存在，lane 会对三条都报 `held`）；
   - 角色描述建议写明「只作趋势与情绪，不作任何定量 Claim 的来源」。
4. **cockpit**：没做。要展示的话，最小有用面是「每家公司近 30 天的 Blind 六维评分序列 + 被锁行数」。
5. **owner 批准**：七条 `proposed` 记录，`connector_governance_cli approve` 逐条批。
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
Ran 2908 tests in 322.826s
OK (skipped=1)
```

（基线 main `d07ed8d` 为 2,798；本片新增 110 项。
另外 `PYTHONPATH=src .venv/bin/python scripts/build_connector_inventory.py --check` 输出
`packaged connector inventory matches the frozen definitions`。）

新增测试覆盖：批准拒绝（proposed / 错的 capability / 漂移的 schema hash）、
未绑槽拒绝且理由带槽名（缺 grant、缺其中一把、operation 不在 grant 里、target 不对、过期）、
原始字节哈希与 `raw-sink:` 回指、schema 拒绝（无 id / 无时间戳的帖子）、
去重（同一帖两次、重启后仍认得）、body_locked（评分留、正文置 null、占位文本不进 observation）、
grade 映射（三个 crowd spec ref 在 `GRADE_BY_SPEC` 里都不存在、`require_grade` 抛错、
`admissible_as_sole_quantitative_source(CROWD_GRADE)` 为 False）、
lane 门控（缺 grant / 缺 mission / 源未 connected / 每源每 tick 一个 child / 失败后换公司 /
冷却到期后重试 / 治理哈希一变立刻清空 / tick 报 `held`）、
lane 登记（在 tick 末位、空 state 目录下 argv 为空、**`proposed` 记录不开 lane**）、
路线凭证（fallback 路免凭证、主路缺 grant 被拒、主路带 grant 通过）、
整键脱敏（单帖顶层的 `author` / `author_id` 原样留下）、
非 ASCII 评论（重音与中文原样通过 payload 反转义）、
按位置锁体（首页之后的行 `body_locked` 为真、评分仍在）。

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


## 11. code review 之后改了什么（v1.0 → 交付版）

| 项 | 改了什么 |
| --- | --- |
| **B1** | worktree 的 `.venv` 是指向主 checkout 的**符号链接**，而 `.gitignore` 里写的是 `.venv/`——带斜杠的模式只匹配目录，所以这条机器相关的路径被提交了。`git rm --cached .venv`，并补上不带斜杠的一行。 |
| **B2** | `hot_rank` 的凭证豁免原来挂在 operation 上，实际决定要不要 cookie 的是**路线**。改成先解析路线、再按 `provenance_label` 判定；两条路各一个测试（fallback 路免凭证并通过、主路缺 grant 被按槽名拒绝、主路带 grant 通过）。 |
| **S1** | `redacted()` 改为整键比对（`CREDENTIAL_SHAPED_KEYS`），并加了一个直接喂 `get_post` 顶层帖子形状的测试，断言 `author` / `author_id` 原样留下。 |
| **S2** | `argv_fragment` 原来只看文件在不在，于是**对 `proposed` 记录也开 lane**；现在读 `status == "approved"`。`_failed` 原来永不清空，一轮下来 lane 就永远 `idle` 了；现在是 `FAILURE_COOL_OFF_TICKS = 12` 的冷却，且**治理哈希一变就立刻清空**（owner 批准了就是换了个记录）。tick 结果新增 `held` 字段，因为「闲着」和「全被扣住」从外面看是同一个词。 |
| **S3** | 见 7.1 / 7.2：配额今天只是声明，接 S1 的 `HostToolRunner` 才会真的计数；接线清单已写。 |
| **S4** | 锁体检测原来只看 `pros`，且无条件保留 `summary`。现在 (a) `pros` / `cons` / `summary` **任一**命中占位文本即算锁，(b) **超过首页（`PAGE_SIZE`）按位置算锁**——这半条在 Blind 换 lorem ipsum 文案之后仍然成立，(c) 锁行的 `summary` 也置 null。 |
| nit | `--emit-wire` 拒绝时也打一份带理由的 JSON；Blind 的 16 MB 上限改为**整次读的总预算**而不是每页各 16 MB；flight payload 的反转义从 `unicode_escape`（经 latin-1，静默糟蹋非 ASCII）改成 `json.loads(f'"{chunk}"')`，并加了一个带重音与中文的 fixture 测试；`credential_grant()` fixture 的 `grant_kind` 加了注释，作为下面的 owner 问题。 |
| 合并 | `tests/test_lane_registry.py` 上的改动**整个撤回**——main 把那些断言改成了子集检查，并写明「P14-0 之后新增的 lane 不该出现在这些字面量里」，所以我的 lane 只在自己模块的测试里钉。`tests/test_connector_quota_policy.py` 按合并后的排序重建了字面量。 |

**新的 owner 问题**：`CredentialGrantEnvelope.grant_kind` 的词表是
`{mcp_managed, https_credential}`，**没有一个词描述 host tool**。我暂时用 `mcp_managed`
（live gate 对所有非公开 transport 都要求这个词），但这是个占位。要么加一个 `host_tool` grant kind，
要么明确 `mcp_managed` 就是「host 拥有的一切」的意思——两者都要改 `credential_authority.py`，
不在我的范围内。


## 12. 接上 host_tool_runner 之后改了什么（第二轮 review）

| 项 | 改了什么 |
| --- | --- |
| 合并 | main `d07ed8d`（S1 feeds + `host_tool_runner`、P14e research task）。六个文件冲突，全是「两边各自 append」：`connector_governance.py` / `connector_inventory.py` / `LANE_MODULES` 保留两侧；`index.json` 与打包文件**用 `scripts/build_connector_inventory.py` 再生**，没有手工合过；两份测试的字面量按生成器与配额表的排序合进去（`company-wiki` 排在 `employee-reviews` 前，`x-xreach-crowd` / `xueqiu-posts` 排在 `web-fetch` 后、`yfinance` 前）。 |
| lane order | 140（120 / 130 是 S1 的两条 feed，150 是 P14e 的 research task）。tick 末位断言改成「所有**证据类** lane 的末位」——research task 排在我后面，但它不是证据源。 |
| 执行模型 | 从异步（起 child → 下一 tick 结算）改成同步（一次 tick 内跑完并落账），见 7.2。`_open` / `_settle` / `LaneChildConflict` / 孤儿 ticket 处理整块删掉——那些状态在同步模型里不存在。 |
| 配额 | 从声明变成执行，见 7.1。新增测试钉 `template_key` 与配额键逐字相等。 |
| 幂等校验 | `_since("")` 原来抛「格式错的日期」，导致二次校验拒掉自己刚接受的 job。空即无。 |
| 新测试 | `ExecutionTests` 七条：argv 末尾是 `--emit-wire`、ticket 开了又结、receipt 变成 ledger 读得懂的形状、未批准的记录**根本到不了 runner**、runner 抛异常时 ticket 结成 failed、三条源七个 operation 的 identity 都能绑、配额键对得上。 |
