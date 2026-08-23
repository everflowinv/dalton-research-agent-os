# AlphaEngine 完整文档分页 v1

日期：2026-08-23
状态：development candidate；未部署、未跑真实多页 canary

## 结果

本切片把 AlphaEngine 的配额单位和分页单位拆开：`get_document` 每份文档每天只消耗 1 个 document unit，
不是每个 page 消耗 1 次。首页在 ConnectorStore 的 `records` meter 预留并结算 1，续页结算 0；每个 page 仍分别
记录 physical call 和 raw response bytes。80 份文档/日因此落在 `records=80`，`calls=1,600` 只是
80 份 × 最多 20 页的内部安全阀，不代表供应商按页计费。

新增 `AlphaEngineDocumentAcquisitionCoordinator` 和三份闭合 contract：

- acquisition plan 冻结 document ref、AlphaEngine source/bridge hash、最多 20 页、单页响应字节、总响应字节和
  文档字符上限；
- page request 为每页生成稳定 id/hash，首页 cursor 为空，续页 cursor 必须等于上页 `next_offset`；
- acquisition manifest 绑定每页的 RunnerResponse、Invocation、Profile、CallSpec、PhysicalAttempt、Usage、Cost、
  QuotaSettlement、raw Artifact 和 SourceEnvelope ref/hash，以及最终 assembled spool object。

Coordinator 不信任 page port 返回的摘要。每页完成后，它重新读取 immutable authority 和 exact raw JSON-RPC bytes，
核对文档 id、content hash、content chars、offset、returned chars、cursor、provider request id、raw hash、usage 与
quota settlement。只有终页出现、所有 offset 连续、拼接字符数等于 `content_chars`，且 UTF-8 SHA-256 等于
`content_sha256`，manifest 才能标为 `complete`。触及页数、总响应字节或文档字符上限时只保留 `partial` prefix；
页面失败时保留失败 authority，不伪造完整文档。

Page request identity 在 replay 时不变。若 ConnectorRunner 已经完成该页，它返回 duplicate receipt，Coordinator 会
归一到原始 fresh response authority；测试中的 crash 发生在第二页 provider observation 之后，恢复时第一页和第二页
都不重复调用上游，最终 manifest 与再次 replay 完全一致。

## 验证

- AlphaEngine acquisition、Connector quota、live MCP、Connector authority、credential、contracts 与 packaging：
  91/91 通过；
- acquisition 专项：5/5 通过，覆盖三页完整文档、首页 1/续页 0、crash replay、跨页 hash drift、页数/字节上限和
  失败页的保守 document quota projection；
- `compileall`、全部 contract JSON 解析和 `git diff --check` 通过。

## 边界

- 没有调用真实 AlphaEngine；测试使用 deterministic PagePort，但每页都必须满足与 live ConnectorRunner 相同的
  immutable authority 关系；
- 没有把 manifest 写入 live Core，也没有 production Catalog/profile/grant、Evidence、Claim 或 Thesis mutation；
- 现有 live bridge 仍只执行单个 page。把 page request 交给生产 ResearchPlan/Scheduler、再做真实完整文档 canary，
  属于部署接线与 canary gate，不在本地 coordinator 内伪造；
- Gemini web search 与独立 web fetch 尚未实现，是下一开发切片。
