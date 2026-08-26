# S7d-4：Cockpit 读回 Ledger 提升状态，policy 已入库的候选不再让人点 accept（v0.1，2026-08-26）

## 结论

- 2026-08-26 19:21–19:22 UTC owner 在 Cockpit 对 ACN、EPAM 两条 SEC quantitative 候选点了 accept。这两条在 19:02 / 19:03 UTC
  已由 governance policy `policy-2` 自动写进 live Core（`claim-version:1c4f31d8…`、`claim-version:41a60259…`），Cockpit 却仍把它们
  显示成「等待审阅」并给出接受按钮——因为 `list_candidates` 只看 staging 库里的 `human_review_decisions`，不知道 Core 的
  `reviewed_candidate_commits` 已经有 policy 路径的收据。owner 点完之后，review control 的 reconcile 线程每 60 秒重试一次
  `commit_reviewed_candidate`，Core 每次都以 `IdempotencyConflict("review decision or candidate was already promoted")` 拒绝，
  `human_review_commit_events` 从 19:21 到 20:23 UTC 累积 110 条（64 条 `conflict`、18 条 `transport_error`），直到本切片部署才停。
- Core 本身没有脏数据：四条正式 Claim / Evidence 都各只有一份，人工 accept 从未写入第二份。owner 的两条 accept 决定作为不可变记录留在
  staging 库里，commit 状态是终态 `failed / conflict`，Cockpit 现在会把它写成「已由 policy 自动入库；这条人工接受未另行写入」。
- CTSH 那条（19:48 UTC 才 stage，owner 点击时还不存在）现在同样显示「已由 policy 自动入库」，没有按钮。
- 部署 `8357465`（20:24 UTC）后 reconcile 不再重试：事件数停在 110，最后一条 20:23:48 UTC；`pending_commits()` 为 0。

## 做了什么

1. **Core 新增只读 `candidate_promotions`**（`store.py`）：按 `candidate_claim_ref` 列表回读 `reviewed_candidate_commits` 收据，返回
   `authority`（`policy-commit:*` → `policy`，`human-review:*` → `human`）、`review_decision_ref`、`claim_version_ref`、
   `evidence_version_ref`、`promoted_at`。0..500 个 ref，只认 `candidate-claim-version:` 前缀，不碰 staging 库。
2. **writer 暴露该 op**（`writer_server.py` / `writer_client.py`）：加入 `CORE_OPERATIONS` 和 `RESEARCH_REVIEW_CONTROL_OPERATIONS`，
   `OPERATION_FIELDS` 只收 `candidate_claim_refs`。`commit_reviewed_candidate` 对 scoped review principal 的检查从「操作集完全相等」改成
   「是 review 操作集的子集且含 `commit_reviewed_candidate`」——旧 token 文件（只有两项）在 managed subset 模式下仍是合法 review principal，
   只是拿不到它没有的 op。live 的 `research-review-control` principal 由 `dalton-bootstrap` 在部署时刷新到三项（token 不变）。
3. **Cockpit control plane 每次渲染都问 Core**（`research_review_control.py`）：`view()` 对整页候选调一次 `candidate_promotions`，
   每项带 `promotion` 和 `promotion_state`（`known` / `unknown`）；`record()` 在写任何决定前先查，已提升的候选直接拒绝
   （`candidate was already promoted into the Ledger by policy review (…)`），writer 读不到时同样拒绝，不记录决定。
4. **conflict 成为终态**（`research_review.py`）：`pending_commits()` 跳过 head event 为 `failed` 且 `error_code ∈ NON_RETRYABLE_COMMIT_ERRORS`
   （目前只有 `conflict`）的决定；`transport_error` 之类仍会重试。`list_candidates` / `candidate_status` 多回 `commit_error_code`。
   contract 未动：commit event 仍只有 `queued / committed / failed` 三态。
5. **页面文案**（`cockpit_control.html`）：有 `promotion` 的候选显示「已由 policy 自动入库 · claim-version…」或「已由人工审阅入库 · …」，
   若还挂着一条没写进去的人工决定，追加「；这条人工接受未另行写入」；`promotion_state=unknown` 显示「Core 入库状态暂不可读，稍后刷新」。
   三种情况都不渲染接受 / 修订 / 拒绝按钮。

## 验证

- 专项：`tests.test_research_review`（conflict 终态、transport_error 仍 pending、`candidate_promotions` 人工收据回读与参数校验）、
  `tests.test_research_review_control`（policy 已提升的候选被标记且 `record()` 拒绝、writer 不可读时 fail closed、原有 accept 路径只发一次 commit）、
  `tests.test_writer_service`（review principal 可读、worker 被拒、非法 ref 报 `RemoteError`、两项旧 token 在 managed subset 下仍加载）。
- 回归：`test_research_review` + `test_research_review_control` + `test_agenda_control` 32/32；`test_writer_service` +
  `test_transcript_candidate_writer_ops` + `test_research_plan_executor` + `test_research_plan_closure` 54 项中 53 过，唯一失败是
  `test_partial_frame_does_not_block_valid_client_and_connection_limit` 的本机 socket 超时，S7d 之前的 `84ffc70` 就失败，CI 上过。
- live（部署后 20:25–20:28 UTC）：
  - `human_review_commit_events` 停在 110 条，最后一条 20:23:48 UTC（部署前），之后三个 reconcile 周期没有新事件；`pending_commits()` 为 0。
  - writer `candidate_promotions` 对四条候选返回 1 human + 3 policy 收据，`claim_version_ref` 与 Core `claim_versions` 一致。
  - 用 live 配置和 review principal 实例化 control plane 跑 `view()`：ACN transcript = accept / committed / human 收据；ACN、EPAM SEC =
    accept / failed conflict / policy 收据；CTSH = 无决定 / policy 收据；四条 `promotion_state=known`。
  - venv 里的 `cockpit_control.html` 含新文案；token 文件里 `research-review-control` 已是三项，`core` 含 `candidate_promotions`。

## 明确没做

- 没有删除或改写 owner 的两条 accept 决定和 110 条 commit event——它们是不可变审计记录，Cockpit 只是把状态讲清楚。
- 没有让 policy 提交路径反向写 staging 库。Cockpit 的真相来源改成 Core 的 `reviewed_candidate_commits`，staging 库继续只记人工决定。
- 没有在浏览器里点开 Cockpit 复核渲染；页面逻辑靠 `view()` 输出和文案 diff 验证。
- 没有给 `human_review_commit_events` 加重试上限或退避；`conflict` 之外的失败仍每 60 秒重试一次，无上限。

## 待改设计点（不阻塞）

- reconcile 对非终态失败没有退避，writer 忙（例如 SEC lane 子进程跑 6 分钟）时它会持续制造 `transport_error` 事件并占 writer 连接；
  建议加指数退避或上限。
- research plan executor 的 `candidate_staging` 节点在 policy 提交后可以顺手把收据写回 staging 库，让 Cockpit 不必每次渲染都问 writer；
  现在每次 `view()` 一次 RPC，候选数 ≤200，可接受。
