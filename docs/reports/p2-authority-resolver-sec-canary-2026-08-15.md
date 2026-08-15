# P2 Authority Resolver 与 SEC Public Canary

日期：2026-08-15

状态：隔离开发候选已跑通；未部署，未接 Agenda 或正式 Research Ledger

## 目标与结果

本轮把上一阶段的 fixture-only verifier 接到真实 Connector authority，并用公开 SEC submissions API 跑一条
无凭据、只读 WorkOrder。Microsoft `CIK0000789019` 的 2025 年 10-Q 窗口返回 3 条 filing；流程完成
connector → persisted authority → authority resolver → source/numeric verifier → candidate staging，最终状态为
`human-review-ready-candidate`。CandidateClaim 的 `semantic_verification_status` 仍是 `unverified`。

canary 命令：

```bash
python3 scripts/run_public_sec_authority_demo.py
```

脚本只访问 `data.sec.gov`，不接收 credential handle。Core、Connector、Scheduler、Coordinator、Catalog、
candidate staging 和 raw spool 都在内存或临时目录中创建；脚本不打开 live 数据库，也不保存 SEC 原始响应。
本次返回的 filing authority refs 是 `0000950170-25-010491`、`0000950170-25-061046` 和
`0001193125-25-256321`。

## Authority resolver

`ConnectorAuthorityResolver` 是只读 join。它从六个独立 authority/scratch 边界重读并重算：

- ExecutionInvocation、ConnectorProfile、CallSpec、ConnectorInvocation 和 WorkOrder；
- Runner 实际请求、AdapterRequest、transport observation、commit context 和 RunnerResponse；
- physical attempt、Reservation、最新 Usage/Cost/Settlement；
- ArtifactVersion/raw bytes、SourceEnvelope、ResultEnvelope、formal Scheduler result/event chain；
- plan、ContextPack、coordinator request/receipt 和完整 ResearchCheckpoint chain；
- blocking incident、source health、source/schema/policy、时间与 completeness。

resolver 会从 raw spool 重读 provider bytes，用冻结的 SEC normalizer 再解析一次，并与 journal 中的 normalized
observation 精确比较。它不接受调用方提供的 hash 作为证明。raw 篡改、source type 换绑、实际 RunnerRequest
换绑、旧版 receipt、开放的阻断状态、旧 correction、部分结果和缺失父链都会拒绝。

## 合同分界

新增三份 closed schema：

- `AuthorityResolution` 0.1：保存 resolver 已复核的完整 ref/hash 摘要；
- `AuthoritySourceVerificationMaterial` 0.2：明确区分 normalized payload 与原始 provider bytes，并从可信
  Profile 派生 `source_type`；
- `ConnectorCompletionReceipt` 0.2：同时绑定 coordinator 的 plan request 与 Connector Runner 的 actual
  request。两者的 CallSpec hash 语义不同，不能合并或互相冒充。

旧 `ConnectorCompletionReceipt` 0.1 schema 和 validator 继续兼容 fixture coordinator。真实 authority resolver
只接受 0.2 成功 receipt。coordinator scratch 新增 append-only request/receipt 表，checkpoint 不再是唯一副本。

## SEC public adapter

adapter 固定 GET `https://data.sec.gov/submissions/CIK{cik}.json`，只发送可见 User-Agent 和 Accept header。
SSRF、DNS pinning、redirect、body size 与 credential-shaped channel 继续由既有 `PublicHttpTransport` 处理。

normalizer 只在请求窗口完全落入 `filings.recent` 覆盖范围、结果没有超过 limit 时声明 `enumerated`。它把
10-Q/A 计入 10-Q 查询，但 amendment 必须有显式 revision authority；不能从名称猜测 revision edge。
重复 accession、非法日期、unsafe primary-document path、列长度漂移和不明 amendment 全部 fail closed。

## 安全边界

- CandidateStagingStore 仍没有 DaltonStore 或 Research Ledger handle；
- source/numeric pass 只证明来源链和数值/metadata，不证明 claim 叙述语义；
- synthetic canary approval 只存在于隔离内存 authority，不是生产 capability promotion；
- 没有部署、Agenda 接线、凭据读取、Evidence/Claim/Thesis commit、外发或 cron cutover。

## 验证

- authority/SEC/Agenda 回归专项：8/8 通过；
- 相关 connector/coordinator/verifier 回归：41/41 通过；
- 真实 SEC canary：HTTP 成功，3 条 filing，candidate staging 成功；
- Python 全量：370/370 通过；OpenClaw broker：15/15 通过；
- `compileall`、88 份 contract JSON 解析和 `git diff --check` 通过；
- Python 3.13 两次 no-build-isolation wheel 逐位一致，SHA-256 均为
  `d85ad4ecb466a18f3447549a3765f6561eba025a6b8bbed33baee3469dec22ae`，557,781 bytes；
- wheel 在干净 Python 3.13 venv 安装成功，`pip check` 无冲突；新增模块、14 份 packaged SQL 和 88 份
  packaged contract schema 均可读取；
- Python 3.11、Python 3.13 与 broker 远端 CI 在最终提交后补入本报告。

全量测试首次运行遇到既有 Agenda fixture 的 1 天 availability TTL 穿过当前墙钟边界；该测试改为 365 天固定
TTL 后恢复确定性。产品 Agenda 路由和生产 TTL 没有改动。

## 下一步

增加独立、可审计的人工 review authority/入口。正式 Evidence/Claim/Thesis commit、AgendaDecision 接线、
生产 connector promotion、部署和旧 cron cutover 继续保持人工 gate。
