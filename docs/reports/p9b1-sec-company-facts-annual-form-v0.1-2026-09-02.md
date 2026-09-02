# P9b-1：SEC company-facts 读 10-K（同一 filing 的 Q4 季度对比）v0.1

日期：2026-09-02  
状态：development candidate 完成；live 只读副本排练通过；**2026-09-02 07:01Z 已激活 live**
上游：[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md) P9b、[P9a 报告](p9a-research-playbook-and-coverage-mission-v0.1-2026-09-02.md)

## 先核对事实，再定规则

P9b 原计划写「10-K + Q4 = FY − 9M 派生」。动手前先拉了五家公司 `data.sec.gov` 的 companyfacts（2026-09-02）：

- **ACN**：10-K 里直接带 Q4 单季值和上年同期 Q4（同一 accession，`frame` 齐全，`fp=FY`）。FY2025 10-K
  `0001467373-25-000217`（2025-10-10 提交）：2025-06-01..2025-08-31 USD 17,596,260,000，上年同期
  16,405,819,000。FY2026 10-K 预计 2026-10 上旬提交（业绩日 10/1 发的是 8-K）。
- **CTSH / EPAM / IBM / DXC**：最新 10-K 只有全年值，没有季度 duration 的事实。Q4 只能用 10-K 的 FY 减 Q3 10-Q
  的 9M，跨两个 accession。它们的下一份 10-K 在 2027 年 2–5 月，不在 10/1 窗口内。

所以本片只做「让 10-K 走既有的同一 filing 季度对比规则」，覆盖 ACN；FY − 9M 跨 accession 派生（source record
4 条、两个 accession、auto-commit 规则要改）作为后续独立冻结规则，不混进来。

## 做了什么（全部追加式）

1. **表单注册表**：`sec_public_adapter.SEC_COMPANY_FACTS_FORMS = ("10-Q", "10-K")`，`company_facts_selection_basis(form)`
   给出 `ordered_allowlist_latest_{form}`。`normalize_sec_company_concept` / `normalize_sec_company_facts` 对两种
   form 用同一套「同 accession、同 fp、350–380 天间隔、duration 相差 ≤7 天」的选择规则；只有全年值的 10-K
   fail closed（`no allowlisted revenue concept resolves on the latest 10-K accession`）。
2. **输出合同**：`connector_inventory` 的 `get_company_facts` 输出 schema 把 `form`、`fact.form`、`selection_basis`
   的 enum 扩到 10-K；packaged inventory 重建（`scripts/build_connector_inventory.py`）。
3. **SEC 模板注册表**（`research_plan.SEC_TEMPLATE_REGISTRY`）：plan 的 execution scope 冻结了模板 `content_hash`
   与 operation 的 `output_schema_hash`，重验时会用当前 packaged 模板重建——模板一变，历史 plan 全部读不出来。
   照 S7d-5 预算注册表的做法：v1 记旧 hash（模板 `c5050e46…`，company facts 合同 `13a570d3…`），v2 记当前
   （`0193636e…` / `0b25854c…`）；重验接受任一注册版本的一致配对，新 plan 绑 head。`sec_template_registry()`
   在 packaged 模板不是注册 head 时直接拒绝，避免以后改模板忘记追加。
4. **policy rule**：`research-plan-auto-start:sec-public-company-facts-annual:v1` 与
   `research-auto-commit:sec-public-company-facts-growth-annual:v1`。plan 的 form 决定它需要哪条规则；policy 的
   `rules` 可以列多条已知规则（去重、全部已知），只列 10-Q 规则的 policy-3 继续拒绝 10-K plan 与 10-K 候选。
   lane 的 `check_core_governance_rules` 同步改成「必须含 10-Q 规则、全部已知、无重复」。
5. **connector 权威记录的 template 维度**：profile spec 含 descriptor / schema hash，模板变了以后
   `connector-profile:sec-public:budget-v2` 的幂等键会冲突（副本排练第一次就撞上）。现在 template tag ≠ v1 时，
   profile / price / rate policy 的 ref 与幂等键都带 `:template-v2`，profile 版本号锚在该 Core 现有最新版本之后；
   `sec_current_rate_policy_ref` / runner binding / environment ref 同步带后缀，lane manifest 与 executor 一致。
6. **`form` 参数贯通**：`SecCompanyFactsLane.run_issuer / run_lane(form=)`、`sec_lane_cli --form {10-Q,10-K}`
   （默认 10-Q）、`SecLaneLauncher.start(form=)`（写进 ticket 与子进程 argv）、writer op
   `run_sec_company_facts_lane` 可选 `form`。10-Q 的问题 suffix 保持逐字节不变，10-K 才把 form 加进身份。
7. **顺手修掉一个潜在阻断**：backlog question 的身份是 (mandate, company, 文本)，状态机 answered 后终态；lane
   之前对每家公司只问同一句话，所以同一 issuer 的第二个窗口（新的 10-Q、10-K）会在 `select_question` 上撞
   「only an open question can be selected」。P9b-2 的自动重跑必须过这一关。现在每次运行问窗口专属的问题
   （`… (10-K filed 2025-09-01..2025-12-31)`），同参数重跑仍走幂等键收敛。
