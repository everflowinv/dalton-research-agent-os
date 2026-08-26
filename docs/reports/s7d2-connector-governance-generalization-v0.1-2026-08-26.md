# S7d-2：connector 治理记录泛化 v0.1（2026-08-26）

分支 `s7d-connector-governance`（worktree `/Users/everflow/Projects/dalton-s7d-gov`，基于 main `84ffc70`），已 `--no-ff` 合并进 main `0b0fbc9`。子任务回报里列的 commit 与 worktree 实际 commit（`31cbb59` → `2950248` → `9f723e9` → `882c6cc`）不一致，且子任务没有留下报告文件，本报告由主 session 按合并后的代码补写。

## 结论

`StaticConnectorGovernance` 原来把 AlphaEngine `get_document` 的 capability、schema hash、fixture manifest 写死在 `alphaengine_core_acquisition.py` 里。现在治理记录按 `capability_id` 分 kind，`dalton_core.connector_governance` 维护一个 `KINDS` 注册表（`alphaengine-get-document`、`sec-company-facts`），`ConnectorGovernance` 做通用校验（字段闭集、hash 自洽、`status` / `approved_by` / `effective_from` 语义、fail closed），AlphaEngine 那份 approved 记录的 canonical JSON 逐字节不变。SEC company-facts lane 用同一套 owner 审批 CLI 拿到自己的记录，writer 端（S7d-3）按路径加载，不再各写一份治理逻辑。

## 做了什么

- `src/dalton_core/connector_governance.py`：`KINDS` 注册表按 capability_id 索引；`build_governance_record(kind, approved_by, status, effective_from, max_lease_seconds, version)`；`ConnectorGovernance`（`approval` / `policy` / `policy_hash` / `policy_ref` / `principal_ref` / `approved_by` / `effective_from` / `id` / `content_hash` / `status` / `approved` / `capability_id` / `kind` / `allowed_permissions` / `wire`，`load`）；`load_connector_governance(path)`；`write_governance_proposal(kind, path, approved_by, ...)`。SEC kind 的 `expected_source_hash` / `expected_schema_hash` 来自 `sec_connector_identity(sec_template, "get_company_facts")`，`fixture_manifest_hash` 来自 packaged sec template，permissions 是 `sec_authority_harness.PUBLIC_PERMISSIONS` 的深拷贝；id `connector-governance:sec-company-facts:v1`，policy_ref `policy:dalton:connector-governance:sec-company-facts:v1`，principal_ref `principal:dalton-core-trusted-runner`。
- `alphaengine_core_acquisition.StaticConnectorGovernance` 变成 `ConnectorGovernance` 的薄子类，只额外拒绝 capability 不是 AlphaEngine 的记录；`build_governance_record` 委托通用 builder，签名和输出不变（测试用旧函数的冻结副本断言 `canonical_json` 相等）。
- `connector_governance_cli.py`：`show` / `approve` 按记录里的 `capability_id` 分派；新增 `propose --kind ... --path ... --approved-by ... [--effective-from] [--version]`（0600，拒绝覆盖）。
- `deploy/connector-governance/sec-company-facts-v1.json`：子任务生成时 `effective_from` 是 `2026-08-27T00:00:00+00:00`；lane 把 `governance.effective_from` 当 descriptor 的 `created_at`，为免未来时间戳挡住当天的 live 运行，主 session 用 `propose --effective-from 2026-08-26T00:00:00+00:00` 重生成（与 AlphaEngine 记录一致），再按 Lumos 的 go 执行 `approve --approved-by human:lumos`。最终 `status: approved`，content_hash `e57b25b66458d226cfe7d29bc13e4a81304181370d1c81cfb771dc27b08517f0`（commit `552fa73`）。
- `tests/test_connector_governance.py`。

## 验证

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_connector_governance` → 5/5（子任务回报 14/14，与磁盘上的测试文件不符，以本地实跑为准）。
- 合并 S7d-2 + S7d-3 后：`tests.test_connector_governance tests.test_sec_lane_launcher tests.test_alphaengine_core_acquisition tests.test_alphaengine_acquisition_launcher tests.test_transcript_candidate_writer_ops tests.test_research_plan_executor tests.test_service` → 55/55（593s）；`compileall -q src tests` 通过。
- `load_connector_governance` 读回签核后的记录：`approved=True`、`kind=sec-company-facts`、`capability_id=capability:dalton:connector:sec-edgar`。
- `git diff` 确认 `alphaengine-get-document-v1.json` 一个字节没动。

## 明确没做

- 没改 `writer_server.py` / `macos_launchagent.py` / `deploy/macos`（那是 S7d-3）。
- `PUBLIC_PERMISSIONS` 仍定义在 `sec_authority_harness.py`，`connector_governance` 运行时依赖 harness 模块；没有搬常量。
- AlphaEngine `approve` 路径仍按精确 proposal 形状校验，owner 手改任何字段仍被拒（行为未变）。

## 需要注意

- `research_plan_executor.sec_descriptor_spec` 默认 `capability_policy_ref=policy:sec-public-research`；lane 必须显式传 `capability_policy_ref=governance.policy_ref`，否则 catalog policy 查不到。S7d-1 的 lane 已这样做（`sec_company_facts_lane.py` 里两处）。
