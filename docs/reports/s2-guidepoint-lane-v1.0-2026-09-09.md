# S2：Guidepoint 专家访谈 lane v1.0

日期：2026-09-09
分支：`s2-guidepoint-lane`（worktree `~/Projects/dalton-s2-guidepoint-lane-worktree`），
基线 main `88c040b`，交付前已合并 Wave 0 后的 main `888a814`
范围：并行开发计划 v1.0 第 3 节 S 线 S2；PROJECT_STATUS 下一步 #10

---

## 0. 一句话

身份与两条治理记录 P13ae 已经批过，缺的是"花掉一次调用"的那一段：本片补上 search 子进程、
discovery plan、launcher、协调器、摘录级 acquisition 与 ≤20 词逐字引用的合同级拦截；
`get_transcript` 上游没有对应物，出了一条**新的**收窄提案，v1 一个字节没动。
lane 已按 Wave 0 的 registry 自行登记（`LANE_MODULES` 一行，`writer_server` / driver /
launchagent 一行没动）。上线还差的只有 `coverage_mission.DISCOVERY_SOURCES` 一条词表、
install.sh 的 plan 种子、以及 mission 版本把 `source:guidepoint` 置 connected。

---

## 1. 交付的文件

| 文件 | 是什么 |
| --- | --- |
| `src/dalton_core/guidepoint_search.py` | 治理收窄类、search spec、host bridge 适配器、摘录规范化与稳定 id、`quote_policy` 与 `verify_guidepoint_quote`、`GuidepointCoreSearch` 执行器、`get_transcript` 收窄提案构造器 |
| `src/dalton_core/guidepoint_acquisition.py` | 摘录级 acquisition manifest、`acquire_guidepoint_excerpt`、`verified_guidepoint_source` |
| `src/dalton_core/guidepoint_cli.py` | 子进程 CLI：`search` 与 `acquire` |
| `src/dalton_core/guidepoint_launcher.py` | `GuidepointSearchLauncher(LaneChildLauncher)`（P13aj 的共享 ticket 机制） |
| `src/dalton_core/mission_guidepoint_lane.py` | discovery plan 校验与编译、cadence、预算、`GuidepointLaneCoordinator`，以及文件末尾的 `LaneSpec` 登记（`dispatch_guidepoint_discovery`，order 35） |
| `deploy/phase9/p9-us-it-services-guidepoint-v1.json` | 5 家 × 2 条公司 spec + 4 条行业 spec = 14 条查询 |
| `deploy/connector-governance/guidepoint-get-transcript-narrowing-v1.json` | `not_available_upstream` 收窄提案（status `proposed`） |
| `src/dalton_core/connector_quota_policy.py` | 追加 `("guidepoint","search_library")` 日配额（纯追加） |
| `src/dalton_core/lane_registry.py` | `LANE_MODULES` 加 `"dalton_core.mission_guidepoint_lane"` 一行 |
| `tests/test_lane_registry.py`、`tests/test_connector_quota_policy.py` | Wave 0 与配额的字面量断言按新增 lane / 新增配额同步（纯追加，四处） |
| `tests/test_guidepoint_search_lane.py`、`tests/test_guidepoint_acquisition.py`、`tests/test_guidepoint_lane.py`、`tests/fixtures/guidepoint_search_library_synthetic.json` | 45 项离线测试与合成 fixture |

**没有碰**：`writer_server.py`、`coverage_mission.py`、`coverage_mission_schema.sql`、
`bounded_planner_driver.py`、`macos_launchagent.py`、`deploy/macos/install.sh`、`cockpit_*`、
`pyproject.toml`、`docs/PROJECT_STATUS.md`、`tests/test_service.py`、`connector_inventory.py`、
`connector_inventory/index.json`、`connector_governance.py`、`guidepoint_core.py`、
两条已批治理记录 json。

---

## 2. 三个设计决定

### 2.1 被发现的"文档"是摘录，不是访谈稿

