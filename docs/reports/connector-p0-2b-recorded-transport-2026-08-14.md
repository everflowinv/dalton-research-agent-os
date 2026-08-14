# Connector P0-2b recorded transport 实施与复核

日期：2026-08-14
部署状态：未部署；未访问真实数据源

## 结论

P0-2b 实现了第一条真实执行的 Connector 链：

`reserve → transport barrier → adapter → attempt → Usage → Cost → Settlement → raw Artifact → SourceEnvelope → ResultEnvelope`

执行对象只限仓库内 recorded fixtures。代码没有 socket、credential grant、真实 source、writer RPC 或
Research Ledger 写入。ContextPack、Checkpoint 和 ClaimIndex 仍按既定顺序排在第一条只读研究 WorkOrder 前，
不阻塞本段 Connector 执行闭环。

## Fable 5 开工前裁决

Fable 5 独立复核给出“有条件 Go”：P0-2b 是正确的下一阶段，但 P0-2a 的
`ConnectorRunnerResponse` 强制所有结果都引用 raw Artifact/SourceEnvelope，无法表达没有响应体的
429、timeout 和 crash。复核还要求明确 journal 落位与 W2 的 Scheduler 语义。

本轮据此作出三项裁决：

1. `ConnectorRunnerResponse` 发布 wire 0.2。成功必须有 raw Artifact/SourceEnvelope；非成功结果允许两组
   ref/hash 成对为空。SourceEnvelope 保持 0.1，不伪造空 artifact；
2. journal 放 Core DB 的独立 append-only 表，但语义上属于 runner operations，不属于 Research Ledger；
3. W2 恢复不保存或重用 Scheduler lease token。Runner 立即做 indeterminate 结算，Scheduler 按自己的
   lease policy 到期重排。

## 实现

### Durable journal 与恢复

- request journal 保存闭合 RunnerRequest 和 hash；event journal 保存
  `admitted/reserved/transport_started/observed/responded` 及两种 recovery 终态；
- 每个 reservation 最多一个 `transport_started`，adapter 只能在该事务提交后调用；
- W1 只在没有 transport barrier 时 released；W2 一律写 indeterminate attempt、unavailable Usage、
  reserved upper-bound estimated Cost 和 indeterminate Settlement；
- W3 用 `runner:{invocation}:{attempt}:{step}` 重放；W4 no-op；
- journal 完全缺失但存在未结算 reservation 时按 indeterminate，不按 released。

### Raw spool

- adapter 只拿 write-only bounded sink，不拿路径；
- 写入过程中执行 `max_response_bytes`；超限删除 partial，不生成 partial success；
- finalize 顺序为 flush/fsync → SHA-256 → content-addressed object → directory fsync；
- 同 hash 去重；全局高水位计入已落对象和本进程 open reservations；恢复清理 orphan partial。

### AuthorityPort 与 recorded executor

- `ConnectorAuthorityPort` 只开放 attempt、Usage、Cost、Settlement、ArtifactVersion v0.2、SourceEnvelope、
  Scheduler completion 七类写入；
- adapter observation 只能报告 `succeeded/rate_limited/failed`。timeout 由 Runner watchdog/deadline 判定，
  indeterminate 只由 journal recovery 判定；
- success/empty finalize raw 并写 SourceEnvelope；429/timeout/failed 不写假的 raw source；
- ResultEnvelope、artifact version id 和所有写入 key 在调用 Scheduler 前确定，解开 Artifact ↔ ResultEnvelope
  引用环；
- success、empty、429、timeout、adapter exception 五类 fixture 均走真实 authority 方法，不直接插表。

## 故障验收

故障注入覆盖：

- W0：admitted 后、reserved 前；
- reservation authority 已落库、journal 尚未覆盖的 orphan 缝隙；
- W1：reserved 后，以及 raw sink 已打开但 transport_started 尚未落盘；
- spool capacity 的 released Settlement 已落库、recovery 终态尚未写 journal 的缝隙；
- W2：transport_started 后、observed 前；
- W3：observed 后；
- W4：responded 后；
- attempt、Usage、Cost、Settlement、Artifact、SourceEnvelope、Scheduler completion 每一个写入缝隙。

每个 case 都使用新的 executor 实例恢复，并再次执行 recovery。第二次 recovery 不新增 authority 行。
另有 orphan reservation 探针，证明 journal 缺失不会被误放为 released。

## 已知边界

- Core 与 Connector/Observability 共用 DaltonStore；Scheduler 是另一个 SQLite DB。这里没有跨库事务，
  journal + idempotent replay 只保证最终收敛；
- 若 Artifact/SourceEnvelope 已写而 Scheduler lease 在 completion 前过期，会留下指向未被 Scheduler 接受的
  ResultEnvelope 的元数据。journal 保持 observed 供运维判断，不能把它描述成跨库原子完成；
- orphan reservation 没有 transport barrier 时间。恢复用 reservation `created_at` 作为 unknown-start lower
  bound 并标 indeterminate，这是保守记账，不是对真实调用时点的断言；
- spool high-water reservation 只在单个 Runner 进程内串行化；多进程共享 spool、retention/lifecycle 和
  外部对象存储不在 P0-2b；
- hard watchdog 使用 Runner 主线程的 wall-clock signal；真实网络 transport 后续仍要使用可终止的受限
  transport/process 边界，不能把 recorded watchdog 当生产网络 sandbox；
- P0-2a 既有两处 Core connection 直读仍是已知债务；本轮没有扩大写权限。

## 验证

最终本机验证：

- Python：254/254；
- recorded transport 专项：12/12；journal 3/3；spool 3/3，合计 18/18；
- OpenClaw model broker：15/15；
- `compileall`：通过；
- `git diff --check`：通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel 构建 SHA-256 相同：
  `df03f7c7eca91e820babed3c2cb61c50b516b3e4a777487b7c8971016eeaf313`；
- wheel 隔离安装后可导入 executor/spool/journal，创建 2 张 runner journal 表，SQLite integrity 为 `ok`；
- Fable 5 最终独立敌对复核及增量复核均为 **Go**。它独立复跑全量 254/254、专项 18/18，未发现
  新的阻塞缺陷；
- 实现提交：`f0e824f`；GitHub CI：
  <https://github.com/everflowinv/dalton-research-agent-os/actions/runs/31824401325>，Python 3.11、3.13 和
  OpenClaw broker 最终全部通过。第一次 Python 3.11 运行因既有 writer socket 启动等待超时失败；同一提交
  未改代码直接重跑后通过。

本机系统 Python 的 `python3 -m build` 仍受已安装 `build` 包缺少 `build.__main__` 影响；上述确定性构建
使用独立 Python 3.13 venv 的 `pip wheel --no-build-isolation`。CI 的 Python 3.11/3.13 `python -m build`
均已通过。

## 下一阶段

P0-2b 独立复核通过后，按以下顺序继续：

1. skill/MCP metadata importer；
2. SSRF-safe public transport、redirect/DNS/IP 复核与 credential authority 分界；
3. A 股公告、SEC、AlphaEngine 三条 shadow connector；
4. 在第一条只读研究 WorkOrder 前实现 ContextPack builder、RunState/Checkpoint 和 ClaimIndex。

真实 connector、部署和 Agenda/Research Ledger 接入仍需单独 Go gate。
