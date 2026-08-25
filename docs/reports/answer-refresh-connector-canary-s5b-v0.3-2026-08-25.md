# 有限回答刷新 S5B：connector → CandidateStaging 隔离 canary v0.3

## 结果

S5B 隔离 canary 已打通 observed refresh 的候选链。一次合成 SEC `get_company_facts` 响应经正式
ConnectorStore、CapabilityCatalog、Runner admission、transport、SourceEnvelope、raw Artifact、authority resolver、
deterministic verifier 和 CandidateStaging 后，形成 1 条 CandidateEvidence 与 1 条 CandidateClaim。父级有限刷新只写
`ResearchOutcome=observed`、`evidence_observed_for_review` terminal 和 append-only outcome receipt；正式
Evidence、Claim、Thesis 仍为 0。

这里的“真实 connector 链”指 production connector/authority 代码路径，不是外网 canary。上游 HTTP exchange 使用进程内
合成 SEC 响应；本切片没有访问网络、调用付费模型、打开 live 数据库、部署 `:8793` 或修改 production pointer。

## SourceEnvelope gate 加固

S5A 只会从 CandidateStaging 重读 CandidateEvidence、CandidateClaim 和 stage receipt，再把 caller 提交的
SourceEnvelope ref/hash 与 CandidateEvidence 对比。caller 和 CandidateStaging 如果同时引用一条不存在的 SourceEnvelope，
旧 gate 没有独立的 Connector authority 可供复核。

`AnswerRefreshControlPlane` 现在要求 observed finalize 同时拿到只读 connector receipt authority，并执行以下检查：

- exact SourceEnvelope 必须能从 Connector authority 读取；id、保存的 hash 和重算 hash 全部一致；
- SourceEnvelope 绑定的 raw ArtifactVersion 必须能从 Observability authority 读取；
- CandidateEvidence 的 artifact ref/hash 必须包含这条 exact raw ArtifactVersion；
- CandidateEvidence、CandidateClaim、SourceEnvelope、ResultEnvelope 和唯一 CandidateStaging request receipt 必须互相一致；
- 同一候选绑定若意外对应多条 stage receipt，停止而不是任取第一条。

canary 先故意不给 connector receipt reader。Core 在写 ResearchOutcome 和 terminal 前拒绝；随后交入 exact reader 才完成
observed finalize。重复 finalize 返回原 outcome receipt，没有重复 terminal、outcome 或正式写入。

## 隔离执行

脚本：`scripts/run_isolated_answer_refresh_connector_canary.py`

执行结果：

- connector 四段状态：`admitted → admitted → admitted → complete`；
- physical connector attempt：1；CandidateStaging request：1；
- outcome：`observed`；terminal：`evidence_observed_for_review`；
- CandidateClaim 的 `semantic_verification_status` 仍为 `unverified`，没有借 canary 自动升级；
- answer Core 正式 Evidence/Claim/Thesis 计数前后不变；connector Core 三张正式表均为 0；
- answer Core、connector Core、CandidateStaging 三个 SQLite `integrity_check` 均为 `ok`；
- external network、paid model、live database write 均为 0。

canary 首轮还核出两个真实接口要求：Scheduler 会把 ResultEnvelope 按合同规范化后再计算正式 hash，Bounded Planner 的每个
match 必须包含非空 `source_location`。脚本现读取 Scheduler 保存的正式 hash，并提交带 exact SourceEnvelope location 的
match；没有绕过这两个 gate。

## 边界

本切片仍没有给 Cockpit 增加 dispatch endpoint。canary 使用隔离的 human actor 和测试 worker，连接的是预先创建的
bounded refresh WorkOrder 与预先批准的四段 connector 计划；它证明 connector 产出的候选可以安全关闭 observed refresh，
不代表任意 question 已能自动生成新计划。

CandidateClaim 仍需走原 human review/promotion gate。`evidence_observed_for_review` 只表示“候选材料可供审阅”，不表示旧回答
已经刷新，也不表示新 Claim 正确、充分或可以直答。

## 验证

- Answer Routing：13/13；
- S5B connector canary：1/1；
- S4 ACN canary、Contracts、Packaging：12/12；
- connector receipt → SourceEnvelope → Artifact binding：1/1；
- Python `compileall`、全部 JSON contract 解析和 `git diff --check` 通过。

本机 Python 3.13 仍缺可执行的 `build.__main__`，`python3.13 -m build --sdist --wheel` 没有运行起来，因此本轮没有
生成 sdist/wheel。这个限制与 S5A 相同，packaging manifest test 已通过。

## 下一步

下一切片再评估 Cockpit 显式 human dispatch。若开放，endpoint 必须提交 exact RouteDecision ref/hash、subject、question、
actor 与 current-day decision，并复用现有 `AnswerRefreshControlPlane.dispatch`；UI 不能创建 ProbeTemplate、Bounded Loop、
connector 计划或扩大日预算。独立 ad-hoc research 继续保持关闭。
