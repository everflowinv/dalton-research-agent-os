# P9d-4b：public-web fetch lane——把搜索引用的页面原始字节收进 authority

日期：2026-09-06
状态：development candidate；专项、全仓与 live Core 只读副本 canary 通过；**未部署、未写 live、0 网络调用**。
基线：`11d17d5`（P9d-4a）；分支：`p9d4b-public-web-fetch-lane`。
上游：[P9d-4a](p9d4a-web-search-mission-discovery-v0.1-2026-09-06.md)、[P9d-2 人工抽取队列](p9d2-document-extraction-review-queue-v0.1-2026-09-04.md)、[ADR-0003 B](../adr/0003-transcript-candidate-admission.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)。

## 这一片解决什么

P9d-4a 之后 web search 只能留下 opaque `public-web-url:sha256:<hash>` ref，URL 永远停在 `discovered`。本片接上
public-web `fetch_get` lane：协调器像处理 AlphaEngine 文档一样，每 tick 最多为一个已发现 URL 启动一次有预算的获取，
子进程通过既有的 `PublicWebFetchAdapter` + `PublicHttpTransport`（无凭据、只走公网 HTTPS）把页面**原始字节**收进
Core connector authority（ConnectorInvocation / 物理尝试 / usage / cost / quota / 原始 ArtifactVersion / SourceEnvelope），
文档状态转 `acquired`，并按 P9d-2 规则进入人工抽取队列。Gemini 的综合与 snippet 仍然只留在搜索 artifact 里，
从不被当作页面内容。

## 冻结边界

- **URL 从哪来只有一条路。** 子进程只接受 mission 账本里已发现的 ref，找到它的发现记录与 SourceEnvelope，从 exact 原始
  搜索字节重建 `PublicWebUrlAuthority`（`build_public_web_url_authorities`）；搜索没引用过的 ref、篡改过 host 的
  authority 都在取任何字节前被拒。
- **每个 host 一个 profile。** 通用 runner gate 把 profile 的静态 `allowed_hosts` 交给 adapter，adapter 要求它恰好等于
  authority 的 host，所以本 lane 在 `connector:web-fetch` 链上按 host 发布 operation-scoped profile
  （`connector-profile:web-fetch:<host-slug>:v1`）、价格与速率策略（共享 quota scope），绝不发布通配 profile。
- **独立治理能力。** `capability:dalton:connector:web-fetch`（kind `web-fetch`，权限 `network=true`、无 credential slot、
  `read:public-http`）进入注册表，`dalton-connector-governance approve` 可原地批准；deploy 记录
  `deploy/connector-governance/web-fetch-v1.json` 为 **proposed**。配额行 `("web-fetch","fetch_get")` 200 页/日。
- **预算共享。** 搜索与页面获取共用 web 计划的 `budget.max_calls_24h`（与 AlphaEngine 搜索/文档页共用 mission 窗口同构）；
  deploy 计划上限由 20 提到 **40**（hash 随之变化，未上线）。
- **授权规则不变。** 子进程以调用者身份重算 `authorize_source_discovery(source:web-search)`：自动化需 `connected` +
  `source_discovery`，human 可在 `probe_only` 排练；`probe_only` 下自动化获取与队列登记都被拒且 tick 如实报告。
- **只收字节，不解释。** 本片不渲染页面、不抽取文本、不生成候选；Cockpit `mission_document_evidence` 对 web 页面仍拒绝
  （固定文案，指向 P9d-4c）。ADR-0003 B 人工 accept 边界不变。
- **live 模式存在但不会跑。** 网络模式 = 真实 `PublicHttpTransport`；但 live mission 的 web-search 仍 `not_connected`，
  且 P9d-4a 的搜索没有真实 host bridge，所以 live 上没有任何 URL 可取。本片 0 网络调用。

## 实现

- `public_web_core_fetch.py`（新）：治理记录/类、契约与 hash、`PublicWebCoreFetch`（descriptor 一次、按 host 的 profile /
  price / rate policy / runner manifest，通用 `ConnectorRunnerAdmissionGate` + wire 0.1 runner request、durable replay、
  receipt 核对 envelope 与 body hash、从 ArtifactVersion 读 media type）、`url_authority_from_discovery`、
  `count_recent_public_web_fetch_calls`、闭合的 `PublicWebFetchManifest 0.1`。
- `public_web_fetch_launcher.py`（新）：单槽 launcher，`start`（human）/`start_bounded_probe`（automation）/`status`/
  `read_completed_manifest`，票据目录 `<state>/fetches/`，前缀 `public-web-fetch:`。