上游只返回排序后的问答摘录加一个回 Guidepoint360 的链接，没有读全文的 op。所以发现的单位是摘录，
ref 是 `guidepoint-excerpt:sha256:<hash>`，hash 取自**能识别一段话**的四个字段：
`transcript_name`、`date`、`respondent`（只取姓名，不含 title/company）、问题原文的 sha256。
两次不同的查询命中同一段话必须得到同一个 ref，否则同一条专家回答会按"找到它的查询数"重复入账。
（`test_the_same_passage_from_two_queries_is_one_document`）

### 2.2 没有 cursor

冻结的 inventory contract 声明 cursor 分页，因为那是模板的形状；上游实际只有 `size`，一页排序结果。
非空 cursor 在 spec 校验与适配器两处**拒绝**而不是静默丢弃——丢弃会让第二页看起来像第一页的重复。

### 2.3 ≤20 词逐字许可写进合同层

存的是**整段摘录**（那是我们被授权*读*的东西，截断会让 claim 层对着片段推理而引用指向没人留下的段落），
许可约束的是**复制**。所以每条摘录、每份 manifest 都带 `quote_policy: {"max_verbatim_words": 20}`，
`verify_guidepoint_quote()` 是引用必须过的门，两种拒绝：

1. 超过词数上限 → `GuidepointQuotePolicyError`（测试里用的是**真·逐字**的 21 词，拒绝理由是许可不是准确性）；
2. 不是摘录里的逐字原文 → 拒绝（把改写塞进专家嘴里比不引用更糟）。

manifest 可以比许可更严（`max_verbatim_words: 3`），**不能更松**（500 被 validator 拒）。
`document_numeric_claim` / 定性引用模块一行没改：它们调这个 helper，或者不引用。

---

## 3. 上线要接的线（集成时统一做）

### 3.1 `coverage_mission.DISCOVERY_SOURCES` —— 一条，必需

`coverage_mission.py` 是我的禁改文件，但 `authorize_source_discovery` 与 `record_source_discovery`
都按这张表判定。缺这一条，lane 一切正常直到 mission 授权那一步。原样加在 `source:sec-edgar` 之后：

```python
    # S2: Guidepoint 专家访谈库。发现的单位是问答摘录，不是访谈稿——
    # 上游没有读全文的 op（见 guidepoint-get-transcript-narrowing-v1）。
    "source:guidepoint": MappingProxyType({
        "connector_source_ref": "source:guidepoint",
        "operation": "search_library",
        "document_ref_prefix": "guidepoint-excerpt:",
    }),
```

`validate_mission_source_discovery` 里 `search_library` 分支要求参数是
`{query, filters, cursor}`——本 lane 用的正是那个冻结形状，无需再改。

测试里用 `mock.patch.object(coverage_mission, "DISCOVERY_SOURCES", ...)` 打上这一条，
证明它后面的一切已经是对的；子进程测试**故意不打补丁**，于是 ticket 停在
`CoverageMissionConflict: source:guidepoint is not a search-driven discovery source`——
这句话就是整条 lane 还欠的全部。

### 3.2 lane registry —— 已做完

合并 main（Wave 0，`888a814`）后按新机制自行登记，`writer_server.py`、`bounded_planner_driver.py`、
`macos_launchagent.py` 一行没动，共享改动只有 `LANE_MODULES` 里的一行 import：

- `mission_guidepoint_lane.py` 末尾 `register_lane(LaneSpec(operation="dispatch_guidepoint_discovery",
  order=35, driver_key="guidepoint_discovery", handler=dispatch,
  init_kwarg="guidepoint_search_launcher", argparse=..., launcher_factory=..., argv_fragment=...))`；
- order 35 = 三条既有 discovery 协调器（30）之后、document extraction（40）之前——这条 lane 排进队列的
  东西正是抽取层要读的；
