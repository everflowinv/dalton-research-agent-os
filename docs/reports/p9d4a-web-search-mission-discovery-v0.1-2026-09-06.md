# P9d-4a：web search 作为 mission 第二个发现来源（shadow）

日期：2026-09-06
状态：development candidate；专项、全仓与 live Core 只读副本 canary 通过；**未部署、未写 live、无真实 host 调用**。
基线：`df7a8b038c91b7798d45a25635dda4c1cb2bb978`；分支：`p9d4a-web-search-mission-discovery`。
上游：[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)（P9d 顺序：web search 先于 Guidepoint）、[P9d-1](p9d1-alphaengine-search-driven-source-discovery-v0.1-2026-09-02.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)、[ADR-0003 B](../adr/0003-transcript-candidate-admission.md)。

## 这一片解决什么

P9d-1 之后 CoverageMission 只能通过 AlphaEngine `search_library` 自己找资料；mission v1/v2 的 `source:web-search`
一直是 `not_connected`。仓库里早有 Gemini `web_search` 发现桥与独立的 public-web `fetch_get` adapter（2026-08-14
inventory），但它们从未接到 Core-hosted 的治理执行链，live MCP 通道的 plan builder、请求校验和准入 gate 都硬绑
AlphaEngine，mission 的发现计划、账本和协调器也只认一个来源。

本片把 web search 接成 mission 的第二个搜索驱动发现来源，形态与 P9d-1 完全对称：owner 写好的 hash-bound
DiscoveryPlan → 单槽子进程 → Core connector authority（ConnectorInvocation / usage / cost / quota / 原始
ArtifactVersion / SourceEnvelope）→ mission 发现账本 → 待获取文档。**搜索结果只是发现**：Gemini 的综合答案、
snippet 和标题留在原始 artifact 里，向后只暴露由引用 URL 派生的 opaque `public-web-url:sha256:<hash>` ref；
被引用页面的原始字节只能经独立的 public-web `fetch_get` 进入 authority，那条 lane 是下一片 P9d-4b。

## 冻结边界

- **host bridge 注册表。** `live_mcp_connector` 由单一 AlphaEngine 桥改为冻结的两条目注册表
  `{alphaengine, gemini-web-search}`，每条目声明 inventory template、operation→host tool 映射、模板 source type
  与逻辑 credential slot。operation 名在注册表内唯一，所有校验器按 operation 解析桥；AlphaEngine 的 plan id、
  hash、错误路径逐字节不变（既有 live MCP / 搜索 / 发现测试全部原样通过）。Gemini 桥复用发现 adapter 冻结的
  `openclaw-bridge:gemini-web-search:0.1` 身份；host key 留在 OpenClaw，Core 只带 `credential-slot:gemini-web-search`。
- **独立治理能力。** `capability:dalton:connector:gemini-web-search`（kind `gemini-web-search`）进入
  `connector_governance` 注册表，`dalton-connector-governance approve` 可原地批准；proposed 记录未批准时
  launcher 在进程启动前拒绝。deploy 记录 `deploy/connector-governance/gemini-web-search-v1.json` 为 **proposed**。
- **DiscoveryPlan 0.2。** 0.1 保持 AlphaEngine 专用且逐字节不变；0.2 允许 `source:web-search`（spec 无
  `document_type`，编译为显式日期窗的 `search_web` 参数）并新增计划级 `budget.max_calls_24h`（≤1000）。
  mission body 没有 web search 预算字段，Gemini 调用由 host 计费，因此**人工编写、hash 绑定的计划预算就是
  硬上限**；AlphaEngine 计划改用 0.2 时该预算与 mission/owner 上限取小。
- **来源表。** `coverage_mission.DISCOVERY_SOURCES` 冻结两条：`source:alphaengine`（envelope `source:alphaengine`
  / `search_library` / `alphaengine-doc:`）与 `source:web-search`（envelope `source:public-web` / `search_web` /
  `public-web-url:sha256:`）。表外来源（如 `source:sec-edgar`）不再能通过 `authorize_source_discovery`；
  发现记录的 ref 前缀、参数形状与 envelope 来源/操作按来源校验。
