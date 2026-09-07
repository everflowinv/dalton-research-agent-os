# P9d-5：web 链部署上线与 live probe_only 人工排练

日期：2026-09-07
状态：**已部署 live**；live mission 已发布 v3（`source:web-search` = `probe_only`）；
已在 live 上完成一次真实 Gemini 搜索（human 身份），自动化仍被拒。
部署提交：`3db0720`（含 P9d-4a~4e）。上游：[P9d-4e](p9d4e-live-web-search-activation-v0.1-2026-09-06.md)。

## 部署前的事实核对（纠正了状态文档）

部署前比对 live 已安装包与仓库 HEAD：live 已含 **136** 个模块，仓库 **143**，差集**只有** web 链的 7 个新模块。
即 thesis-impact 换版修复、P9d-3a、P9d-3b 早已随 2026-09-06 08:22 的安装上线；PROJECT_STATUS 里
"未部署" 的记述已过时。live thesis-impact LaunchAgent 退出码为 **3**、日志为 `blocked_pending_human` /
`verification_policy_superseded`——正是修复后的停泊行为，不是失败循环。

## 部署前回归验证

在 live Core 只读副本上以**新代码**跑既有 AlphaEngine 发现 canary：真实 launcher + 真实子进程完成一次
human 发现（1 个新文档、1 个已在 authority），发现记录与文档行正常写入副本，integrity ok，0 网络 0 付费。
该 canary 整体报 `ok=false`，原因是它的**预期已过时**（写它时 live mission 还是 v1 `probe_only`，现在是 v2
`connected`），以及 AlphaEngine 24h 预算当前已用满 31/30——不是本次改动的回归。

## 部署

1. `dalton-backup create` 快照 `pre-p9d4-web-chain-20260907`：core.sqlite（23,449,600 B，
   sha256 `f0750fdb…`）与 scheduler.sqlite（2,977,792 B，sha256 `4982b35b…`）。
2. LaunchAgent 补上 broker 接线：web search broker 的 socket 与 key **由 planner 已配置的 broker 路径派生**
   （两者同为 OpenClaw 插件 socket，在同一 state 目录），且**仅在两个文件都存在时**传入；否则网络搜索照旧在
   spawn 前被拒。没有新增配置项。
3. `deploy/macos/install.sh`：重装 wheel、重建四个 plist、重启四个 LaunchAgent。`dalton-health` 返回
   `ok: true, state: running`，11 项检查全 true。
4. 部署后 controller tick 同时驱动两条发现 lane：AlphaEngine 照旧（cadence/预算），web search 如实
   `not_authorized`（当时 mission 仍 `not_connected`）。

## live mission v3

`dalton-gov create_coverage_mission`（actor `human:lumos`）发布
`coverage-mission-version:us-it-services:3`（hash `caae3a13…`）：**只把 `source:web-search` 从
`not_connected` 改为 `probe_only`**，其余 source 状态、`may_write`、预算、绑定全部原样继承。

## live 上的首次真实搜索（human 排练）

`dalton-gov run_mission_source_discovery`（actor `human:lumos`，`source_ref=source:web-search`，
spec `industry-demand`，窗口 2026-07-09..2026-09-07）：

- ticket `web-search-discovery:b3c1300c…`，transport **openclaw-search-broker**，退出 0；
- **1 次 provider 调用**，返回 **10 个去重 URL ref**（spglobal、morningstar、staffingindustry、tikr、
  vertexaisearch grounding 重定向、investor.accenture.com、newsroom.accenture.com、alphastreet、
  livemint、quartr）；
- live authority 新增：1 条 dispatch、1 条 source discovery（`requested_by: human:lumos`，绑定 mission v3、
  spec `industry-demand`）、10 行 `discovered` 文档；
- `formal_authority_writes = 0`；**Evidence 6 / Claim 6 / Thesis 2 前后不变**；`PRAGMA integrity_check = ok`；
- web fetch invocation **0**：没有抓取任何页面。

## 部署后的稳定状态（fresh tick 确认）

