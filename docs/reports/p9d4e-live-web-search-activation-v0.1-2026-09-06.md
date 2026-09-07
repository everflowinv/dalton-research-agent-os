# P9d-4e：web search 真实激活与首次真实搜索（OpenClaw 侧已启用，Dalton 未部署）

日期：2026-09-06
状态：OpenClaw 插件**已安装、已配置、正在运行**；首次真实 Gemini 搜索在 live Core 只读副本上跑通；
**Dalton 代码栈未部署、live mission 未改、live Core 未写入**。
基线：`4001a07`（P9d-4d）；分支：`p9d4e-real-gemini-payload-contract`。
上游：[P9d-4a](p9d4a-web-search-mission-discovery-v0.1-2026-09-06.md)、[P9d-4d](p9d4d-openclaw-web-search-broker-v0.1-2026-09-06.md)。

## 这一片做了什么

owner 批准两条治理记录后，本片按其指示完成 OpenClaw 侧激活并执行了授权的真实搜索测试。真实调用推翻了两个
此前只能靠推断的判断，并暴露了三个真实缺陷——全部在 Dalton 任何 live 写入之前被发现和修复。

## 激活（OpenClaw 侧，已生效）

- `openclaw config patch`（先 `--dry-run` 通过）追加：`plugins.load.paths` 增加插件目录、`plugins.allow` 增加
  `dalton-openclaw-web-search-broker`、`plugins.entries` 增加 `{enabled, clientId: client:dalton-core,
  expectedProvider: gemini, socketName}`。改动前已备份 `openclaw.json`；模型 broker 条目原样保留。
- 重启 gateway（launchd `ai.openclaw.gateway`）。插件启动，owner-only socket 与 key 就位
  （`dalton-web-search-broker.sock` / `.sock.key`，均 0600）。`openclaw plugins doctor` 与 `openclaw health` 通过。
- 本机既有事实：`tools.web.search` 已启用、provider `gemini`、超时 240s；`models.providers.google.apiKey` 已配置。

## 真实调用推翻的两个判断

1. **`api.runtime.webSearch.search` 返回 `{provider, result}` 包装，不是 provider payload 本身。**
   P9d-4d 的 broker 把包装原样透传，Dalton 的 normalizer 因此如实拒绝（形状漂移）。已修：broker 校验包装形状、
   要求外层与内层 provider 都等于配置值，然后**只转发内层 payload**。
2. **内层 payload 正是 2026-08-14 冻结的那个形状**（`query/provider/model/tookMs/content/citations/externalContent`）。
   我在本片早期曾用 OpenClaw 的 **agent 工具** `web_search` 取样，那是另一个表面（返回 `kind` 而非 `model`），
   并据此"纠正"了契约——那次纠正是错的，已完全回退。教训记录在案：**取样必须取 Dalton 实际消费的那个表面**。
   现在 normalizer 的 docstring 写明两个表面的差异，并有一条测试确认 agent 工具形状在这条路径上被拒。

## 真实调用暴露并修复的三个缺陷

1. **broker 未拆包装**（见上）。新增 Node 测试覆盖：正常拆包 + 8 种包装漂移（缺 result、result 非对象、多余键、
   外层/内层 provider 不符等）全部 `PROVIDER_CONTRACT_DRIFT`。
2. **Gemini grounding 返回的 citation 数超过页面上限。** 一次真实搜索返回 13 条引用，而冻结上限是 10，旧代码
   把超额当作硬错误，真实答案会被拒。inventory 早已把该来源的 `completeness_ceiling` 定为 `ranked`（排名子集，
   从不声称穷尽），因此改为**取排名前 N 条**并共享同一个常量 `GEMINI_WEB_SEARCH_MAX_RECORDS = 10`，
   URL authority 重建用同一常量，避免两处漂移。原始 artifact 始终保留全部引用。
   同时修正既有测试：声称全部 11 条引用的 envelope 与实际准入的 10 条不符，应被 `PublicWebAuthorityConflict` 拒绝。
