# ClaimIndex Ledger 状态投影

日期：2026-08-15

状态：隔离开发候选已完成本地验证；未部署，未接 Agenda 或现有 cron

## 结果

Slice 2 已删除 ClaimIndex 的 caller-provided status 路径。`build_claim_index` 现在必须读取一个
`DaltonStore`，并由 Core 在同一 SQLite read transaction 内生成 `ClaimIndexLedgerSnapshot`。即使
Ledger 为空，fixture 和 canary 也必须走真实空快照；调用方提供 claim bundle、status、snapshot ref 或
snapshot hash 都会失败。

快照绑定全部 immutable ClaimVersion、EvidenceRelation、numeric challenge 和每个 exact ClaimVersion
的最新 Adjudication。Core 从快照内容派生 snapshot ref/hash，ClaimIndex 再把两者写入自己的 identity 和
content hash。snapshot wrapper、relation、challenge、adjudication 与内层 version/hash 会逐项重验；只重算
外层 hash 不能掩盖内层篡改。

状态投影顺序固定为：旧 ClaimVersion 先标 `superseded`；当前版本若有 exact numeric conflict，标
`contested`；否则读取只属于这个 ClaimVersion 的最新 adjudication；没有裁决时为 `proposed`。旧版本不会
继承新版本的裁决。`updated_at` 取实际改变索引条目的最新 claim、replacement、conflict、adjudication 或
relation 时间，不再永远停在 Claim 创建时间。

## 兼容与精度

- 默认索引每个稳定 claim ref 的最新版本；审计调用可显式选择历史 exact ClaimVersion；
- ClaimVersion 0.2 的结构化 period 以 canonical JSON 字符串无损投影到现有 ClaimIndex 0.1 wire；
- 0.2 numeric semantic key 加入 currency/scale，避免不同币种或量纲被误判为冲突；
- 0.1 的五字段 semantic key 保持不变，历史 challenge 仍可重放；
- 0.1 JSON number 与 0.2 canonical Decimal text 使用 Decimal 语义比较，`100` 与 `"100.0"` 不会被误判为冲突；
- ClaimIndex 仍是可整体重建、可删除的只读投影，不进入 Ledger authority。

## 验证

- ClaimIndex/Human Review/coordinator/verifier/Ledger 组合：41/41 通过；
- Python 全量：385/385 通过；OpenClaw broker：15/15 通过；
- `compileall`、92 份 contract JSON 解析、15 份 packaged SQL 和 `git diff --check` 通过；
- Python 3.13 两次 wheel 逐位一致，SHA-256 均为
  `ed4ea1194580c8b99d522fc42bfaac0152303f9033093986851ed5e00f52bf7e`，586,078 bytes；
- wheel 在干净 Python 3.13 venv 安装成功，`pip check` 无冲突；空 Ledger 构建、snapshot contract 和
  packaged SQL 读取均通过。

Python 全量测试仍会打印既有测试夹具未关闭 SQLite connection 的 `ResourceWarning`；385 项全部通过，
本轮 41 项组合测试没有 warning。

## 未完成项

- 代码没有部署，没有修改 live authority、Agenda、scheduler 或现有 cron；
- ClaimIndex 只解决 Claim 检索投影，原始 Artifact 正文仍没有全文检索面；
- ContextPack 仍缺 refs → 正文的 materializer，AgendaCoordinator 也还没有迁移到统一路径；
- Question Backlog、Planner、Interrupt 和 Reflection 继续按冻结顺序后续实现。

## 下一步

下一笔开发 DocumentIndex FTS5：从 ArtifactVersion 的 title、source metadata 和抽取文本生成可重建只读投影，
并保留 company、source type、content type 和日期分面。DocumentIndex 验收后，再单独开发 ContextPack
materializer，不把两笔合并。
