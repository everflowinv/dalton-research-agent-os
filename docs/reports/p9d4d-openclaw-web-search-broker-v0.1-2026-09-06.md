# P9d-4d：host-owned OpenClaw web search broker（真实 transport 接线，未启用）

日期：2026-09-06
状态：development candidate；Node 与 Python 双侧专项、跨语言回环、全仓通过；**未安装插件、未部署、0 次真实搜索**。
基线：`48e26ce`（P9d-4c）；分支：`p9d4d-openclaw-web-search-broker`。
上游：[P9d-4a](p9d4a-web-search-mission-discovery-v0.1-2026-09-06.md)、[P9d-4b](p9d4b-public-web-fetch-lane-v0.1-2026-09-06.md)、[P9d-4c](p9d4c-public-web-extraction-source-v0.1-2026-09-06.md)、模型 broker（`integrations/openclaw-model-broker`）。

## 这一片解决什么

P9d-4a 冻结了 Gemini `search_web` 桥，但**没有可调用的 transport**：本机 OpenClaw 只把 MCP 暴露给
guidepoint(8943)、alphaengine(8950) 与远端 firecrawl，web search 是 OpenClaw 的 agent 工具与插件运行时能力，
不在 MCP 上。因此 `WebSearchLauncher` 的网络模式此前一律在 spawn 前拒绝，整条 web 链只能是 shadow。

本片按模型 broker 已有的边界补上这条 transport：新增 Dalton 自有的 OpenClaw 插件，宿主保管 provider 与凭据，
通过 owner-only Unix socket 只提供**一次有界搜索**；Core 只发送一条 exact query，永远不发送 key、provider、
model、endpoint 或 header。Python 侧 `WebSearchBrokerHandle` 实现与 loopback MCP handle 相同的
`OpenClawToolHandle` 协议，因此其上的 adapter、transport plan 与 authority 链**逐字未变**。

调查结论（本机事实）：`tools.web.search` 已 `enabled`、`provider: "gemini"`、超时 240s；
`models.providers.google.apiKey` 已配置。插件运行时暴露 `api.runtime.webSearch.search({config, args})`
（`docs/plugins/sdk-runtime.md`、`architecture-internals.md`），这正是 broker 需要的共享接口。

## 冻结边界

- **闭合请求，无凭据面。** 请求只含 `schemaVersion / callRef / profileId / query / count /
  timeoutMs`，可选 `dateAfter+dateBefore`（必须成对且有序）与 `replayOnly`；多一个字段即拒。客户端
  不能选择或传入 provider、model、endpoint、baseUrl、header 或任何 key。
- **provider 由宿主决定并被核对。** broker 调用宿主共享 helper 后，若返回 payload 的 `provider` 与配置的
  `expectedProvider` 不符（或不是对象），返回 `PROVIDER_CONTRACT_DRIFT` 而不是交出另一种形状——
  Dalton 的 Gemini normalizer 钉死 7 键形状，静默换 provider 会破坏该契约。
- **payload 逐字透传。** broker 不改写、不重排、不摘要、不过滤；payload 原样放进 tool-result 信封
  （`result.content[0].text`），因此存进 Core 的**原始 artifact 就是 broker 应答帧**，P9d-4b 的
  `build_public_web_url_authorities` 无需改动即可从中重建 URL authority（专项已验证）。
- **认证与重放。** HMAC-SHA-256 覆盖去掉 `auth.mac` 的规范化请求；强制 client 身份、时间偏移、MAC 与
  nonce 重放保护，全部先于请求校验。key 为 owner-only `<socketName>.key`，不进应答、参数或日志。
- **幂等以 `callRef` 为键**（Dalton 的 credential-use ref，一次 physical attempt 一个）：先落 `pending` 声明
  再调宿主；崩溃留下的 `pending` 报 `IDEMPOTENCY_INDETERMINATE`，绝不自动重搜；`replayOnly` 只读、
  未命中返回 `IDEMPOTENCY_MISS` 且不触达宿主；同 ref 换 query 是 `IDEMPOTENCY_CONFLICT`。
- **不记录内容。** 日志只有 socket 名、expectedProvider 与上限；宿主错误文本截断 300 字符并分类为
  权限/限流/一般失败，错误里不回显 query。
- **仍不会自己跑起来。** 插件未安装、未配置；`WebSearchLauncher` 缺 socket/key 仍在 spawn 前拒绝；
  live mission 的 `source:web-search` 仍是 `not_connected`。本片**没有执行任何真实搜索**。

## 实现

- `integrations/openclaw-web-search-broker/`（新插件）：`protocol.mjs`（闭合请求校验、严格 JSON、规范化
  JSON、封口应答、tool-result 信封）、`broker.mjs`（配置校验、并发、幂等、宿主调用与 provider 核对、
  错误分类）、`plugin-definition.mjs`/`index.mjs`/`openclaw.plugin.json`/`package.json`/`README.md`。
  `auth.mjs`、`journal.mjs`、`server.mjs` 自模型 broker 逐字复制——它们是通用的 socket/认证/幂等层，
  两个插件各自独立安装、各自 socket 与 key，因此按副本而非共享库交付。
