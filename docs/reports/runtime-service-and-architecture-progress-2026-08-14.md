# Dalton 常驻服务与架构进度审计

> 日期：2026-08-14  
> 审计基准：`vision-and-architecture-v0.1.md`、`architecture-debate-and-v0.2-direction.md`、`SPEC.md`、当前源码、当前数据库和本机运行态

## 判断

Dalton 现在有了正确的常驻外壳：`daltond` 和 owner-only writer 由 macOS LaunchAgent 持续运行；空闲时不保留 LLM session。controller 会回收过期 lease、检测权威库变化、重建只读投影、驱动静态看板插件并写健康心跳。

这还不是完整的“自主研究分析师”。当前服务能监督运行状态，不能自己形成研究议程、派发研究 worker、执行独立 verifier，再把验证后的认知变化提交回 Ledger。按最初的产品路线，Dalton 仍处于 **Phase 0 部分完成，Phase 1 尚未开始**。部分 Phase 3—5 的底层契约已经提前实现，但没有串成 live research loop。

旧 OpenClaw Dalton 的资产已经完成影子归档，运行迁移尚未完成。截至本次审计，`dalton-coverage-*` 仍有 10 个启用中的 OpenClaw cron。新架构还没有等价的事件接入、Agenda Engine、worker coordinator 和投递 outbox，因此现在关闭这些 cron 会让现有 coverage 停止更新。

## 本次上线的运行面

### 常驻进程

- `space.lumos.dalton.writer`：独占 `core.sqlite`，只通过 owner-only Unix socket 接受闭合 RPC。
- `space.lumos.dalton.controller`：运行确定性维护循环，不持有模型会话，不领取研究 WorkOrder。
- 两个 LaunchAgent 均启用 `RunAtLoad + KeepAlive`，运行目录、配置、日志和 authority data 都在 OpenClaw workspace 之外。
- controller 和 writer 被强制重启后，LaunchAgent 均恢复为 `running`，健康检查重新通过。

### controller 当前会做的事

- 调用 `Scheduler.sweep_expired()`，按入队时冻结的 policy 回收 lease；
- 比较 Core、Scheduler、Model Router 和 Capability Catalog 的文件签名；
- authority 变化后重建 dashboard projection；
- authority 在投影期间再次变化时，下一轮继续重建，避免漏掉并发写入；
- 插件失败时保留 Core 运行，并按配置间隔重试；
- 原子写入 owner-only heartbeat，记录 sweep、projection、plugin 和错误状态；
- 依赖 LaunchAgent 处理进程崩溃和登录后自启动。

### 静态看板插件

`static_dashboard` 是 Dalton 内建插件，不是 OpenClaw skill，也不允许从配置任意 import Python 模块。它只读 disposable projection DB，把固定 dashboard API 物化成一个自包含 HTML，再发布到腾讯 COS：

- 对象：`dalton/index.html`
- 地址：<https://eve.lumos.space/dalton/>
- ACL：`public-read`
- 缓存：`no-cache, no-store, must-revalidate`
- 发布后回读字节并核对 SHA-256；
- 发布前后核对站点根页和 `kweb.html`，不调用 `put_bucket_website`，不修改现有 `IndexDocument`。

本次按用户决定发布完整的现有监督投影，不再生成第二套脱敏页面。原有 projection 安全边界仍保留：不投影 prompt、credential、raw tool output、storage locator、完整模型输出、writer token 或 authority DB path。公开 HTML 会包含现有 projection 中的 workflow、模型、能力和 artifact metadata。

## 对照最初架构目标

### 已经形成真实边界并有测试覆盖

1. **状态独立于模型和 harness**
   - 独立仓库、独立 runtime state 和独立 LaunchAgent 已落地。
   - OpenClaw 只为六条模型提供受限 broker；Core、Scheduler 和投影不依赖 OpenClaw session。

2. **Research Ledger 与 commit gate**
   - Evidence → Claim → Thesis 的不可变版本链已实现。
   - stage、verify、commit 分段事务、版本化 policy、独立性 predicate 和幂等冲突检查已有 E1 测试。

3. **单写者和外部 runtime 边界**
   - writer service 独占 DB path；worker/verifier 只能使用 scoped token 和分配好的 invocation/work order identity。
   - 这能防止普通 adapter 绕开 Core，但不能防御同一 OS 用户下的恶意进程。

4. **Scheduler policy boundary**
   - immutable WorkOrder、原子 claim、lease/renew、到期回收、bounded retry、迟到结果拒绝和 completion 幂等已实现。
   - 本次 controller 把到期回收接入了常驻运行面。

