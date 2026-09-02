# P9b-2：CoverageMission observation → SEC lane → Claim 阶段绑定 v0.1

日期：2026-09-02
状态：development candidate；专项与 live Core 只读副本 canary 通过；**未部署、未写 live**
上游：[Phase 9 v1.0](phase9-coverage-mission-autonomous-research-v1.0-2026-09-02.md)、[ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)、[P9b-1](p9b1-sec-company-facts-annual-form-v0.1-2026-09-02.md)

## 闭合的路径

`bounded probe` 观察到新 SEC accession 后，Core 仍先写一条 open ResearchQuestion；随后它从 active
CoverageMission 解析唯一公司、ticker、automation principal 和 exact mission hash，检查：SEC 在 source plan 中是
`connected`，`may_write` 同时含 claim / evidence / research_question / observation / stage_record，playbook / constitution /
mandate 仍是 active exact binding。SEC 是零付费调用，所以 mission receipt 明确记录 paid calls=0、cost=0；真实 HTTP
次数继续受 connector rate policy 控制。

通过检查后，Core 把 `(mission, company, form, filing window, observed accession)` 写入持久 dispatch 队列，再争用单一
SEC lane slot。slot 忙时记录保持 pending；controller 每个 tick（即使 active loop 为 0）都会重试，不再丢 observation。
launcher 只接受：

- 人工路径：`human:*`，不能夹带 mission automation context；
- 自动路径：exact `automation:coverage-mission` + 单一 ticker + mission ref/hash/company + hyphenated expected accession。

child lane 在任何 plan/agenda 写入前再次读取 Core，重验 mission grant。候选完成后、正式 policy commit 前，它要求 lane
选中的 `latest_accession` 与 probe 观察到的 accession 完全相同；不相同就 fail closed。正式 Claim/Evidence 写入后，新增
append-only `coverage_mission_stage_claims` 把两者的 exact ref/hash、source accession 和 automation actor 绑定到公司的当前
playbook stage。首次写入只把公司带入 `initial_screen`，不会自动通过任何 gate；Deep Insight Gate / Investment Memo 等
人类检查点不变。

## 代码与合同

- `coverage_mission.py` / `coverage_mission_schema.sql`：SEC mission authorization receipt、持久 dispatch queue、stage-claim
  ledger、progress `claim_count`；队列可更新 pending→launched/rejected，但不能删除，stage-claim append-only。每次重试前
  重新验证 mission exact binding；已失效的 mission 任务转 rejected，不会无限重试。
- `bounded_probe_executor.py`：probe 的 identity / metadata 记录 form，`10-Q|10-K` 都可选；
  `sec-quarterly-growth-observation.schema.json` 同步接受两种 form 和 selection basis。
- `bounded_planner_loop.py` / driver：observation 必须带 exact form + filed window；writer 自动排队，driver 每 tick 重试 pending。
- `sec_lane_launcher.py` / `sec_lane_cli.py` / `sec_company_facts_lane.py`：human 与 mission automation 双路径、exact accession、
  child-side mission revalidation、正式 Claim 的 stage binding。
- 新合同：`contracts/coverage-mission-stage-claim.schema.json`。
- `scripts/run_p9b_annual_lane_canary.py` 增加 `--mission-automation`，仍只复制 source Core、用本地 fixture，不访问网络或
  修改 live。

## 验收

- 专项 126/126：writer/service、P9a writer ops、mission authority、launcher、bounded planner/probe、SEC lane、research plan /
  executor、connector governance。
- live Core 只读副本 + 本地真实 ACN companyfacts fixture：active policy-4；历史 5 条 v1 plan 全部重验；
  `automation:coverage-mission` 跑 10-K，Claim 6→7，stage-claim 0→1，绑定 live mission hash `b63e1652…`，Q4 FY2025
  收入 USD 17,596.26M、同比 +7.26%，source / numeric verification 均 pass；Core / staging / coordinator integrity 均 ok。
- 0 付费调用、0 live 写入。全仓 **999/999**；`compileall` 与 `git diff --check` 通过。测试过程中只有既有的
  sqlite `ResourceWarning`，没有失败。

## 仍未做

- P9b-2 没有 live 激活。部署会让 `automation:coverage-mission` 在新 accession 到来时产生正式 Claim，属于新的自动写入
  权限，保留单独 owner gate。
- dispatch 当前只记录 pending→launched；child 完成状态仍由 lane ticket / summary 表达。writer restart 后已经 launched
  但尚未完成的 orphan reconciliation 仍沿用 launcher 的 truthful `orphaned` 状态，自动重跑策略留下一片。
- CTSH / EPAM / IBM / DXC 的 10-K FY − 9M 跨 accession 派生仍未实现。

## 下一步

owner 批准后：重装 wheel、重启四个服务；不造假触发 live。等下一条真实 SEC accession，或另行批准一条 live 历史
accession canary，再验证 `observation → pending/launched ticket → formal Claim → stage_claim → thesis-impact → weekly brief`。
