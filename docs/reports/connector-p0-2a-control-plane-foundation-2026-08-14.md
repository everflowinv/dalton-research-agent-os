# Connector P0-2a 控制面底座实施与复核

日期：2026-08-14
部署状态：未部署；未访问真实数据源

## 结论

Connector P0-2a 控制面底座已完成，Fable 5 第三轮独立敌对复核结论为 **Go**。这个 Go 只覆盖闭合
Runner frames、Scheduler/Capability 双 use-time gate、静态 resolver、静态 input validator、Runner
专用 quota reservation 和 authority-derived AdapterRequest。

它不包括 adapter execution、durable runner journal、raw response spool、credential authority、writer RPC、
网络 transport、真实 connector 或 E1。当前代码不会读取 CNINFO、SEC、AlphaEngine 或其他外部数据，也不会
写 Evidence、Claim、Thesis 或 Agenda Perception。

## 已完成

- 新增闭合 `ConnectorRunnerRequest`、`RunnerEnvironmentManifest`、`ConnectorAdapterRequest`、
  `AdapterTransportObservation` 和 `ConnectorRunnerResponse` schema/validator；
- Scheduler 提供 exact-current lease use-time gate；Runner 在 reservation 验证前后分别重读
  Scheduler 与 Capability authority；
- `StaticAdapterResolver` 只使用 operator 注入的 adapter 和 input validator，不从 proposal path
  动态 import、`exec` 或 `eval`；
- live CapabilityDescriptor 必须是 `kind=connector`、`contract.mode=typed_call`、
  `instruction_ref=null`，且 adapter/input/output refs 与 Profile 和 manifest 完全一致；
- AdapterRequest 的 parameters、source、hosts、network policy、schema、deadline、response limits 和 raw sink
  都来自最后一次 authority 重读，调用方不能用修改后的 admission body 注入；
- Runner 专用 reservation 从 exact active policy、Profile 上限和冻结 price book 派生 attempt、calls、bytes、
  records、cost 和 TTL；同一 invocation 只允许一份待执行 reservation；
- P0-2a 只准 `auth_mode=none`，`credential_grant_ref` 固定为 null；authenticated connector 在 credential
  authority 落地前 fail closed；
- JSON Schema 与 Python validator 已统一 auth/credential、public-only 和 redirect 条件语义。

## Fable 5 复核过程

第一轮和第二轮都给出 **No-Go**。复核实际复现并推动修复了以下越权路径：

- 伪造或浅拷贝 `ValidatedRunnerAdmission`；
- reservation 验证过程中续租造成的 TOCTOU；
- 调用方自选 attempt 或低报 reservation；
- 未实现 credential authority 时接受任意 grant；
- 调用方传 raw sink 路径/handle；
- Descriptor source/contract 只做浅层 hash 检查；
- JSON Schema 与 Python validator 语义不一致；
- `max_concurrency=2` 时同一 invocation 产生两份 pending reservation；
- `process` mode 或非空 instruction ref 被误当 typed connector 执行。

第三轮复验中，两个独立 SQLite connection 使用两个 idempotency key 并发 reservation，结果只有一个
`fresh`，另一个被 `ConnectorConflict` 阻断，最终 authority 表只有 1 行。Schema/Python 三组条件探针、
Descriptor mode/instruction 探针及前两轮旧探针均按预期 fail closed，因此第三轮只对 P0-2a 给出 Go。

## 验证

- Fable 相关敌对测试：58/58；
- Python 全量：236/236；
- OpenClaw model broker：15/15；
- `compileall`：通过；
- `git diff --check`：通过；
- 62 份 JSON Schema：随全量 contract/packaging tests 解析并检查闭合性；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel 构建一致，SHA-256：
  `0e198638dcde7b52ccc756715eef20da5ca9a1ead8f91fb72cb1c8d3f2d25881`；
- wheel 隔离安装后可导入 `ConnectorRunnerAdmissionGate`，可创建 16 张 connector 表，SQLite
  `integrity_check=ok`。

第一次隔离导入探针错误地从包根导入未导出的 `DaltonStore`，因此失败；改为从
`dalton_core.store` 使用公开实现位置后通过。这个失败属于验收脚本假设错误，不是 wheel 内容缺失。
本机系统 Python 的 `python3 -m build` 仍缺少可执行的 `build.__main__`；本轮使用 PEP 517 隔离的
`pip wheel` 完成两次确定性构建。

Python 3.14 全量测试偶发报告测试代码未关闭 SQLite connection 的 `ResourceWarning`。它不影响本轮
authority 裁决，但应作为测试清理项保留，不能据此声称资源生命周期已经完全验收。

## 下一步

下一段是 P0-2b recorded transport thin slice，顺序固定为：

1. durable runner request/attempt journal 与 crash/indeterminate recovery；
2. content-addressed raw spool 和 bounded sink；
3. narrow Connector AuthorityPort，把 attempt、artifact、Usage、Cost、Settlement、SourceEnvelope 和
   ResultEnvelope 按既定事务边界写回；
4. recorded adapter 只跑 success、empty、429、timeout/crash fixtures；
5. 再做 Fable 独立复核。

P0-2b 通过前不加真实网络、凭据或外部 connector。之后才进入 metadata importer、SSRF-safe transport、
offline/networked attestation 分界，以及 A 股公告、SEC、AlphaEngine 三条 shadow connector。