3. **broker journal 重启后拒绝加载自己的文件。** 复制自模型 broker 的 `journal.mjs` 把记录键钉死为
   `^invocation:`，而本 broker 以 Dalton 的 `credential-use:` ref 为键；重启时插件因
   `journal invocation id is invalid` 启动失败，socket 变成陈旧文件。已改为校验为**有界的命名空间 ref**。
   新增回归测试：写入一次 → 重启 → 同 key 重放为 `duplicate` 且宿主零调用（这条测试此前缺失，正是它漏掉了该缺陷）。

## 首次真实搜索（live Core 只读副本，`ok=true`，条件具名全真）

`scripts/run_p9d4e_live_web_search_canary.py`（仓库中唯一会花真实调用的脚本）：

- 副本发布 `probe_only` mission 版本；owner 身份经**真实 launcher → 真实子进程 → 真实 broker → 真实 Gemini**；
- transport `openclaw-search-broker`，**1 次 provider 调用**，query
  `Accenture ACN IT services demand bookings outlook`（60 天窗口）；
- 返回 **5 个去重后的 URL ref**（news.alphastreet.com、www.morningstar.com、koalagains.com、seekingalpha.com、
  www.moomoo.com），mission 发现记录 5 条，URL authority 由 4090 字节的原始 artifact 逐字重建；
- **合成答案只留在原始 artifact 里**（`EXTERNAL_UNTRUSTED_CONTENT` 标记仅出现在 raw，未进入向后传递的结构化输出）；
- `formal_authority_writes=0`；Claim 6 / Evidence 6 / Thesis 2 前后不变；`integrity_check=ok`；
- **live Core 只读打开，只写副本**；live mission、live 配置、Dalton 服务均未改动。

本轮共经 broker 花费 **4 次真实 Gemini 搜索**（3 次失败诊断 + 1 次成功），另有 1 次经我自己的 agent 工具取样
（即那次取错表面的调用）。全部在计划的 24h 上限 40 之内。

## 验收

- 插件 Node **16/16**（新增重启重载回归）；模型 broker **25/25** 原样通过。
- Python web 相关专项 **56/56**（含真实 host 返回值录制的 fixture 回归、agent 工具形状被拒、超额引用取排名前 N）。
- 全仓 unittest **1162/1164**（两条失败仍是既有 `test_document_extraction_preflight` 的 `/private/var` 路径断言，与本片无关）。
- 真实 host 返回值已录成 fixture `tests/fixtures/openclaw_gemini_web_search_2026-09-06.json`
  （内容截断、引用取前 3 条，未信任标记原样保留），供后续漂移检测。

## 未做与仍需 owner 决定

- **Dalton 代码栈仍未部署。** live writer 仍跑旧 wheel，没有 web search/fetch/extraction 任何一片，也没有
  broker 接线参数。部署会一次性上线 P9d-3a、P9d-3b、thesis-impact 换版修复、P9d-4a~4e 共五片，并重启四个
  LaunchAgent——影响面明显大于本片，因此留给 owner 决定时机。
- **live mission 未改**：`source:web-search` 仍是 `not_connected`，因此即便部署，live 上也不会自动搜索。
  启用顺序建议：部署 → 传 broker socket/key 给 writer → 发布 mission 新版本先 `probe_only` 人工排练 → 再 `connected`。
- 真实抓取（fetch lane）尚未对真实站点跑过：本片只验证了搜索。P9d-4b 的 fetch 会访问第三方站点原文，
  首次真实抓取应单独授权。
- 未验证 Gemini 在不同 query 下的引用质量与稳定性；单次成功不代表命中率。

## 复跑

```bash
(cd integrations/openclaw-web-search-broker && npm run check)
.venv/bin/python -m unittest tests.test_public_web_connector tests.test_openclaw_web_search_broker_client -v
# 会花 1 次真实 Gemini 搜索：
.venv/bin/python scripts/run_p9d4e_live_web_search_canary.py \
  --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
  --governance "$HOME/Library/Application Support/Dalton/state/dalton-core/connector-governance/gemini-web-search-v1.json" \
  --broker-socket "$HOME/.openclaw/dalton-web-search-broker.sock" \
  --broker-auth-key "$HOME/.openclaw/dalton-web-search-broker.sock.key" \
  --output temp/p9d4e/live-search-canary.json
```
