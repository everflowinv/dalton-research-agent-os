# 首条人工接受 ResearchPlan 正式闭环

日期：2026-08-20
状态：隔离真实 canary 已闭环；开发候选；未部署到 live research path

## 结果

Owner 明确接受 exact candidate
`candidate-claim-version:b0ffc451a57fed093d9c352a1c5699d9ccfb67428b4e21138cd1305eb3a66de7`。
现有 HumanReviewAuthority 随后把该候选无损 promotion 为正式 EvidenceVersion 0.2、ClaimVersion 0.2 和
`supports` relation；Backlog question 从 `in_progress` 进入 `answered`。

- plan：`research-plan:52ba6d0c497e8b2aaedd4cb0e24deeb6`
- review decision：`human-review:cb0e85c70ae680f18e50e9708ae13abad6722b482a326ded88a5ac634161e2ca`
- EvidenceVersion：`evidence-version:e7b661a7eaa93b0929e0c2c3925d0ca11ecec449c5ec3003b10454fc341954aa`
- ClaimVersion：`claim-version:58906eaea44ea390f64a757f301f72607b94fe93cf406b459a5b020b93d501b2`
- relation：`relation:reviewed:e3206753d09e46c60870cc0c1aa332d3ac2ea8037100d44d605a2dcc165266df`
- question：`research-question:066edd1579c660baebeedd1d22290b2d`
- answer binding：`research-question-answer:c7e9c699f758b716a7cc5093d4dfc449`

正式 Ledger 当前有 1 条 Evidence、1 条 Claim、1 条 supports relation 和 1 条 reviewed-candidate receipt；
Backlog 有 1 条 formal answer binding。Claim 初始状态保持 `proposed`，人工语义接受没有被冒充为独立来源交叉验证。
Core、review staging 和 research coordinator 三份 SQLite 的 `PRAGMA integrity_check` 都返回 `ok`。

候选在 `2026-08-20T07:58:51.765397Z` 就绪，review decision 在
`2026-08-20T09:42:16.053469Z` 写入，当前可核验的端到端人工审阅耗时约 1 小时 43 分钟。该数字包含消息等待和
本轮操作时间，不代表纯人工阅读时长。

## 本轮开发

新增 `ResearchPlanClosureCoordinator`，把原先需要人工拼接的最后一段改成可重放控制面：

1. 重读完整四节点计划树与 final candidate staging proof；
2. 要求 final proof 的 CandidateEvidence/CandidateClaim 与 exact accepted decision 完全一致；
3. 重验 review commit event 全链、Core reviewed-candidate receipt、正式 Evidence/Claim/relation 和三条 domain event；
4. 只允许 promotion 得到的 exact ClaimVersion 回答 plan 自己的 Backlog question；
5. 崩溃发生在 answer transaction 之后时，重放收敛到同一 answer binding。

新增 `scripts/close_sec_research_plan_canary.py`。它不创建人工决定，也不执行 Ledger promotion；只有 exact accept
和正式 promotion 已经存在时，才会关闭同一条隔离 canary。对本次 canary 重跑返回 `duplicate`，所有正式 ref
保持不变。

专项测试覆盖：成功闭环、重复重放、accept 后未 promotion、answer 后崩溃、Core promotion receipt 篡改、review
commit event chain 篡改。

## 验证

- Python 全量：543/543 通过；
- OpenClaw model broker：15/15 通过；
- `compileall`、106 份 contract JSON 解析和 `git diff --check` 通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两份 Python 3.13 no-isolation wheel 逐位一致，SHA-256 均为
  `3481869aed3def45731ec609f1d318b527030fea13b766245c944ffb5d0417ee`，每份 701,575 bytes；
- wheel 在干净 Python 3.13 venv 安装后 `pip check` 通过，包根能导入 closure coordinator、错误类型和 review
  commit event validator；
- 对已闭环的真实 canary 重跑 `close_sec_research_plan_canary.py` 返回 `duplicate`，Core、review staging、
  research coordinator 三份 SQLite 的 `integrity_check` 仍为 `ok`。

系统 Python 3.14 和未使用项目 venv 的 Python 3.13 都缺可执行的 `build` 模块；确定性构建最终使用项目现有
`.venv` 的 Python 3.13 完成，没有修改全局 Python 环境。全量测试仍打印一条既有 reference-shadow 测试未关闭
SQLite connection 的 `ResourceWarning`；543 项全部通过，新增 closure 专项单独运行没有该 warning。

## 边界与下一步

- 本次只修改隔离 canary 数据；没有打开 live authority，没有部署服务，没有改 cron，没有扩大 connector 权限。
- review HTML 与 closure controller 仍未部署到 live；现有 live Agenda 也不会自动执行 research plan 或自动写 Ledger。
- v0.6 的首条真实闭环门槛已经满足。下一轮应先记录人工接受/修改/拒绝原因、来源和数字错误、成本与审阅时间，
  用真实质量数据决定补 connector、verifier 或 model；Interrupt / Reflection、旧 cron cutover 和横向内核扩建仍需
  独立价值证明和人工 gate。
