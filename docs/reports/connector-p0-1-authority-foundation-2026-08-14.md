# Connector P0-1 authority foundation 实施与独立复核

日期：2026-08-14

## 结论

Connector P0-1 authority foundation 已实现，并通过 Fable 5 六轮只读敌对复核。最终结论为
**技术 Go**：可以提交和推送这批 authority foundation；不能据此部署真实 connector，也不能宣布
完整 P0、P0-1 或 E1 完成。

live 环境仍运行 `6356ceeecf7e937bc1aa6fb20d7635cc4370f792`。本轮没有部署服务、改动 live
authority DB、接入 Agenda Perception、写 Research Ledger 或关闭旧 cron。

## 本轮实现

- `ExecutionInvocation(kind=connector)` 与 ConnectorInvocation exact subtype binding；
- 16 张 append-only connector authority 表和 trusted `ConnectorStore`；
- ConnectorProfile、CallSpec、RatePolicy、QuotaReservation、PhysicalAttempt、Usage、Price、Cost、
  Settlement、SourceEnvelope、Incident 和 SourceHealth 的闭合 contract；
- stable quota scope、exact active policy、调用前 reservation、固定 UTC window 和并发串行化；
- Usage、Cost、Settlement 的 exact attempt/revision binding；
- complete canonical price book：注册时覆盖整个 policy interval，admission 按当前时点复核，Cost 按
  physical attempt 开始时间复核；免费来源使用显式 zero-rate；
- quota、measurement 和 price-book drift 的 durable blocking incident；
- SourceEnvelope 与同一 execution 的 ArtifactVersion v0.2、raw hash、source/schema/policy/provider request
  和 result attempt 绑定；
- ConnectorProposalManifest 的 executable validator、离线 replay 和人工晋升边界；
- wheel 包含 connector SQL schema，隔离安装可直接初始化 authority 表。

## Fable 5 六轮复核

前五轮先后复现并推动修复：

- release/attempt、policy version 和 settlement/Usage 绕过 quota；
- SourceEnvelope 伪造 provenance；
- non-final measurement、actual overage 和跨 window correction 少计；
- attempt 时间、execution environment、price schedule 和保守 cost reservation 漏洞；
- policy 遗漏已生效 PriceRate，以及 Cost 阶段发现短暂价格漂移后 incident 被事务回滚。

第六轮重新运行原始探针，确认：

- price drift 会阻止 Cost，CostEntry 数量保持 0；
- blocking incident 在异常返回后仍已提交；
- 重试不会重复建立 incident，也不会写成功 idempotency result；
- ephemeral rate 失效并跨 quota window 后，incident 仍阻止新 admission；
- public 成功或失败结果都不泄漏内部 deferred-raise marker。

最终批准范围是 **P0-1 authority foundation**。Runner、writer RPC、真实 adapter、use-time lease/source/schema
gate、SSRF、真实 connector shadow 和 E1 仍在后续阶段。

## 验证

- Connector：23/23；
- Python：226/226；
- OpenClaw broker：15/15；
- `compileall`、`git diff --check`：通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel SHA-256 相同：
  `825f07246ed13afff39ac0c5201242ca4e756542332d6590cd40bbb6a6d7a8c5`；
- wheel 隔离安装：16 张 connector 表，SQLite integrity `ok`。

本机系统 Python 的 `python3 -m build` 仍因已安装的 `build` 包缺少 `build.__main__` 不能执行；带
build isolation 的 `pip wheel` 已重复通过。该环境问题没有被写成代码通过，也不阻塞本轮已完成的
wheel 验证。

## Context、Memory 与 Log 裁决

Dalton 不采用跨任务聊天 compaction 作为 authority。Research Ledger 是 durable research memory，现有
ExecutionInvocation、DomainEvent、ArtifactVersion 和 Scheduler 承担执行审计与粗粒度恢复。后续只补：

- derived、可重建的 ContextPack；
- per-attempt RunState/Checkpoint；
- 从 Ledger 重建的 ClaimIndex；
- authority DB 之外的 OpsTelemetry。

transcript、compaction summary、scratch 和 ops log 都不能直接成为 Evidence 或 Claim。

## 下一步

1. P0-2：可信 Connector Runner、CapabilityLease use-time gate 和 exact adapter resolver；
2. P0-3：OpenClaw skill/MCP metadata importer、connector dashboard projection、SSRF 与 attestation 分界；
3. P1：A 股公告、SEC、AlphaEngine 三条 thin connector 依次做 shadow；
4. P2：一条 `connector → verifier → candidate staging → human review` 的只读研究 WorkOrder；
5. 在第一条只读研究 WorkOrder 前冻结 ContextPack、Checkpoint 和结构化 ClaimIndex contract。
