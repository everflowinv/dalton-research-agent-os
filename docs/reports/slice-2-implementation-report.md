# Dalton Core Slice 2 实施与验收报告

> 日期：2026-08-14  
> 状态：在下述信任边界内验收通过；未接入 Dalton live 系统

## 1. 本轮交付

本轮把 Slice 1 的可信内核扩成可供外部 runtime 调用、但不让 runtime 接触权威
数据库的最小控制面：

- 独立 writer service：使用 owner-only Unix socket 和 versioned JSON-lines RPC；
  client 只持 socket endpoint 与 scoped token，不导入 SQLite，也没有 DB path；
- operation、actor、invocation 和 work order 四层授权：Core 先登记 invocation，
  worker、verifier、researcher、adjudicator、capability builder/evaluator 只能引用分配给
  自己的 invocation；actor 由服务端从 principal 注入；
- Research Ledger：新增 append-only `EvidenceVersion`、`ClaimVersion`、
  `EvidenceRelation`、`AdjudicationVersion`，形成
  Evidence → Claim → Thesis 的可追溯链；
- Claim status 不允许调用者直接写入；数值同口径冲突生成 challenge，定性判断保存独立
  adjudication 版本；Thesis commit 引用的 claim 必须已有 evidence relation；
- Capability Registry：支持 gap/proposal、不可变 revision、外部 sandbox evaluation 证据、
  独立 evaluator、human approval、active pointer history 和 rollback；
- 能力批准不能扩大 proposal 权限；evaluation 必须使用 proposal 声明的完整 fixture 集，
  并绑定 policy version/hash、environment hash、builder/evaluator invocation；
- `create_policy`、capability approve 和 rollback 只能由 writer 认证后的 `human:*`
  principal 执行；Core principal 不能修改 policy；
- writer 使用有上限的连接线程和单独的串行 store thread；半截 frame 有 idle timeout，
  不会阻塞其他客户端；
- contract 数量由 9 份增至 18 份；wheel 同时携带两份 SQL schema 和全部 JSON Schema。

## 2. 已验证失败路径

- runtime 伪造 event actor、model family、inline invocation 或他人的 work order；
- researcher 给其他 actor 的稳定 Claim/Evidence ID 追加版本，或修改不属于自己的 claim
  graph；
- claim 没有 evidence relation 就进入 thesis commit；
- adjudicator 另选 subject invocation，以制造表面独立性；
- agent 自批能力、伪造 `human:` actor、Core token 修改 policy；
- 空、重复、遗漏或替换 proposal fixture 后提交 evaluation；
- evaluation 后 policy 变更，以及未来或过期 policy 下的 evaluation、promotion、
  adjudication；
- permission 使用 `null`、未知/扩张权限，或 rollback 激活从未获批的版本；
- evaluation 缺稳定 ID 却声明幂等、relation 或 capability key 的 duplicate/conflict 重放；
- 未认证客户端发送半截/超大/非法 frame，或单连接占住 writer accept loop；
- 对 ledger、capability decision/pointer 和 idempotency authority 做裸 UPDATE/DELETE。

## 3. 验收结果

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v`：
  72 项通过；
- `compileall` 和 `git diff --check` 通过；
- wheel 构建、隔离安装、installed demo 和 writer/registry import 通过；
- wheel SHA-256：
  `b70dc87b7546241706b32778709f3c9ae902b58ce20e6a6eb0a6b18a5a9a61af`；
- wheel 内含 18 份 `.schema.json`、`schema.sql` 和 `capability_schema.sql`；
- source isolation test 未发现 live Coverage OS 路径或数据库引用。

第一轮只读审计复现了 actor spoof、空/假 fixture、过期 policy、RPC 阻塞、空 human
subject、permission null 和两类幂等缺口。修复并加入回归测试后，第二轮只读验收未发现
P0/P1，建议在本报告的信任边界内放行。

系统默认 `python3` 环境缺少 wheel 构建 backend；本轮使用临时 Python 3.12 venv 和
项目声明的 `setuptools>=69` 完成构建与隔离安装。这不影响 stdlib-only 的运行代码，
但正式发布流程需要固定 build environment。

## 4. 信任边界与未完成项

writer service 解决的是“外部 runtime 不拿 DB path，所有权威写入经过同一逻辑边界”。
owner-only socket、token 和 SQLite 文件不能抵御同一 OS 用户下读取 token 或直接打开
数据库的恶意进程。生产部署若需要覆盖这个威胁模型，必须使用独立 OS identity、
container/VM，或带独立服务身份的存储。

本轮明确未做：

- 不执行 proposal 中的未知代码；还没有 capability sandbox、静态分析、依赖锁、
  side-effect attestation 和长期 monitoring；
- 不接 Pi、DeepSeek Harness、OpenClaw adapter 或真实模型；
- 不实现 Model IR、formula census、Excel exporter、agenda/scheduler；
- 不迁移 Coverage OS，不读写 live DB，不改 cron；
- 不把 SQLite 定为生产长期存储。

下一轮用同一份 `RuntimeProfile / WorkOrder / ResultEnvelope` 做受控 runtime spike：让
Pi、DeepSeek Harness 和 Dalton-native thin loop 执行同一个数据格式化 capability，
比较启动开销、隔离、工具调用、恢复、usage/provenance 和接 writer 的复杂度。能力代码
仍在 sandbox 中运行，只把 evaluation 证据交给 Core；OpenClaw 继续只做人和 Dalton 的
桥。