- `lane_registry.LANE_MODULES` 加 `"dalton_core.mission_guidepoint_lane"`；
- `tests/test_lane_registry.py` 里四处"迁移前字面量"同步加上本 lane（`LANE_OPERATIONS`、
  `CORE_DISCOVERY_OPERATIONS`、`TICK_ORDER`、launcher kwargs 集合）。这四处一起改正是 registry 的意义：
  一条新 lane 要么四个地方都出现，要么 registry 没在干活。

tick 返回 `{status, reason, budget, launched, skipped}`，可直接进 cockpit；`reason` 的封闭词表是
`quota_exhausted` / `tick_cap_reached` / `nothing_due` / `all_grants_refused`（`skipped[].reason`
另有 `child_slot_busy`）。写 CLI 开关：`--guidepoint-search-governance`、`--guidepoint-discovery-plan`、
`--guidepoint-mcp-endpoint`、`--guidepoint-fixture`；两个文件都在才开，缺一则 lane 不存在
（只给一个会得到一条"起来了但每 tick 都拒"的 lane，那看起来像故障而不是缺席）。

协调器在没人指定 mission 版本时自己解析 `missions.active_mission(plan["mission_ref"])`，
每 tick 解析一次不缓存——owner 连上或断开这个源的机制就是发新 mission 版本，
攥着上周版本的 lane 两样都看不见。

### 3.3 `deploy/macos/install.sh`

治理记录**已经有人在种**（第 108–114 行的 `for guidepoint_kind in ...` 循环，P13ae 留下的），
不用动。要加的是 discovery plan，照 AlphaEngine（141–146 行）与 web-search（169–170 行）的写法：

```bash
gp_plan_file="$plan_dir/us-it-services-guidepoint-v1.json"
if [[ ! -f "$gp_plan_file" && -f "$repo_root/deploy/phase9/p9-us-it-services-guidepoint-v1.json" ]]; then
  cp "$repo_root/deploy/phase9/p9-us-it-services-guidepoint-v1.json" "$gp_plan_file"
  chmod 600 "$gp_plan_file"
fi
```

`macos_launchagent` **不用改**：argv 片段由 `LaneSpec.argv_fragment` 提供，条件是
`<state>/connector-governance/guidepoint-search-library-v1.json` 与
`<state>/discovery-plans/us-it-services-guidepoint-v1.json` 两个文件都在。
所以 install.sh 种下 plan 这一步同时也是打开这条 lane 的开关。
收窄提案 `guidepoint-get-transcript-narrowing-v1.json` **不种进 state**——它是给 owner 读的裁决材料，
不是运行时会被加载的记录。

### 3.4 `document_extraction._document_text` 一行分发

抽取层按 `review["source_ref"]` 分支，Guidepoint 需要一支：

```python
if review["source_ref"] == GUIDEPOINT_SOURCE_REF:
    _, text = verified_guidepoint_source(
        self.writer.store, self.writer._transcript_spool, manifest, reader)
    return text
```

`verified_source` 不能复用：它校验的是 AlphaEngine 的**分页** manifest（offset、contiguous、
assembled prefix hash），而一段摘录没有页。硬套要伪造一个单页的 `content_chars`，那是哈希抓不到的谎。
所以本片新增 `verified_guidepoint_source`，纪律一模一样：九张 Core 回执按 ref **与** hash 重读，
文本从原始字节重新推导，再和 spool 里存的对象逐字节比对。

### 3.5 mission 版本：`source:guidepoint` → connected

`deploy/phase9/p9a-us-it-services-mission-v1.json` 的 `source_plan` 里 guidepoint 现在是
`not_connected`，automation 会被直接拒。发新版本时：

- `source_plan` 中 `{"source_ref": "source:guidepoint", ...}` 的 `status` 改为 `connected`；
- `autonomy.may_write` 需要含 `source_discovery` 与 `observation`（两者本 lane 都要，
  `authorize_source_discovery` 同时检查）；