5. **模型和 capability 控制面**
   - 六条 exact model profile、确定性 route/retry/switch decision、OpenClaw broker HMAC/UDS 边界已实现并做过真实 completion smoke。
   - Capability Registry、attestation contract、Catalog 的 search/describe/prepare 和 use-time lease gate 已实现。

6. **Usage、Cost、Artifact 和人类监督投影**
   - 版本化 usage/cost/artifact authority、只读 projection、固定查询 API 和静态发布已实现。
   - 看板会显式显示缺归属、未定价和 usage 缺失，不把未知值写成 0。

7. **旧资产来源保全**
   - 72 份逻辑 artifact、Coverage DB 快照、旧约束、研究产出、模型、wiki、memory、script、config 和 cron 定义已影子归档。
   - 旧 thesis 仍处于 `quarantined_pending_core_verification`，旧任务仍是 `shadow_only`。

### 已有骨架，但还没有运行闭环

1. **常驻与自监督**
   - 已完成进程存活、lease 回收、投影、静态发布和健康检查。
   - 未完成 workload admission、worker 派发、超时后的自动替换执行、verifier 返工和研究层重新规划。

2. **Execution Fabric**
   - `ProcessRuntimeAdapter` 的 timeout、环境清理、预算、副作用和 envelope 校验已实现。
   - formatter fixture 可以跑通；Pi 只完成 spike，尚无 production worker manager。DeepSeek Harness 仍是实验 adapter。

3. **Verifier 与能力增长**
   - verification、independence 和 capability promotion/rollback 的契约已实现。
   - 没有 operational verifier、真实 hostile-code sandbox、历史回放 runner、上线后 monitoring 或自动 rollback trigger。

4. **OpenClaw 解耦**
   - Core 能在没有 OpenClaw session 的情况下维护自身状态。
   - 真实模型、现有 connector、审批和消息仍依赖 OpenClaw bridge；outbox 还没实现。本次没有停止共享 Gateway 做故障演练。

### 原始设计中仍未实现

1. **Human Mandate 与 steering**
   - 缺 mandate、coverage requirement、priority override/TTL、pause/cancel/approve/emergency-stop command service。

2. **Perception & Event Plane**
   - filing、市场、行业、source health 等仍由旧 OpenClaw cron 生产；Core 没有原生 event ingestion、dedup 和 connector policy。

3. **Research Agenda Engine**
   - 缺 ResearchQuestion、AgendaCycle、candidate/decision、未选原因、预算池和下次重评条件。
   - Dalton 现在不会回答“为什么今天做 A、不做 B”。

4. **Planner & Resource Allocator**
   - 缺从 agenda card 到任务 DAG、模型/能力选择、成本/并发预算和 verifier contract 的 live planner。

5. **端到端研究执行**
   - Scheduler 不会自行派发 worker；controller 也没有 claim-ready → route → execute → collect → verify → commit 的 coordinator。

6. **Operational Verifier 与 Reflection**
   - 缺来源、数字、研究完整性、投资联动检查器；缺 revise work order、reflection record 和 replanning。

7. **Model IR 与 Excel exporter**
   - datapoint、computation、scenario、uncertainty taint、Tier 1/2/3 evaluator、formula census 和 Excel exporter 均未实现。

8. **Artifact Store 与生产存储**
   - 当前有 content-addressed legacy store 和 SQLite authority，但没有完整对象生命周期、备份/恢复演练或生产级外部存储身份。

9. **Human Bridge outbox**
   - 缺 command/event/outbox 协议和幂等补投。OpenClaw 离线时 Core 虽可继续维护，但暂时没有可继续执行的新研究任务，也无法在恢复后补发结果。

10. **真正的隔离**
    - controller、writer、broker 和普通本地进程仍属同一 macOS 用户。面对 hostile worker，需独立 OS identity、container/VM 或带服务身份的存储。

## 原路线所处阶段

### Phase 0：记录和可观察性 —— 部分完成

Ledger、invocation、usage、cost、artifact、dashboard 和 legacy shadow import 已有实现。AgendaCycle、ResearchQuestion、incident、当前人类/cron 选择原因仍未记录，Phase 0 不能算结束。

### Phase 1：Agenda Engine Shadow Mode —— 尚未开始

当前没有自主候选议程、人类选择对照和标注数据。这是验证 Dalton 能否从“执行任务”转成“选择研究”的第一道产品门槛。

### Phase 2：低风险自主闭环 —— 尚未开始