- web discovery：`not_authorized` —— "mission marks source:web-search as probe_only; automation discovery
  requires connected"；
- web acquisition（fetch）：同样 `not_authorized`，因此那 10 个 URL **不会被自动抓取**；
- 我那次 human dispatch 已 settle 为 `succeeded`（10 个新文档）；
- AlphaEngine lane 不受影响（其自身 24h 预算已用满）。

即：**web search 现在只对人开放，自动化仍被合同拒绝；页面抓取一次都没发生。**

## 计费与用量

本轮共经 broker 花费 **5 次真实 Gemini 搜索**（4 次在只读副本上诊断/验证，1 次在 live），另有 1 次经 agent
工具取样。计划 24h 上限为 40，mission 预算未被触及（web 计划自带 `max_calls_24h`）。

## 仍未做 / 下一步由 owner 决定

- **未把 `source:web-search` 改为 `connected`**：改了之后 automation 会在 tick 里自动搜索**并自动抓取**已发现
  的 10 个 URL（每 tick 至多一次，受计划 24h 上限约束）。这是下一个真实外部动作，建议单独授权。
- **真实页面抓取一次都没跑过**：fetch lane 在 live 上从未访问过第三方站点。若要先小步验证，可用
  `dalton-gov acquire_public_web_document`（human-only）对**单个** URL 抓一次。
- 抓到的网页目前只能"读"：`mission_document_evidence` 可渲染可核验窗口，模型起草与候选 staging 对网页
  仍关闭（P9d-4c 的边界，需另立一片并复核 ADR-0003）。
- AlphaEngine 24h 预算当前 31/30 已满，属既有软上限边界（多页文档），非本次改动引入。

## 追加：live 上的首次真实页面抓取（human-only，2026-09-07）

`dalton-gov acquire_public_web_document`（actor `human:lumos`）对发现结果中**第一手**的
`newsroom.accenture.com` 抓了一次：

- ticket `public-web-fetch:6a384f54…`，transport **public-https**，退出 0，**1 次抓取**；
- 该 URL 实为 **PDF**：`https://newsroom.accenture.com/content/3QFY24-Earnings/accenture-reports-third-quarter-fiscal-2024-results.pdf`，
  **190,517 字节**、`application/pdf`、字节以 `%PDF-1.4` 开头，已进 Core connector authority；
- live 现有 **1 条 `fetch_get` invocation**；`public_web_urls_in_authority` 对该 ref 返回 true；
- `formal_authority_writes = 0`；Evidence 6 / Claim 6 / Thesis 2 不变。

### 由此暴露的两件事

1. **最有价值的第一手来源常常是 PDF，而抽取来源目前渲染不了 PDF。** 审阅面按设计如实拒绝：
   "fetched page media type application/pdf cannot be rendered as text for review"。字节已在 authority、
   可核验、可重放，但人还读不了它。**要不要支持 PDF 是 owner 的依赖决策**：本仓库运行时依赖几乎为零
   （只有部署用的 `cos-python-sdk-v5`），本机也没有任何 PDF 库；引入 `pypdf` 之类会扩大依赖面，
   自研 PDF 文本抽取则工作量与出错面都不小。在决定之前，PDF 会一直被如实拒绝，不会被猜着读。
2. **human 抓取不推进 mission 账本**（与既有 `acquire_alphaengine_document` 同构）：文档行仍是 `discovered`。
   在 `connected` 下，协调器会为这些**已在 authority 的字节再付一次抓取**。已修（见下）。

## 后续修复：已持有的字节不再重复抓取（P9d-6）

`MissionSourceDiscoveryCoordinator.launch_acquisition` 在启动抓取前先判断该文档的字节是否已在本来源的
authority 中；若是，则经新的 `CoverageMissionAuthority.settle_document_already_held`
（只允许 `discovered → acquired`）直接结算并登记人工审阅，返回 `status: already_in_authority`，
**不花第二次抓取**。新增回归测试：human 抓取后账本仍为 `discovered`，下一 tick 结算为 `acquired`、
登记 review，且抓取次数不变。
