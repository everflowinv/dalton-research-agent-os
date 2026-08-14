# Connector P0-4a trusted metadata sync authority

日期：2026-08-14  
部署状态：未部署；未连接 OpenClaw live inventory；未访问真实数据源

## 结论

P0-3 的 metadata importer 已补上可信来源注册、单调 snapshot chain 和 exporter 崩溃恢复。Catalog 而不是
exporter 排序承担最终 authority；旧 snapshot 不能回滚 metadata，两个 exporter 也不能同时推进同一代。
本切片只完成 P0-4a 的第一笔提交，connector dashboard projection 留在下一笔独立提交。

## Source 与顺序 authority

- Snapshot wire 0.2 新增 `source_instance_ref`、`exporter_version`、正整数 `catalog_generation`，以及成对
  nullable 的 `prior_snapshot_ref/prior_snapshot_hash`；generation 1 必须没有 prior，后续 generation 必须有；
- 新 source instance 必须经过 trusted resolver 返回的 active human registration。更换 instance 会撤换
  active source、清除旧 current metadata、撤下 external authority scope 的全部旧 descriptor 并推进 epoch，
  包括 importer 尚未观察过的 pre-existing descriptor；旧 instance 不能自行复活；
- registration receipt 绑定 reset 前的 exact active source/ref/hash，两个并发人工 reset 只能有一个提交；
  首次注册也按 cutover 处理，撤下 P0-3 legacy current metadata/descriptor；registration wire 0.1 禁止有限
  expiry，实例停用必须显式换新 source instance；
- Catalog 只接受 exact next generation 和 exact prior head。duplicate、stale、gap、fork、equivocation、
  unregistered 都有明确分类；
- 拒绝只追加脱敏 ingest event。event 不含 snapshot body、skill instruction、schema body、credential、路径或
  server config；拒绝不会推进 head、current metadata、descriptor projection 或 catalog epoch；
- 两个 SQLite connection 同时提交同一 next generation 时，loser 在事务锁内重读 head 并持久化
  equivocation event，不会提前以无 ingest event 的 `StaleCatalog` 退出；
- 接受路径把 ingest event、snapshot、source head、schema/metadata、complete-scope 删除和 descriptor withdrawal
  放在一个 SQLite 事务。中途写入失败会整体回滚，同一 generation 可以安全重放。

## Exporter crash recovery

- host-owned exporter 用权限 `0600` 的独立 SQLite 保存 acknowledged head 和最多一个 pending snapshot；
- prepare 后即使观测到新的 inventory，只要 pending 未 acknowledge，就继续返回原 snapshot；
- Catalog 已接受而 exporter 尚未 acknowledge 时，重启会重放原 snapshot。Catalog 返回 duplicate 后 exporter
  才能清除 pending 并生成下一 generation；
- exporter 输入只允许已过滤的 compact skill/MCP records，并自行计算 metadata hash。它不接收或持久化
  skill path/instruction、MCP server config、credential 或 tool output；
- 当前没有调用 `openclaw mcp probe`，也没有把 exporter 接到 live inventory。

## 审批与兼容性

- skill descriptor 的 schema hash 现在绑定 exact upstream metadata hash；description 等 metadata 漂移会使旧
  approval 失效；prompt-like description 只在隔离 staging 原样保存，human approval 前不可搜索或调用；
- P0-3 既有 immutable snapshot base table 不做 ALTER。Wire 0.2 source/generation/prior authority 使用严格
  1:1 sidecar chain table，带 registration/snapshot FK、source+generation unique、prior pairing 和 generation
  CHECK；fresh 与升级库使用同一 DDL。旧 row 不伪造 operator registration，新 exporter 从 generation 1 开始；
- 新旧 snapshot 历史都保持 append-only。

## 验证与剩余边界

- metadata 专项 19/19、Python 全量 280/280、OpenClaw broker 15/15、`compileall` 与
  `git diff --check` 通过；
- 专项覆盖完整 external-scope source reset、有限 expiry 拒绝、并发 reset exact-prior gate、
  stale/equivocation/gap/fork、并发 snapshot loser 的 durable event、exporter acknowledge 前崩溃、接受事务
  中途失败与重放、带 dependent metadata/FK 的 P0-3 SQLite 兼容、prompt-like metadata human gate；
- 系统 Python 3.13 没有做全局 package 修改。仓库本地 `.venv` 已安装 `setuptools 84.0.0` 与 `build 1.5.0`，
  `pip wheel --no-build-isolation` 已恢复；固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 wheel SHA-256 均为
  `d06474d8292edcca7efcefaa1c2ee5b4adaec023b941b16aa1e34a1235b4a178`。隔离安装可导入 Catalog/exporter、
  创建 11 张 external metadata authority 表，`foreign_key_check` 无违规，SQLite integrity 为 `ok`；
- Fable 5 的四轮增量复核先后复现并关闭 incomplete reset、fresh/migrated DDL 分叉、有限 expiry、并发
  rejection 无 durable event 和首次 registration cutover 缝隙，最终裁决为 **Go**，范围只含 P0-4a
  Commit A；
- 尚未完成 connector dashboard projection、OpenClaw live inventory attach、真实 HTTP、authenticated MCP、
  A股/SEC/AlphaEngine connector、部署或研究 WorkOrder。上述范围仍是 No-Go。