8. **部署工件**：`deploy/connector-governance/sec-company-facts-v2.json`（仓库保留 proposal；live 副本已由 owner 批准，
   `expected_schema_hash 6ce86d8a…`）；`deploy/phase1/governance-policy-v4-company-facts-annual.candidate.params.json`
   （prior=policy-3，两组 rules 各追加 annual，weekly brief 绑定不变）。
9. **排练脚本** `scripts/run_p9b_annual_lane_canary.py`：只读复制现有 Core → 逐条 exact 读所有历史 plan →
   以 `human:lumos` 装候选 policy-4 → rehearsal governance 下用 companyfacts fixture 跑 `--form 10-K` lane →
   再读一遍所有 plan、核对 Claim 数与最新 authorization 的 rule。

## 验收

- 新测 8 个（adapter 10-K 选择与 fail closed；plan 的 form 校验、annual rule、policy 列表规则、注册表 v1/v2；
  launcher form；lane 10-K committed / 只列 10-Q 规则时拒绝 / 第二窗口新问题）。
- 相关模块 `test_sec_public_adapter test_research_plan test_connector_inventory test_sec_lane_launcher
  test_sec_company_facts_lane test_sec_revenue_growth_batch test_research_plan_executor test_connector_governance
  test_governance_cli`：全过。
- 全仓 `unittest discover`：**993 个测试，992 通过**；唯一失败仍是 `test_writer_service` 的 partial-frame 用例，
  本机 macOS socket 环境问题（P9a 已记录，HEAD 上同样失败），CI ubuntu 上正常。
- `compileall`、`git diff --check`：通过。
- **live 只读副本排练**（`temp/p9b-canary-livecopy.json`，temp 不入 git）：`ok=true`。5 条历史 plan（全部 v1
  模板 hash）重验通过；policy-4 装入并通过 lane 前置检查；10-K lane **committed**：plan form 10-K、authorization
  rule 为 annual、Claim「Accenture plc reported Revenues of USD 17596260000 for 2025-06-01..2025-08-31, up 7.26%
  year over year from USD 16405819000」、source / numeric 均 pass、Claim 6→7、6 条 plan 全部可读、integrity
  core / coordinator / staging ok。0 网络（fixture 是本机保存的 companyfacts）、0 付费调用、0 live 写入。

## Live 激活记录（2026-09-02）

- owner `human:lumos` 批准 live `sec-company-facts-v2.json`：status `approved`，content hash
  `f781c156…fa074`。
- `dalton-gov create_policy` 发布 active `policy-4`（content hash `39dd5b7a…e0f30`），prior `policy-3`；只追加
  annual auto-start / auto-commit 两条规则，weekly brief binding 未动。
- commit `b2f34c8` 让 install seed-once v2，并让 writer plist 指向 v2；`zsh deploy/macos/install.sh` 重装 wheel、重启
  writer / controller / control / thesis-impact。健康检查全绿，bounded planner idle，weekly brief waiting。
- 激活过程没有触发 SEC lane、没有新增 Claim；真实数据路径继续等 observation 或人工触发。激活后的 live Core 只读副本
  mission canary 以 `automation:coverage-mission` 跑通 10-K，仍未写 live。

## 边界与未做

- **FY − 9M 跨 accession 派生没做**（CTSH / EPAM / IBM / DXC 的 10-K 需要）。它改变 source record 数量和
  auto-commit 的「latest_accession == current.accession」不变量，要单独冻结成规则。
- bounded planner 的观察合同 `contracts/sec-quarterly-growth-observation.schema.json` 与 `bounded_probe_executor`
  仍只认 10-Q；loop 观察到 10-K accession 是 P9b-2 的事。
- live 激活已按以下三步一起完成：
  1. `dalton-connector-governance approve --path <state>/connector-governance/sec-company-facts-v2.json --approved-by human:lumos`
     （先把 v2 记录放到 live governance 目录）。模板 hash 变了，descriptor 的 `expected_schema_hash` 随之变；
     只重装 wheel 不批准 v2，人工 lane 也会被 launcher 拒绝启动。
  2. `dalton-gov --operation create_policy --params deploy/phase1/governance-policy-v4-company-facts-annual.candidate.params.json --actor human:lumos`。
  3. `deploy/macos/install.sh` 重装 wheel、重启四个 LaunchAgent。
- 激活后 10-Q 路径行为不变（rule v1、selection basis 不变、历史 plan 按 v1 注册重验）；ACN 的 10-K 需要在
  10-K 提交后由人（或 P9b-2 的自动化）以 `form=10-K` 触发一次 lane。

## 下一片

P9b-2：`record_observation_followup` 发现新 accession → 在 mission `may_write` 与预算内以
`automation:coverage-mission` 触发 lane（launcher / lane 现在只收 `human:` actor，要按 ADR-0004 开一条受 mission
合同约束的自动化路径）；观察合同与 probe 认 10-K；每条自动 Claim 同步写 mission 阶段账本。