- 人（`human:*`）可以在 `probe_only` 下先排练一次，这是 owner 提升连接器前的正常路径。

### 3.6 凭证槽

`credential-slot:guidepoint`（`guidepoint_core.CREDENTIAL_SLOT_REF`，已批记录的
`allowed_permissions.credential_slot_refs` 就是它）。`GUIDEPOINT_CLIENT_ID` /
`GUIDEPOINT_CLIENT_SECRET` 留在 `~/.openclaw/workspace/config/guidepoint_mcp.env`，
token 由 LaunchAgent `ai.openclaw.guidepoint-mcp-proxy` 刷新并存在 OpenClaw 侧。
Dalton 全程只带槽名：`credential_material: forbidden`、`network: false`，
子进程拿到的是一个 loopback endpoint 和一个不透明 handle。

### 3.7 配额

`connector_quota_policy` 追加 `("guidepoint","search_library")`：**日 25 次搜索**，每单位 1 次物理调用。
是所有搜索源里最小的，理由写在代码注释里：Guidepoint 许可允许研究性阅读、明确禁止批量抽取，
一条一天能跑几百次搜索的 lane，流量形状已经不像研究了。plan 全量扫一遍是 14 条查询，
cadence 是 14/21 天，稳态每天个位数；25 够一天跑完一次全量再加重试，且不像爬虫。往上调是治理决定，不是改常量。

三重上限取最小：治理配额 25 / plan `max_calls_24h` 20 / 每 tick `max_calls_per_tick` 3。

---

## 4. `guidepoint-get-transcript` 的收窄

**做法**：新增 `deploy/connector-governance/guidepoint-get-transcript-narrowing-v1.json`
（`connector-operation-narrowing:guidepoint-get-transcript:v1`，`status: proposed`，
`availability: not_available_upstream`，`supersedes_ref` 指向 v1 治理记录）。

**为什么不是改 v1**：已批记录的 `expected_schema_hash` 来自打包模板里 `get_transcript` 的 contract。
把那条 op 从 `PROFILE_DEFINITIONS` 删掉，签过的记录就再也验不出来了——它会从"我批准了这个确切合同"
变成"我批准了一个已经不存在的东西"。所以 `guidepoint_core.py`、`connector_inventory` 与两条 v1 json
一个字节没动（`test_the_narrowing_is_a_new_proposal_that_leaves_v1_untouched` 逐字段核对）。

**owner 要裁的**：

1. 撤回 `approval:connector-governance:guidepoint-get-transcript:v1`，或者知情地让它休眠——
   没有 lane 会发布它的 capability，search lane 在加载时就会拒绝那条记录
   （`GuidepointSearchGovernance` 同时校验 capability_id 与 schema hash）。
2. 确认 `capability:dalton:connector:guidepoint-get-transcript` 保持未发布。
3. 留下的读全文路径：每条摘录的 `reference_url` 指向 Guidepoint360，由人在自己的 Guidepoint 会话里打开；
   Dalton 不去抓。
4. 若 Guidepoint 将来上了读文档的 op，那是一份新 contract、新 schema hash、新审批，不是复活这一条。

---

## 5. 冒烟：一次真实查询（唯一一次）

本地代理在（`127.0.0.1:8943` 可连），按授权只发了**一次**只读 `search_library`，写进 `/tmp` 临时 state 目录，
不碰 live 状态，不写 mission：

```
outcome: succeeded
source_status: complete
excerpts: 20
   guidepoint-excerpt:sha256:08f324a49498bf67cc3fbec25f778b21b8a517f2d28171b83134e7c64d4e630d
   guidepoint-excerpt:sha256:7275804df868cbab1dbee5e62e2821b580e70b4f1f5e1d3ea9bf64ccc93002dc
   guidepoint-excerpt:sha256:a0e78a3eec529f224005bd5921c8e0d3cf77befa811fe3adb171cbe5dd86ad53
raw bytes: 147931 sha256: b8945ac9544271cf
```

