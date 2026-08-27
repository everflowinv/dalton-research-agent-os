# P8b CompanyResearchView 与结构化知识查询 v0.1

日期：2026-08-27  
状态：development candidate；代码、专项/邻接回归（106/106）、隔离 live-copy canary、全仓回归与 broker check
均通过；未部署 live（只读投影，可随下次部署上线）

## 结果

P8b 按 [Phase 8 裁决](phase8-single-topic-autonomous-cognition-loop-v1.0-2026-08-27.md) 落地了最小知识调用层：

1. **`company_research_view.py`（纯投影，无新事实 authority、无新表）**：
   `build_company_research_view(store, company_ref)` 从一个 Core 连接重读 Ledger snapshot
   （`claim_index_snapshot` + `project_claim_status` 派生 proposed/corroborated/contested/superseded/retracted）、
   thesis admission（当前 ThesisVersion + admission candidate 的 template/driver_refs/industry）、
   research backlog（按公司的开放问题）、thesis-impact（assessment + verification verdict）、
   weekly brief issue（覆盖该公司的最新一期）与「最近研究停点」，装配成一个闭合自 hash 的
   `CompanyResearchView 0.1` record。`built_as_of` 是输入 authority 的最大时间戳（非墙钟），
   同一 authority 状态逐字节重建一致；thesis/impact/brief/backlog 未打开 schema 时对应段落为空
   （表存在性守卫），不 fail。
2. **结构化查询**：`query_company_research(store, *, company_ref / aspect / period / status, limit)`
   按公司 / aspect（metric_or_aspect）/ period / 派生状态过滤 claim 行，行携带 immutable
   `claim_version_ref + claim_version_hash`、evidence freshness（最新 `retrieved_at` 与 source_types）。
3. **writer 只读 ops**：`company_research_view`、`company_research_query`（CORE + HUMAN_GOVERNANCE 可读；
   worker 不可）。同时补齐 writer `_error_code` 漏掉的 `ResearchConstitutionValidationError`（P8a 遗漏）
   与两侧映射的 `CompanyResearchView*`。
4. **ContextMaterializer 接手**：视图 claim 的 ref/hash 直接作为 `build_authority_context_pack` 的
   claim input specs；materializer 从 authority 重读并验 hash，产出 token-bounded quoted-JSONL
   ContextPack（测试与 canary 均验证 selected==specs、tokens ≤ 预算）。

## 隔离 canary（live 副本）

`scripts/run_p8b_company_research_view_canary.py` 以只读 backup 复制 live Core 后执行（0 写入、0 网络、
0 付费调用），2026-08-27 结果 `status=passed`：

- 5 家公司全部重建且二次构建逐字节一致；
- **ACN**：thesis `current`（`thesis:acn:ai-reinvention-growth`，medium）、2 条 claim
  （SEC `quarterly_revenue_yoy_growth` + transcript `aspect:new-bookings-direction-local-currency`，均
  proposed）、1 条 open question（thesis-impact insufficient 评估自动登记的 follow-up）、1 条 impact
  （assessment insufficient + verification **pass**）、最近 issue w35、最近停点 `question_recorded`；
- **CTSH/EPAM/IBM**：insufficient、各 1 条 claim、停点 `brief_published`（w35 覆盖）；
- **DXC**：insufficient、1 条 claim、无 issue（w35 早于 DXC 入 lane，正确）、停点 `claim_committed`；
- 查询计数：aspect=revenue growth 5 家、company=ACN 2 条、status=proposed 6 条；
- ACN ContextPack：2 条 claim 全部选中，443 tokens / 7,198 bytes（预算 8,000 tokens / 64,000 bytes 内）；
- 副本 `PRAGMA integrity_check=ok`。

## 验收

- 专项/邻接回归 106/106（视图 5、writer ops 4、weekly brief、coordinator、constitution、
  context materializer、claim index、backlog、thesis-impact control、packaging）；
- 全仓 `unittest discover`：949 / 949（本切片新增 6 个测试）；`compileall`、broker `npm run check` 通过。

## 边界与后续

- 视图是即时重算的投影，不落盘、不建 pointer；消费方（P8c Planner、Cockpit）经 writer op 读取。
- 未包含：claim 历史版本列表（仅最新版 + 派生状态）、跨公司语义召回、DocumentIndex passage offset、
  embedding——按 Phase 8 冻结项继续后置。
- 部署：随下一次 `install.sh` 自然上线（writer 新 ops 无需配置变更）；`dalton-gov` 立即可读。
- 下一切片：P8c Tier 1 Planner 接入 live（Agenda → CompanyResearchView → Bounded Planner →
  probe → verifier → Claim → thesis-impact → Weekly Brief），或按 owner 反馈调整顺序。
