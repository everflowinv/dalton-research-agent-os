# S7c-1：writer 发起的 AlphaEngine 获取与治理记录审批 CLI

日期：2026-08-26
状态：development candidate；未部署；未调真实 AlphaEngine；live Core 写入 0

## 结论

S6b 把 AlphaEngine 获取写进了 Core，但只能由隔离 canary CLI 在自己的进程里跑。本切片让 live writer 也能发起同一条路径，
并把「owner 审批治理记录」做成一条可执行、可核验的命令。两件事之后，把真实 ACN 文档取进 live Core 只剩 owner 的两个动作：
批准治理记录，再以 human principal 调一次 `acquire_alphaengine_document`。

## 为什么是子进程而不是线程

S6b 报告计划的是「writer 内独立 acquisition 线程 + 第二组 Core 连接」。实做时不成立：
`ConnectorTransportExecutor.invoke_adapter_with_deadline` 用 `SIGALRM` 做 watchdog，并显式要求
`threading.current_thread() is threading.main_thread()`，否则直接抛 `ConnectorTransportError`。writer 的所有 op 都跑在
`dalton-store` 单线程上，主线程只做 accept。所以获取改为 writer 启动一个子进程（`dalton_core.alphaengine_acquisition_cli`），
子进程在自己的主线程里持有 watchdog；共享状态走 SQLite（`DaltonStore` 没有独占锁，`busy_timeout=5000`，controller 进程本来
就与 writer 并发写 Scheduler）。测试里 writer 自己的 Core 连接保持打开，子进程写入的 2 条 `connector_source_envelopes`
在 writer 侧可见。

## 做了什么

### `alphaengine_acquisition_cli.py`（原 canary 脚本的可导入版本）

- `run_acquisition(...)` 与 `main(argv)`；新增 `--summary-dir`（summary / manifest 落到 ticket 目录，不再写进 state 根）、
  `--catalog-db`、`--quiet`。`--allow-network` 仍拒绝 `--governance-approved-by`（内存伪造 approved 只允许 rehearsal）。
- console script `dalton-acquire-alphaengine`；`scripts/run_isolated_alphaengine_core_acquisition_canary.py` 变成薄包装，
  默认治理路径不变。

### `alphaengine_acquisition_launcher.py`

- 启动前检查：磁盘上的治理记录必须 `approved`、hash 自洽、`approved_by` 是 `human:*`；`document_ref` 必须是
  `alphaengine-doc:<id>`；请求者必须是 `human:*`；同一时间只允许一个获取在跑（第二个请求返回 `conflict`）。
  任何一条不满足都在 spawn 之前拒绝，`acquisitions/` 目录里不会留下东西。
- 每次启动写一张 ticket（`<state>/acquisitions/<hex>/ticket.json`，0600）：document_ref、actor、治理记录 ref/hash、
  transport（`loopback-mcp` / `rehearsal`）、pid、started_at。子进程 stdout/stderr 落到同目录 `run.log`。
- `status(ticket)`：子进程退出后把 exit code、completed_at 和 `succeeded / failed` 写回 ticket，并附上 `summary.json`
  （manifest ref/hash、assembled digest、`expected_digest_match`、`transcript_authority_probe`、Core 各表计数）。writer 重启后
  ticket 没有对应进程且 pid 不存活时记 `orphaned`，不从残留的 summary 猜成功。

### writer

- 新 op `acquire_alphaengine_document`（`document_ref`、可选 `expected_content_sha256`、`max_pages`，actor 由 principal 注入）
  和 `alphaengine_acquisition_status`，都在 `HUMAN_GOVERNANCE_OPERATIONS`，非 human principal 是 `forbidden`。
- `dalton-writer` 新参数 `--connector-governance <path>`（给了才启用 launcher）、`--alphaengine-mcp-endpoint`（默认
  `http://127.0.0.1:8950/mcp`，handle 只接受无凭据 loopback）；`--acquisition-rehearsal-document` /
  `--acquisition-rehearsal-approved-by` 只给测试用，LaunchAgent 不传。
- LaunchAgent 的 writer 参数增加 `--connector-governance <state>/connector-governance/alphaengine-get-document-v1.json`；
  `deploy/macos/install.sh` 第一次安装时把仓库里的 `proposed` 记录复制到该位置（0600），存在则不覆盖，避免把 owner 已批准的
  记录冲回 proposed。

### `dalton-connector-governance`（owner CLI）

- `show --path FILE`：打印 id / status / approved / approved_by / hash。
- `approve --path FILE --approved-by human:<owner>`：先核 hash 自洽、`expected_source_hash` / `expected_schema_hash` 仍等于
  packaged 合同、整条记录等于 `build_governance_record` 的 proposal 形状，再改 `status=approved` 重算 hash 原位写回；
  同一 principal 重复 approve 幂等，换人拒绝，非 human 拒绝，被改过的记录拒绝。这是 owner 在自己 shell 里执行的动作，
  不是 Eve 代签。

## 验证

- `tests/test_alphaengine_acquisition_launcher.py` 6 项：approve 翻转与 hash 重绑、篡改/非 human 拒绝、CLI show/approve；
  launcher 在 proposed 记录、非法 document_ref、非 human actor、非法 sha256、非法 max_pages、非 human approver 下均不 spawn；
  rehearsal 获取两页进 Core（writer 式连接同时打开）、单槽位冲突、四个产物文件 0600；经 writer 的 human governance
  launch → status 轮询 → `succeeded`、probe ok、digest 匹配，dashboard principal `forbidden`，proposed 记录 `rejected`，
  未知 ticket `not_found`。
- 关联回归 48/48：上述文件 + `test_writer_service`、`test_service`、`test_alphaengine_core_acquisition`、
  `test_governance_cli`、`test_slice2_integration`。`compileall`、`git diff --check` 通过。全仓交 CI。
- 一个已知边界（测试里固定下来）：同一文档第二次 launch 是新的 plan（新 created_at、新 WorkOrder id），journal 不跨进程重放，
  provider 会再被调用、再计 1 个 document unit。所以不要随手重取同一份文档；要核对内容用 `expected_content_sha256`。

## 明确没做

- 未部署 live、未调真实 AlphaEngine、未写 live Core。
- 治理记录仍是 `proposed`；批准要 owner 在 Mac mini 上执行
  `~/Library/Application\ Support/Dalton/runtime/venv/bin/dalton-connector-governance approve --path "~/Library/Application Support/Dalton/state/dalton-core/connector-governance/alphaengine-get-document-v1.json" --approved-by human:lumos`
  （部署后该文件才存在；部署前可先对仓库里的 `deploy/connector-governance/alphaengine-get-document-v1.json` 执行并提交）。
- Cockpit 还没有「获取」按钮；本切片的触发方式是 `dalton-gov`（ephemeral human principal）或后续 S7c-2 的 Cockpit 接线。
- `stage_transcript_candidate` op（把取回的 authority 变成 CandidateStaging 候选）等 S7b 的 builder 合入后再接。
