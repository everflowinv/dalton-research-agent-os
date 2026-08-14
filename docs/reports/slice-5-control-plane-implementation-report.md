# Dalton Research Agent OS · Slice 5 控制面实施报告

日期：2026-08-14

## 结论

本轮把四项长期需求拆成 Core 权威、可信桥接插件和只读投影三层。Core 负责可审计的模型选路、能力发现、用量/成本和状态；OpenClaw 只保留认证与 provider 桥接；网页只读取可重建投影，不拥有研究状态或写权限。

这一边界避免两个错误：Dalton 不复制或读取 OpenClaw 的 OAuth/token；dashboard 不靠模型自报任务状态、成本和产物。

## 1. 模型选择、切换与 OpenClaw OAuth

### Core

- `ModelEndpointProfileVersion` 固定 exact provider/model/family、adapter、credential slot、能力、上下文、可用性、价格和硬限制。
- `ModelRoutingPolicyVersion` 固定允许的 profile/provider/family/adapter、模态、独立性和确定性排序偏好。
- `ModelRouter` 按 WorkOrder、预算、上下文、credential slot、可用性和 verifier 独立性选路；initial/retry/switch 都追加 `ModelRouteDecision`。
- provider 不得在一次调用内静默 fallback；换模型必须形成新决策和新 invocation。

### OpenClaw 可信桥

- 新建未安装的 `dalton-openclaw-model-broker` spike，只调用 OpenClaw host-owned `api.runtime.llm.complete`。
- broker 固定 dedicated agent 和 exact model allowlist；客户端不能传 agent、base URL、header、API key 或 auth profile。
- 每次调用带 HMAC admission、时窗和 nonce；broker 重启后用 owner-only journal 保持 fresh/duplicate/conflict/indeterminate，崩溃中的 `pending` 永不自动重放。
- Dalton 只收到实际 provider/model/agent、标准化 usage、可用时的 cost 和内容 hash；不读取 `openclaw.json`、认证数据库、OAuth token 或 refresh token。
- `OpenClawModelAdapter` 把已接受的 route decision 投影成闭合 UDS 请求，校验返回 attribution、usage、预算和 canonical hash，再生成未提交的 `ModelInvocation + ResultEnvelope`。

OpenAI 官方文档确认 Codex app-server 支持 ChatGPT-managed OAuth，但本架构不让 Dalton 直接连 app-server；认证选择、持久化和刷新仍由 OpenClaw 管理。

## 2. 人类监督看板

- `WorkflowRunVersion + WorkOrderLink` 记录总任务和任务树。
- Scheduler 是执行状态权威；模型自报的 `WorkOrder.status` 不能覆盖页面状态。
- `DashboardProjector` 只用 SQLite `mode=ro + query_only` 读权威库，生成一次性 projection DB。
- 租约已过期但尚未 sweep 时显示“等待调度回收”，不继续显示“执行中”。缺数据、未定价和无法可靠关联都会显示 `partial_data` 和 warnings。
- 网页只绑定 loopback，只开放固定 GET API；不返回 prompt、完整输出、raw usage、storage locator 或 credential。
- 页面用通俗中文显示总任务、最近工作、模型 token/成本、能力状态和产物 metadata。

视觉验收截图：`temp/dalton-dashboard-slice5/dashboard.png`。

## 3. Skill / MCP 按需调用

Core 新增 `CapabilityCatalog`：

1. `search` 只查轻量摘要；
2. `describe` 只展开命中的一项能力；
3. `prepare` 重新检查 catalog epoch、版本/hash、可见范围、政策、WorkOrder、权限和 side effects，再签发短租约。

目录只保存 credential slot ref，不保存 credential；prompt 目录只含 id、名称、摘要和状态。发布 descriptor 必须绑定可信 Registry human approval receipt，prepare 必须读取有效 governance policy；每次实际使用还要重验 lease 的 expiry、epoch、revision、principal、WorkOrder、权限和 side effects。Skill instruction 和 MCP tool schema 只在命中后加载。当前完成的是通用目录、治理和租约边界；OpenClaw skills/MCP 的元数据导入器及 Guidepoint 实际执行 adapter 尚未接入，不能把本轮描述成 live connector 已上线。

## 4. Token 与成本监控

- `UsageEntry` 绑定真实 `ModelInvocation`，分别记录 input/output/reasoning/cache tokens、耗时、请求和字节指标。
- 未知用量用 `null`，不能冒充 0；provider 计量和 worker 自报保留不同可信来源。
- `PriceRateVersion` 固定生效区间和整数 micro-unit 价格。
- `CostEntry` 绑定 exact usage 和 exact rate，由服务端用 `half_up` 规则复算；校正只追加新版本。
- 不同币种分开汇总，不使用当前价回算历史调用。

## Core 与插件的分工

放在 Core：Router、RouteDecision、CapabilityCatalog/Lease、Scheduler、Usage/Cost authority、Workflow/Artifact authority、dashboard projection。

放在可信插件或 adapter：OpenClaw OAuth/provider broker、未来 OpenClaw skill/MCP 元数据导入与实际 call、credential resolver、外部 sandbox runner。自生成工具仍须经过 proposal → fixture/evaluation → 独立验证 → 人工批准 → promotion，不能自己给自己扩权。

## 当前验证

- Dalton Core 全量回归：165/165。
- JSON Schema：34 份全部解析。
- OpenClaw broker：Node 测试 14/14。
- wheel 构建、隔离安装、公开导入和 package data 验证通过；含 34 份 schema、7 份 SQL 和 dashboard HTML。
- wheel SHA-256：`42e9fc7a7ed3208c6bafaf45866cefd4a546b4b4fbe8e49607ca2dccfec5f92a`。
- 敌对审计先后拦下 broker 认证/持久幂等、route authority、Catalog governance/use-time gate 和 projection hardlink TOCTOU；修复后最终复审未发现 P0/P1。
- 未安装 broker、未修改 OpenClaw 配置、未调用真实模型、未接 live Coverage OS。

## 尚未完成

1. 把 broker 作为 OpenClaw 插件安装到专用 Dalton agent，并用两条 exact allowlisted model 做非敏感 fixture smoke；这会改变认证/模型配置，须另行受控上线。
2. 实现 OpenClaw skill/MCP metadata importer，以及 Guidepoint 的 lease-aware adapter；adapter 只能拿 scoped credential slot，不能拿 Core DB。
3. 给 dashboard 增加 command service 后才能做暂停、取消和审批；当前页面严格只读。
4. 把 capability attestation 接入 promotion 的强制 gate，并部署真正的 hostile-code sandbox。

生产边界：broker 与可信 controller 可以共享可信 OS identity；普通 worker 必须使用不同 OS/container identity。HMAC 和 mode-0600 文件只负责跨身份认证，不能防御同 UID 恶意进程。

## 相关文件

- Core 规格：`scripts/dalton-core/SPEC.md`
- Core 代码：`scripts/dalton-core/src/dalton_core/`
- Broker spike：`scripts/dalton-openclaw-model-broker/`
- OpenAI 官方认证文档：<https://learn.chatgpt.com/docs/app-server#authentication-modes>