- **授权规则不变。** `not_connected` 拒绝所有人；`probe_only` 只允许 `human:` 排练；自动化需要 `connected` +
  `source_discovery` + `observation`。live mission v2 的 web-search 仍为 `not_connected`，本片不改 live mission。
- **两条 lane 互不干扰。** 协调器按计划来源过滤 dispatch 与文档：AlphaEngine 协调器不会结算 web 票据，
  也永远不会把 URL ref 交给 AlphaEngine 获取子进程；web 协调器不接受 acquisition launcher，
  `launch_acquisition` 如实返回 `unconfigured` + `queued`，URL 停在 `discovered`。
- **没有真实 transport。** `WebSearchLauncher` 在 `--allow-network` 下于 spawn 前拒绝（固定原因），子进程
  `public_web_search_cli --allow-network` 也在触碰 Core 前写出固定失败 summary 并退出 1。本片只有 fake
  citations 排练 transport。

## 实现

- `live_mcp_connector.py`：`HostToolBridge` 注册表、`host_tool_bridge_for_operation`、按桥解析的 plan
  builder / plan & adapter request 校验 / gate 绑定检查；`search_web` 与 `search_library` 同为 ranked 单页，
  `search_web` 不得带 cursor。
- `public_web_core_search.py`（新）：`search_web` 的 Core-hosted 执行器 `PublicWebCoreSearch`（治理记录、
  descriptor、chained profile、价格/速率策略、compiled plan → live MCP transport plan → gate → executor、
  durable replay、receipt）；`GeminiWebSearchLiveAdapter`（LiveMcpAdapterRequest → host handle →
  mcp_managed 0.2 observation，复用发现 adapter 的 payload 规范化）；`FakeWebSearchHandle`；
  `validate_web_search_spec` / `web_search_spec_hash`；`count_recent_web_search_calls`；
  `public_web_urls_in_authority`（只有绑定 `fetch_get` 调用的 complete/partial envelope 才算在 authority）；
  `url_authorities()` 从 exact 原始字节 + envelope 重建 `PublicWebUrlAuthority`，不落库。
- `public_web_search_cli.py`（新）：子进程，重算 mission 授权、执行搜索、写发现记录，summary 附带
  重建出的 canonical URL 供人看。
- `mission_source_discovery.py`：plan 0.1/0.2 校验与 builder、`discovery_query_hash`、`_SearchLauncherBase`
  + `AlphaEngineSearchLauncher` / `WebSearchLauncher`、来源感知协调器（预算、结算、获取过滤，tick 带 `source_ref`）。
- `coverage_mission.py`：`DISCOVERY_SOURCES`、来源相关校验、`open_discovery_dispatches` /
  `launched_discovered_documents` / `next_discovered_document` / `retryable_failed_document` 的可选来源过滤。
- `connector_governance.py`：新 kind 与 builder 分派。
- `writer_server.py`：第二个 launcher/plan/协调器；`dispatch_mission_source_discovery` 保持 P9d-1 形状并追加
  `web_search` 子结果；`run_mission_source_discovery` 新增可选 `source_ref`；ticket 前缀路由状态查询；
  新 CLI 参数 `--web-search-governance` / `--web-search-discovery-plan` / 两个 rehearsal 参数。
- `macos_launchagent.py` + `install.sh`：seed-once proposed 治理记录与 0.2 计划并传给 writer；live 模式下
  launcher 拒绝、tick 报告原因，且 live mission 的 grant 先于此拒绝。
- contracts：`mission-discovery-plan.schema.json`（0.1/0.2、按来源的 spec 形状与预算条件）、
  `coverage-mission-source-discovery.schema.json`（来源枚举、按来源的 ref 前缀与参数形状）。
- deploy：`gemini-web-search-v1.json`（proposed）、`p9d4-us-it-services-web-search-plan-v1.json`
  （五家公司 × `industry-demand` / `competitive-landscape` / `management-changes`，`max_calls_24h=20`）。

## 验收

- 新增专项 **18 条**：`tests/test_public_web_core_search.py`（治理/CLI 批准、桥注册表、spec、执行器：URL 去重、
  原始 artifact、free replay、空结果、限流记录为 retryable、proposed 拒绝、身份/payload 漂移 fail closed）与
  `tests/test_mission_web_search_discovery.py`（计划 0.2 与 deploy 文件、授权表、发现记录与合同、协调器
  not_connected→connected 全周期与计划预算、AlphaEngine 协调器忽略 web 行、真实 launcher/子进程含网络拒绝、
  writer ops）。
