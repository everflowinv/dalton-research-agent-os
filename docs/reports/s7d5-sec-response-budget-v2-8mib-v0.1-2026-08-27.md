# S7d-5：SEC response budget v2（8 MiB）

## 结论

IBM 的 SEC Company Facts 响应需要 5,650,571 bytes，超过旧 plan 冻结的 5 MiB 上限。不能原地改 `SEC_MAX_RESPONSE_BYTES`：live 已有 ACN、EPAM、CTSH 三条不可变 plan，每次读取都会重建 execution budget；原地改常数会让三条历史 plan 全部 fail closed。

本切片把 response budget 改成追加式注册表：

- `v1 = 5 MiB`：只用于验证和重放历史 plan；旧 profile、price、rate-policy、runner manifest ref 不变。
- `v2 = 8 MiB`：新建 plan 的 head；使用新的 profile v2、独立 zero-price authority、独立 rate-policy ref 和新的 runner manifest ref。
- plan 仍把具体 byte bound 冻结在 execution scope 中；读取时只接受注册表内的完整预算，任意其他值继续 fail closed。

## 实现

- `research_plan.py`
  - 新增 `SEC_RESPONSE_BUDGETS`（v1/v2）和 head 选择函数。
  - `_execution_budget()` 新 plan 默认绑定 v2；历史 plan 按已存 byte bound 匹配注册表。
  - rate-policy、runner binding、runner environment ref 按 budget tag 派生；v1 保留旧 ref。
- `research_plan_executor.py`
  - 按 plan 的 frozen budget 选择 authority。
  - v2 在同一个 `connector:sec-edgar` 版本链追加 profile v2，最大响应 8 MiB；live 已有 profile v1 时不改 v1。
  - v2 使用独立的 price-rate 和 rate-policy，避免把新 profile 塞进 v1 的不可变 authority。
  - v1/v2 继续共用 quota scope，两个 policy 的调用仍在同一 60 秒窗口核算。
- `sec_company_facts_lane.py`
  - 新运行的 runner manifest 绑定 budget-head 的 v2 rate-policy，并使用新的 immutable manifest/binding ref。

## 验证

- 新增/定向：`test_research_plan` 18/18、`test_research_plan_executor` 16/16、`test_sec_company_facts_lane` 9/9。
- 关联回归：connector/runner/transport/resolver 53/53；plan coordinator/closure/review/control 48/48；launcher/writer 26/26；governance/verification/store 29/29。
- `compileall`、`git diff --check` 通过。
- 用本切片代码只读打开 live `core.sqlite`：三条历史 SEC plan 均以 `max_response_bytes=5,242,880` 完成完整 revalidation。
- 在 live Core、catalog、candidate staging 的 SQLite 一致性副本上做真实 SEC 网络演练：
  - 旧 profile v1（5 MiB）保留；追加 profile v2（8 MiB）。
  - 旧/new zero-price authority 和 rate-policy 各一条，profile/ref 精确对应。
  - IBM 10-Q `0000051143-26-000078` Company Facts 获取、source verification、numeric verification、candidate staging、policy-2 自动提交、closure replay 全部通过。
  - 正式候选：IBM Q2 2026 Revenues 17,162.0M 美元，同比 +1.09%；演练副本正式 Claim 为 `claim-version:85612113…1dc5c`。
  - 演练只写副本，没有写 live Core。

## 明确没做

- 本报告提交时尚未部署 live，也未在 live 上触发 IBM lane。
- 8 MiB 只提高单次公开 SEC 响应的 byte ceiling；没有放宽 host、operation、form、date window、attempt、timeout、side-effect、人工/政策权限或成本口径。
- 不把 ACN transcript Claim 或 8-K exhibit 手工 KPI 混进 lane-only brief。
