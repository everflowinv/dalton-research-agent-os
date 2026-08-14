# Dalton Core Slice 4 实施与验收

> 日期：2026-08-14  
> 状态：已放行；未接入 live Coverage OS

## 1. 本轮完成

### Scheduler

- immutable WorkOrder 入队，enqueue 使用 fresh / duplicate / conflict 三态；
- `BEGIN IMMEDIATE` 原子 claim，两个连接争抢同一 attempt 只能一个成功；
- append-only attempt events 与 lease revisions，不维护可被覆盖的 queue row；
- owner + 高熵 token 绑定，数据库只保存 token SHA-256；
- 可信 clock、受限续租、过期回收、迟到结果拒绝和 retry exhaustion；
- max attempts、lease 和 renew 限额按 WorkOrder 入队时冻结的 policy version 执行；
- completion 由服务端重算 ResultEnvelope hash，幂等 key 绑定 work / attempt / owner /
  token / result；每个 WorkOrder 最多一个 formal result。

### Capability attestation

- 新增闭合 `CapabilityAttestation` 契约，固定 proposal、artifact、dependency lock、
  environment/image、policy、fixture manifest、runner、grants、limits、observed effects、
  observed usage、stdout/stderr 与结果 hash；
- `TrustedLaunchContext` 与 `UntrustedSandboxReport` 分开，report 不能注入 runner、policy、
  grants、limits 或启动身份；
- Registry、JSON Schema 与 attestation 全部使用 64 位 lowercase SHA-256；
- `fixture_manifest_hash` 进入 immutable CapabilityProposal 和 proposal canonical hash，
  只保留相同 fixture ID、却替换 input/expected-output 的做法会被拒；
- 标记 `passed` 的 attestation 必须同时满足 wall-clock、reported duration、peak memory、
  stdout 和 stderr 限额；网络、凭据、Core DB 或未声明写入会 fail closed。

## 2. 敌对验收

第一轮审阅拒绝放行，发现旧任务可被新 Scheduler policy 放宽、Registry hash 契约过宽、
fixture 输入输出没有被权威记录锚定，以及 resource limits 只记录不执行。修复后第二轮：

- 全量 unittest：**106/106**；
- Scheduler 定向：**11/11**；Capability attestation 定向：**9/9**；
- `compileall`、JSON Schema 解析、`git diff --check` 通过；
- 独立敌对复审未发现残余 P0/P1；
- wheel 构建、隔离安装、installed demo、`Scheduler` / `CapabilityAttestation` import 通过；
- wheel 包含 **21 份 JSON Schema** 和 **3 份 SQL schema**；
- wheel SHA-256：
  `74771207797d84fedb53f8939a55675965a6ebb4dc58b57a9b34ced8cefd3424`；
- Slice 4 source 不引用 `workspace-chem` 或 live DB path。

## 3. 尚未实现的边界

`CapabilityAttestation` 是证据契约和验证器，不是 sandbox。它目前也没有成为 Capability
Registry promotion 的强制 gate。真实自生成代码上线前仍需：

1. 独立 OS identity、container/VM 或等价隔离服务实际执行权限；
2. 由受限 writer 接受可信 sandbox runner 的 attestation，不能接收调用方自造 wire；
3. promotion 在事务内校验 attestation、policy、proposal/artifact/fixture manifest 和
   builder/evaluator independence；
4. scheduler 对外服务绑定 authenticated worker identity，外部 runtime 仍不能拿 DB path。

SQLite UDF 继续只作可信 Core 内部的完整性护栏，不是同 UID 恶意进程的安全边界。
本轮没有迁移 live Coverage OS，也没有接真实模型。