- 既有邻接：live MCP、AlphaEngine 搜索/发现、writer ops、治理、inventory、配额、service、contracts 原样通过。
- 全仓 unittest **1133/1135**：两条失败均为 `test_document_extraction_preflight` 里既有的路径断言（macOS `TMPDIR` 的 `/var` 与 sqlite 回报的 `/private/var` 不等），在基线 `df7a8b0` 上原样复现，与本片无关，本片未改动该测试。`git diff --check`、compileall、全部 JSON 解析通过。
- wheel + sdist 构建；干净 Python 3.14 venv `pip --no-index --no-deps` 安装 wheel 后，用 installed package
  跑上述 18 条专项通过（子进程走 installed 模块）。wheel SHA-256
  `8a53b36a94d692d017551024bb0de576f0d32e8de4567a7c0f7c4eab3319707f`。
- live Core 只读副本 canary `scripts/run_p9d4_web_search_discovery_canary.py`（`temp/p9d4a/live-copy-canary.json`，
  `ok=true`）：
  - live mission v2 下 `source:web-search=not_connected`，自动化 tick `not_authorized`（原因 `not_connected`），
    owner 同样被拒；计数不变；
  - 副本发布 v3（`probe_only`）后，网络模式 launcher 在 spawn 前 `DiscoveryLaunchRejected`；owner 排练走真实
    子进程（fake citations）：succeeded、2 个新 URL ref、1 次调用、`formal_authority_writes=0`，summary 附
    canonical URL；下一 tick 结算 succeeded，acquisition `unconfigured/queued`；
  - 副本发布 v4（`connected` + `source_discovery`）后，自动化跑完 5 家公司 × 3 spec = **15/15 dispatch
    succeeded** 并进入 idle，全部 `requested_by=automation:coverage-mission`；URL 全部停在 `discovered`；
  - Claim 6、Evidence 6、Thesis 2 前后不变；AlphaEngine 的发现/dispatch/文档行数不变（12/15/157）；
    web search 调用 0→16；`PRAGMA integrity_check=ok`；0 网络、0 付费、0 live 写入。

## 未做与 owner gate

- **未部署。** 部署后 install.sh 只 seed proposed 记录与计划；writer 以 live 模式启动 web launcher，但 (a) live
  mission 仍标 `not_connected`，(b) launcher 无 host bridge 而拒绝，两层都会让 tick 如实报告而不花钱。
- **真实 OpenClaw gateway `web_search` handle 未接线。** 下一片要让 gateway 把只允许 `web_search` 的 host-owned
  handle 交给子进程（同 `OpenClawToolHandle` 协议），并做首次真实 canary；真实调用质量、Gemini 引用可用性与
  host 侧计费尚未验证。
- **public-web `fetch_get` lane 未接（P9d-4b）。** 发现的 URL 只能停在 `discovered`；页面原始字节进入 authority、
  进人工抽取队列、进 ADR-0003 B 人工 accept，都在那一片。`public_web_urls_in_authority` 已按 fetch_get 语义
  实现，当前恒为空。
- **激活需要 owner 分两步**：① `dalton-connector-governance approve` 批准 `gemini-web-search-v1.json`；
  ② 发布 mission 新版本把 `source:web-search` 从 `not_connected` 改为 `probe_only`（先排练）或 `connected`
  （`scripts/build_mission_v2_params.py --set-source-status source:web-search=connected`）。
- Guidepoint、M2 市场数据顺序不变。

## 复跑

```bash
.venv/bin/python -m unittest tests.test_public_web_core_search tests.test_mission_web_search_discovery -v
.venv/bin/python -m unittest tests.test_live_mcp_connector tests.test_mission_source_discovery tests.test_p9d_writer_ops \
  tests.test_connector_governance tests.test_connector_inventory tests.test_contracts tests.test_service -v
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/run_p9d4_web_search_discovery_canary.py \
  --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
  --output temp/p9d4a/live-copy-canary.json
.venv/bin/python -m build --no-isolation --outdir temp/p9d4a/dist
```