- `public_web_fetch_cli.py`（新）：子进程；`--fake-page-file` 排练 transport 或 `--allow-network` 真实公网。
- `mission_source_discovery.py`：web 协调器接受 fetch launcher；`_document_in_authority` 按来源分派
  （`public_web_urls_in_authority` 只认绑定 `fetch_get` 调用的 complete/partial envelope）；预算把 fetch 调用计入；
  未配置 launcher 时如实 `unconfigured` + `queued`。
- `connector_governance.py` / `connector_quota_policy.py`：新 kind 与配额行。
- `writer_server.py`：`web_fetch_launcher` 接入 web 协调器；human-only `acquire_public_web_document` /
  `public_web_fetch_status`；错误分类；CLI `--web-fetch-governance` / `--web-fetch-user-agent` / 两个 rehearsal 参数。
- `macos_launchagent.py` + `install.sh`：seed-once proposed 记录并传给 writer。
- `document_extraction.py`：非 AlphaEngine 审阅的拒绝文案指向 P9d-4c。

## 验收

- 新增专项 **9 条**（`tests/test_public_web_fetch_lane.py`）：治理与 CLI 批准；按 host 的 profile 链（v1/v2）、原始字节、
  manifest、free replay、`urls_in_authority`；未引用 ref / 篡改 authority / HTTP 500 fail closed；proposed 拒绝；协调器
  搜索→获取→`acquired`→人工队列全周期与预算合计；失败获取记录并按间隔重试、AlphaEngine 账本不受影响；真实 launcher
  + 子进程（human 成功、automation 在 `probe_only` 被拒且 0 调用、未知 ref 被拒、票据/清单 0600、清单交叉核对）；
  writer ops（tick 自动获取并登记审阅、单槽冲突返回 `conflict`、human 二次获取为新的一次真实获取）。
- 既有邻接 108 条（发现、writer ops、service、contracts、治理、抽取、live MCP、web search、public web）原样通过；
  配额清单测试按新行更新；P9d-4a 两条断言随 lane 落地改写。
- 全仓 unittest **1141/1143**：两条失败为既有 `test_document_extraction_preflight` 路径断言（macOS `TMPDIR` 的 `/var` 与 sqlite 回报的 `/private/var`），在基线上原样复现，与本片无关。`git diff --check`、compileall 通过。wheel/sdist 构建、干净 venv `pip --no-index --no-deps` 安装后以 installed package
  跑 27 条 web 专项通过；wheel SHA-256 `f0fdc6527962f80dce9da8d9b9125de38acc3dc16d8d7d69cb15fb12554ad19a`。
- live Core 只读副本 canary `scripts/run_p9d4b_public_web_fetch_canary.py`（`temp/p9d4b/live-copy-canary.json`，
  `ok=true`，每项条件具名）：live v2 下搜索被 `not_connected` 拒绝；副本 v3 `probe_only` 下 owner 搜索得 2 个 URL，
  自动化获取 `not_authorized`，owner 排练获取走真实子进程（fake page，142 字节，`text/html`，1 次调用，0 正式写入），
  settle 为 `acquired` 且审阅登记如实 `not_registered`（probe_only 不授自动化登记）；副本 v4 `connected` 下自动化
  15/15 搜索 dispatch succeeded，新 URL 自动获取→`acquired`→`awaiting_human_extraction`，已在 authority 的 URL 记
  `already_in_authority`，跑到 idle；Claim/Evidence/Thesis 与 AlphaEngine 行数不变，web fetch 调用 0→2，integrity ok，
  0 网络、0 付费、0 live 写入。

## 未做与 owner gate

- **未部署。** 部署后只 seed proposed 记录；live mission 不变，无 URL 可取。
- **P9d-4c：把已获取页面渲染为抽取来源。** `document_extraction._source_context` 仍只接受 AlphaEngine 文档；web 页面需要
  自己的 `verified_source`（fetch manifest + `source:public-web`/`fetch_get` 谱系核对、HTML→文本窗口）。
- **真实 OpenClaw gateway `web_search` handle 未接线**（P9d-4a 遗留），因此真实公网获取尚未发生；首次真实获取需先激活
  搜索，且会真的访问第三方站点（无凭据、只读、按 quota 与计划上限）。
- 激活顺序：owner 批准 `gemini-web-search-v1.json` 与 `web-fetch-v1.json`；发布 mission 新版本改 `source:web-search`
  状态；先 `probe_only` 排练再 `connected`。

## 复跑

```bash
.venv/bin/python -m unittest tests.test_public_web_fetch_lane tests.test_mission_web_search_discovery tests.test_public_web_core_search -v
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/run_p9d4b_public_web_fetch_canary.py \
  --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
  --output temp/p9d4b/live-copy-canary.json
.venv/bin/python -m build --no-isolation --outdir temp/p9d4b/dist
```