虽然 Scheduler、router、process runtime 和 gate 分别存在，但还没有试点公司上的 live coordinator，也没有一条低风险 WorkOrder 自动完成完整循环。

### Phase 3：Verifier 与 Thesis Commit —— 机制已实现，运行阶段未开始

commit gate 和不可变版本链已有测试；真实研究 verifier、局部返工和政策化自动 commit 还没有上线。

### Phase 4：Skill 自主改进 —— 治理半边已实现

proposal、evaluation、human promotion、registry、attestation contract 和 rollback 已实现；gap discovery、sandbox 执行、历史回放和 monitoring 未实现。

### Phase 5：多 runtime 与规模化 —— 只完成替换边界

native process runtime、Pi/DeepSeek spike 和六模型 broker 证明 adapter seam 可行。多 runtime coordinator、并行资源分配、Postgres/Temporal 均未达到引入门槛。

## 旧 OpenClaw Dalton 的运行迁移

当前 10 个启用中的 `dalton-coverage-*` cron 分为四组：

- `maintenance`、`dispatcher`：最终应由 controller、Agenda Engine 和 Scheduler 生命周期替代；新 controller 只覆盖新 Scheduler 的 lease sweep，尚未管理旧 Coverage DB。
- `market`、`filings`、`industry`、`source-monitor`：应改为无 LLM connector event 或确定性轮询，再进入 Core event ingestion。
- `health`：可由 Dalton health probe 和一致性检查替代，但还缺告警 bridge 和旧 Coverage DB 的对账。
- `daily-report`、`weekly-pack`、`zero-base`：只保留业务截止时间；内容必须来自已验证 Ledger 和 Agenda 状态，不能复用旧 agent payload。

在以下条件全部满足前，保留旧 cron：

1. 每项旧行为已有新的 business purpose、取消条件和 WorkOrder/Capability contract；
2. 新 connector/schedule 只写 event 或 enqueue，不直接改 thesis；
3. native coordinator 能派发、收集、验证并提交；
4. 报告通过 outbox 幂等投递；
5. shadow 结果与旧系统对账；
6. 单项切换后完成观察期和 rollback 演练。

## 建议的下一实施顺序

### P0：把已有内核串成第一条自主研究闭环

1. 冻结并实现 `Mandate / PriorityOverride / ResearchQuestion / AgendaCycle / AgendaDecision` 最小契约。
2. 先运行 Agenda Shadow Mode：读取旧事件和 backlog，生成候选及未选原因，但不派单。
3. 实现 coordinator：`ready → claim → route → worker → ResultEnvelope → verifier → commit/revise`。
4. 先接一个只读 connector 和一个试点公司，跑通有预算、有停止条件的低风险循环。
5. 实现 command service 与 OpenClaw outbox，支持 pause、cancel、approve、emergency stop 和恢复后补投。
6. 做 formula census 和 Model IR 最小路径；正式模型/估值更新继续人工批准。
7. 将旧 cron 逐项改写、shadow 对账、切换和回滚，不做一次性 big-bang cutover。

### P1：扩大权限前补齐安全和运维

1. 把普通 worker 放到不同 OS/container identity；
2. 部署真实 capability sandbox，并把 attestation 设为 promotion 强制 gate；
3. 加 authority backup/restore、schema migration、reconciliation 和灾难恢复演练；
4. 加 model availability probe、成本额度、SLO、告警和 incident ledger；
5. 看板增加鉴权版本；当前 public-read 完整投影按用户要求保留，后续再做脱敏版。

## 本次验收记录

- Python 单元测试：174/174。
- writer/controller LaunchAgent：均为 `running`，`KeepAlive + RunAtLoad` 生效。
- 强制重启恢复：writer 和 controller 均恢复，PID 更新，健康检查通过。
- authority DB：Core、Scheduler、Model Router 的 `PRAGMA integrity_check` 均为 `ok`。
- 公网回读：`https://eve.lumos.space/dalton/` 返回 200、`Content-Disposition: inline`，本地与远端 SHA-256 一致。
- 浏览器验收：页面成功渲染总览、任务、模型、能力和 artifact 区域；当前 projection 明确显示 `partial_data` warnings。
- 站点保护：发布前后根页和 `kweb.html` 哈希一致；未修改 bucket website configuration。
- 未验证：整机重启/重新登录后的自动恢复、OpenClaw Gateway 断开后的 outbox 补投。前者已有 LaunchAgent 配置但本次没有重启整机；后者尚无 outbox 实现。
