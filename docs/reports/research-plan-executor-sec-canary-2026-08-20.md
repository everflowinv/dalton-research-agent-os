# ResearchPlan executor 与 SEC public canary

日期：2026-08-20  
状态：开发候选；隔离验收通过；未部署到 live research path

## 结论

`ResearchPlanExecutor` 已把人工批准的 SEC public ResearchPlan 依次执行到 connector、authority resolver、
source/numeric verifier 和 candidate staging。真实 canary 的四个节点全部成功，最终停在
`human-review-ready-candidate`；它没有读取凭据或 live DB，也没有写正式 Evidence、Claim 或 Thesis。

## Canary 边界

- plan：`research-plan:52ba6d0c497e8b2aaedd4cb0e24deeb6`
- approval actor：`human:lumos-obliviate`
- source：SEC public submissions，host `data.sec.gov`，`auth_mode=none`
- issuer：Microsoft，CIK `0000789019`
- operation：`list_filings`
- filter：`10-Q`，`2026-01-01..2026-08-17`
- authority：全套临时 owner-only SQLite/raw spool；不打开 live authority
- side effect：只允许 `read:public-http`

四个节点的 admission state 都是 `queued`，attempt state 都是 `succeeded`。coordinator 每次只放行直接子节点，
没有批量或越级 admission。

## 结果

SEC recent submissions 在该窗口内返回两条 `10-Q`：

- `0001193125-26-027207`，filed `2026-01-28`，document `msft-20251231.htm`
- `0001193125-26-191507`，filed `2026-04-29`，document `msft-20260331.htm`

staging candidate：

- ref：`candidate-claim-version:b0ffc451a57fed093d9c352a1c5699d9ccfb67428b4e21138cd1305eb3a66de7`
- statement：`The SEC public 10-Q filing list for CIK 0000789019 in window 2026-01-01..2026-08-17 contains 2 filings.`
- value/unit：`2 records`
- completeness：enumerated official filing list
- semantic status：`unverified`，等待人工 accept/revise/reject

正式 Ledger 行数保持不变：`evidence_versions=0`、`claim_versions=0`、`thesis_versions=0`。四个 canary SQLite
authority 的 `PRAGMA integrity_check` 都返回 `ok`。

## 真实运行发现并修复的问题

第一次 canary 使用超过 SEC recent coverage 的窗口，adapter 正确返回
`normalization_error` 和 `retryable=false`，但 transport 把所有非成功结果统一写成 Scheduler `retryable`。
ResearchPlanExecutor 随后拒绝对已有 physical attempt 的同一 connector invocation 再次 dispatch。

修复后，transport 以 adapter error 的 `retryable` 字段决定 Scheduler 结果：

- `retryable=false` → terminal `failed`，不生成 `retry_at`
- 429、timeout、adapter crash 等 `retryable=true` → 保持有界重试

新增回归验证 normalization failure 的 ResultEnvelope error 原样保留，并直接产生 formal terminal failure；
相邻 connector transport 和 ResearchPlan executor 专项均通过。

## 下一道 gate

人工审阅必须对上面的 exact candidate 作出 accept、revise 或 reject。只有 explicit accept 才能调用
HumanReviewAuthority，把 reviewed candidate 原子 promotion 为 EvidenceVersion 0.2、ClaimVersion 0.2 和
supports relation，再将正式 Claim 绑定到 ResearchQuestionBacklog。当前结果不能冒充已验证 Claim。
