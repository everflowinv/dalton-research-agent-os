# P2 Offline Verifier 与 Candidate Staging

日期：2026-08-15

状态：fixture-only 实现和验证完成；未部署，远端 CI 全部通过

## 本轮目标

把 P2 coordinator 的成功 checkpoint 送入一条离线验证链：重新核对来源、从 raw payload 抽取数值、
用 Decimal 复算，再把通过的 CandidateEvidence/CandidateClaim 写入独立 staging。正式 Research Ledger
仍只接受人工 gate；本轮不接 live connector、不读取凭据、不写 Evidence/Claim/Thesis。

## 合同与验证

新增五份 closed 0.1 schema：

- `SourceVerificationMaterial` 绑定 checkpoint、synthetic SourceEnvelope/Artifact、fixture、raw payload、
  schema、record refs、lineage、四类时间和 completeness；
- `NumericVerificationSpec` 只允许 `identity / sum / difference / ratio`，输入必须绑定 exact material ref/hash、
  JSON Pointer 和 extractor，数值使用 canonical Decimal string；
- `VerificationBundle` 固定 verifier ref/hash，verdict 由 closed findings 推导；
- `CandidateEvidence` 与 `CandidateClaim` 使用 candidate-only identity，并保留 source/numeric verification
  ref/hash；
- quantitative candidate claim 的 value、unit、currency、scale 和 period 必须与 numeric spec 完全一致。

numeric pass 只证明数值和 metadata，不证明 subject、metric、basis 或叙述语义。CandidateClaim 因此固定保存
`semantic_verification_status=unverified`，直到后续人工 review；调用方不能把它改成 `verified`。staging 还会
强制 fixture evidence 保持 `source_type=recorded_fixture`、原始 retrieved time 和 source-derived independence group，
避免 synthetic fixture 冒充 live authority 或伪造来源独立性。

source verifier 会重新验证 plan、ContextPack、step、RunnerRequest、receipt 与 checkpoint 的连续绑定，随后
重放 packaged CNINFO/SEC/AlphaEngine fixture，重算 raw、source summary、artifact、schema、record refs、lineage、
completeness 和时间顺序。numeric verifier 不接受调用方自由填写的数字；它按 JSON Pointer 从同一 verified
raw payload 重新抽取，再按固定 rounding 规则计算结果和 metadata。

## Candidate staging

`CandidateStagingStore` 使用单独的 owner-only SQLite：

- 不导入 `DaltonStore`，不持 Research Ledger handle，也不创建 Evidence/Claim/Thesis 表；
- staging 在 `BEGIN IMMEDIATE` 事务内重新执行 source/numeric verifier，并要求 caller 提交的 bundle 与重算
  结果 canonical equality；伪造 `pass` 无法入库；
- material、spec、verification 和 candidate version 都是 immutable；version chain、source binding、numeric
  binding 和 idempotency 任一漂移都会 fail closed；
- 事务内崩溃全部回滚；commit 后返回前崩溃可用同一 idempotency key 恢复为 duplicate；同 key 不同请求拒绝。

## 本地验证

- verifier/staging 专项：7/7；
- coordinator/verifier/packaging 组合：22/22；
- Python 全量：364/364；
- OpenClaw model broker：15/15；
- `compileall`、五份 JSON 语法检查和 `git diff --check`：通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 Python 3.13 no-build-isolation wheel 逐位一致，SHA-256 均为
  `8930ac9a6574053ccd8c678e2dc80f2c8607631fb4f4cdbecf3480aa579c628a`，每份 526,179 bytes；
- 干净 Python 3.13 venv 安装后 `pip check`、packaged staging SQL、六张表、空库计数和 SQLite
  `PRAGMA integrity_check` 均通过。
- 实现提交 `83e7a5e` 的 GitHub CI `31874505563`：Python 3.11、Python 3.13、broker 全部通过。

全量测试仍会从既有 MCP/reference-shadow 测试夹具打印少量未关闭 SQLite connection 的 `ResourceWarning`；
364 项全部通过，新专项单独运行没有 warning。干净安装验收脚本最初两次分别写错预期表名、调用了不存在的
便利方法，因此断言失败；改为实际表名和 SQLite 原生 `PRAGMA integrity_check` 后通过，产品代码未因这两次
验收脚本错误改动。新增 semantic gate 后的专项首跑还因测试漏导入 validator 出现一次 `NameError`；补齐测试
import 后，专项和全量均通过。

## 当前边界与下一步

P2 coordinator 目前只生成 synthetic SourceEnvelope/Artifact summary ref/hash；它们不是 ConnectorStore 中的
完整 authority record。因此本轮只证明 fixture contract 和 fail-closed staging，不能据此开放 live research。

下一步先实现只读 authority resolver，要求 verifier 从真实 ConnectorStore/Artifact authority 解析 exact record，
再跑一条公告或 filing WorkOrder：connector → source/numeric verifier → candidate staging → human review。
正式 Evidence/Claim/Thesis commit、Agenda 接线、部署、凭据扩权和旧 cron cutover 继续保持 No-Go。