- `src/dalton_core/openclaw_web_search_broker_client.py`（新）：`WebSearchBrokerHandle`（签名、成帧、
  超时按 lease 推导并按宿主上限截断、应答校验、错误映射到既有 bridge 异常）、`load_broker_key`
  （owner-only 校验）、`sign_request`。
- `mission_source_discovery.WebSearchLauncher`：接受 broker socket/key/client/profile，网络模式下改为
  仅在缺少 socket 或 key 时拒绝，并把参数传给子进程；transport 标签 `openclaw-search-broker`。
- `public_web_search_cli`：`--allow-network` 改为构造 broker handle（缺参数仍写固定失败 summary 并退出 1）。
- `writer_server`：新增 `--web-search-broker-socket/-auth-key/-client-id/-profile-id` 并透传。

## 验收

- 插件 Node 专项 **15/15**（`npm run check`）：payload 逐字透传与信封、日期窗必须成对有序、闭合请求与
  上限（未知字段、count、query 长度、profile、timeout、schema、callRef 命名空间）在触达宿主前被拒且
  宿主零调用、provider 漂移与非对象 payload 判为契约漂移、宿主错误分类且不回显 query、同 ref 重放只调用
  一次宿主且换 query 冲突、`replayOnly` 命中/未命中、pending 声明阻断自动重放、缺 `webSearch.search`
  的 runtime 在构造期被拒；socket owner-only 0600、未认证/伪造 MAC/换 client/过期/重放全部不触达宿主、
  超长与畸形帧先于认证被拒、key 文件 0600 且复用、插件只注册一个 service 且日志不含 query 或结果、
  源码不含 Dalton authority、`process.env`、`child_process` 或 provider transport。
- 模型 broker 专项 **25/25** 原样通过（未改动该插件）。
- Python 专项 **8/8**（`tests/test_openclaw_web_search_broker_client.py`）：签名请求闭合且不含 key、
  nonce 每次不同、应答帧即原始 artifact 且能被 P9d-4b 的 URL authority 重建逐字解析、错误映射
  （限流/权限/其他）、错误 key 与非 owner-only key 文件 fail closed、客户端侧拒绝（错误工具名、
  freshness、半个日期窗、count、非 credential-use ref、已过期 deadline）时**不打开 socket**、
  超限应答与畸形应答 fail closed。
- **跨语言回环**：Python 客户端对真实 Node broker 签名并往返成功（`fresh` → `duplicate` → 换 query
  `IDEMPOTENCY_CONFLICT`），证明两侧规范化 JSON 与 HMAC 逐字一致；宿主为 fake runtime，无真实搜索。
  另已验证 Python 与 Node 的规范化 JSON 对含非 ASCII 的对象输出逐字节相同。
- 全仓 unittest **1159/1161**（两条失败仍是既有 `test_document_extraction_preflight` 的 `/private/var` 路径断言，与本片无关）；wheel/sdist 与干净 Python 3.14 安装后 installed 专项 18/18，wheel SHA-256 `646eced60c63c6b0e8b4309a1e6c4688bbe5f428c5d374dd512d622a82899ba0`；`git diff --check`、compileall 通过。

## 未做与 owner gate

- **未安装插件、未改 OpenClaw 配置。** 启用需要 owner：①把插件装为 OpenClaw 插件并配置
  `clientId` 与 `expectedProvider: "gemini"`；②把 broker socket 与 key 路径传给 writer；
  ③发布 mission 新版本把 `source:web-search` 从 `not_connected` 改为 `probe_only`（先人工排练）再
  `connected`。治理记录两条已由 owner 批准。
- **首次真实搜索会花钱并访问第三方**：Gemini API 由宿主 `models.providers.google.apiKey` 计费，
  受 mission 计划 24h 上限（当前 40）与 broker `maxCount` 约束。本片未执行，也未验证真实 payload 形状——
  `api.runtime.webSearch.search` 的返回值形状是**本片唯一未经真实验证的契约**；若宿主返回的不是
  provider payload 本身，broker 会以 `PROVIDER_CONTRACT_DRIFT` 拒绝而不是交出错误形状。
- 限流没有宿主提供的 retry 提示，客户端按固定 60s 上报，不伪造 provider 值。
- `auth.mjs`/`journal.mjs`/`server.mjs` 是副本而非共享包；两个 broker 未来若要同步修复需各自更新。

## 复跑

```bash
(cd integrations/openclaw-web-search-broker && npm run check)
(cd integrations/openclaw-model-broker && npm run check)
.venv/bin/python -m unittest tests.test_openclaw_web_search_broker_client -v
.venv/bin/python -m unittest discover -s tests
```