**这一次调用抓到一个会让整条 lane 在真实数据上崩的 bug。** 搜索成功、20 条摘录 ref 正常落进 envelope，
但紧接着**重读原始 artifact 失败**：`Guidepoint raw response is not strict UTF-8 JSON`。
原因是 Guidepoint 代理用 `text/event-stream` 回 `tools/call`，spool 里的字节是 SSE 帧，不是 JSON 文档。
AlphaEngine 的 8950 代理回的是纯 JSON，所以照抄它的 raw 解析在 fixture 上全绿、在真数据上必挂——
而 acquisition 与 extraction 全靠"从原始字节重推"。

修法：`guidepoint_json_rpc_from_raw` 改用 bridge 自己的 `_parse_mcp_body`（两种 framing 都吃），
不另写一个会和传输层对字节含义产生分歧的第二解析器。`FakeGuidepointHandle` 增加 `sse=True`，
新增两项测试（`test_the_live_sse_framing_is_readable_and_so_is_plain_json`、
`test_an_sse_framed_search_acquires_and_verifies`）把 SSE 形状钉进套件。

真实摘录内容没有落进仓库任何文件；fixture 全是合成文本。

---

## 6. 验收

全量：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`

```
Ran 2125 tests in 271.075s

OK (skipped=1)
```

（合并 Wave 0 之后；Wave 0 自身带来 2,034 → 2,080，本片新增 45 项：19 + 12 + 14。）

覆盖到的：审批拒绝（proposed 记录一次调用都不发、Core 里零 invocation）、原始 artifact 哈希与
从字节重推 refs、输出形状漂移变成失败尝试而非异常、摘录 id 稳定性、quote-policy 拒绝（>20 词与非逐字）、
discovery 记录（打补丁的 DISCOVERY_SOURCES）、acquisition manifest 校验与四种伪造拒绝、
配额耗尽行为、SSE framing、真实子进程 tick。

---

## 7. 遗留问题

1. **`DISCOVERY_SOURCES` 一行**（3.1）是硬阻塞，集成时必须加，否则 lane 全程正常但永远授权不到。
2. **行业查询挂锚定公司**：mission 授权是按公司发的，而"GenAI 是否在压缩计费工时"这种问题没有 issuer。
   plan 里显式写了 `industry_anchor_company_ref`（ACN），行业查询记在 ACN 名下但摘录是行业级证据。
   比另一种做法诚实——那种做法是写五条几乎一样的公司查询假装成行业扫描。
   若 owner 认为行业证据不该挂在某一家名下，需要 `coverage_mission` 支持 mission 级（非公司级）discovery，
   那是它自己的一片。
3. **摘录的证据层级**：盘点里定为"专家 / 准一手"。本片没有写 grade——`document_figure_grade` 不是我的文件。
   建议新增 `expert-network-transcript` 一档，位于管理层原话之下、卖方研报之上。
4. **`context` 字段**：上游有时给 surrounding passage。目前只存进摘录记录不进 `excerpt_text`，
   因为许可边界按"返回的 answer"算最保守。若 owner 认为 context 同样在许可内，是一个 manifest 版本升级。
5. **`search_events` / `register_event` 等四个 op**：OpenClaw 的 Guidepoint bridge 还暴露事件类工具。
   本片一个都没接，也不建议接——`register_event` 是写操作，和"只读连接器"的整个 permission 形状冲突。
6. **`max_excerpts` 目前只进 plan 不进 profile**：profile 的 `max_records` 是 20，spec 的 `max_excerpts`
   是 12，适配器发的 `size` 取的是 profile 上限而非 spec 的。要让 spec 真正收窄一次调用的返回量，
   需要把 per-query 上限带进 compiled step 的参数里——那会动到冻结的输入 schema，属于新 profile 版本。
   现状不是 bug（20 是硬上限，plan 的 12 是预算意图），但值得记一笔。
