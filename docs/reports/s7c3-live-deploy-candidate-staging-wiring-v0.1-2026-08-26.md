# S7c-3：writer LaunchAgent 接 `--candidate-staging` 并把 S7a / S7c-1 / S7c-2 推到 live v0.1

日期：2026-08-26
状态：已部署 live（source `dc747de`）；未调真实 AlphaEngine；live Core 正式 Evidence / Claim 仍为 0

## 结论

live writer 现在带 `--connector-governance`（S7c-1）和 `--candidate-staging`（S7c-2）启动，两条路径都从既有配置推导，
不新造第二套约定：治理记录由 install.sh 首次复制到 state 目录，staging 路径直接取 `control.config.research_review.candidate_staging_path`，
所以 writer 和 Cockpit 写的是同一个 `candidate-staging.sqlite`。部署后用 `dalton-gov` 探针确认 `transcript_candidate_status` 对未知 ref
返回 `not_found`（未配置 staging 时会是 `rejected`），证明参数已生效。

部署本身没有出问题，但部署前 13:01 EDT 发布的 policy v3 让 live Agenda 从 17:01 UTC 起每小时报错，这一点在本切片里发现并处理，见下。

## 做了什么

### `src/dalton_core/macos_launchagent.py`

- `render()` 先读 `ServiceConfig`，若 `control.research_review` 存在，就在 writer `ProgramArguments` 末尾追加
  `--candidate-staging <candidate_staging_path>`；没有 control 或没有 `research_review` 时不加，writer 上两个 transcript 候选 op 返回 `rejected`。
- 没有改 install.sh：它本来就调用这个 renderer。

### `tests/test_service.py`

- `test_enabled_control_plane_gets_a_separate_launchagent` 增加断言：writer 参数里 `--candidate-staging` 的值等于 `ServiceConfig` 解析出的路径。
- 新增 `test_writer_without_control_plane_has_no_candidate_staging`：无 control 配置时 writer 不带 `--candidate-staging`，仍带 `--connector-governance`。

### 部署（`deploy/macos/install.sh`，13:18 EDT）

- venv 重装 `dalton-core`（`dc747de`），四个 LaunchAgent bootout → 重渲染 → bootstrap。
- `state/dalton-core/connector-governance/alphaengine-get-document-v1.json` 首次落地，`status=approved`、`approved_by=human:lumos`、
  content_hash `2f6ad555…997c49`，与仓库一致。
- writer plist 实际参数：`--db / --scheduler / --socket / --token-config / --transcript-spool-dir / --connector-governance / --candidate-staging`，
  最后一项指向 `state/dalton-core/research-review/candidate-staging.sqlite`（Cockpit 同一文件，8/25 已存在）。

## 验证

- `tests.test_service` + `tests.test_writer_service` + `tests.test_transcript_candidate_writer_ops` + `tests.test_model_deployment`：46/46；
  `compileall`、`git diff --check` 通过。
- 部署后 `dalton-health`：writer socket、control socket、controller pid、heartbeat、projection、dashboard 发布全部通过；
  整体 `degraded` 只因 Agenda 子状态是 `error`（原因见下）。
- live 探针（`dalton-gov --actor human:lumos`）：`transcript_candidate_status` 对不存在的 ref 返回 `not_found`。
- `installed dalton_core.macos_launchagent` 与仓库 HEAD 逐字节一致。

## 部署时发现的 live 问题：policy v3 让 Agenda 找不到有效 policy

- `AgendaStore.active_policy()` 只看 `agenda_policy_pointer` 指向的那一个版本，并要求 `effective_from <= now`。
  13:01 EDT 用 `create_agenda_policy` 发布 v3 时 `activate` 默认为 true，pointer 立刻指向 v3，但 v3 `effective_from` 是 8/27 00:00 UTC，
  于是 17:01 UTC 以后每个 Agenda tick 都拿到 `AgendaNotFound("active agenda policy")`，heartbeat 记为
  `RemoteError: requested object was not found`。17:00 UTC 那次 cycle 在 v3 发布前跑完，所以没受影响。
- 处理：发布 v4（`agenda-policy-version:phase1-shadow-v4`，内容与 v3 相同，`max_input_tokens` 16,000，
  `effective_from` 2026-08-26T17:21:42Z，content_hash `28679036…326c11`，prior v3），pointer 现在指向 v4。v3 保留为历史版本，不会再被选中。
- 处理后 Agenda 仍报 `request conflicts with existing immutable data`：cycle_key 含 policy 版本 hash，所以 v4 触发今天的第二个 cycle，
  而 S7a 按 provider 预算 bounding 后的 snapshot 内容变了，`snapshot_id` 却仍是按日期生成的
  `perception-snapshot:wanhua:2026-08-26`，与 00:57 UTC 已登记的不可变版本冲突。这个错误发生在任何模型调用之前，不花钱、不写脏数据；
  8/27 00:00 UTC 起 snapshot_id 和 cycle_key 都换新，S7a + v4 才会第一次真正跑通。
- 两条待改的设计点（不在本切片改）：
  1. pointer 指向未生效版本时 `active_policy()` 应回退到 prior chain 里最近一个已生效版本，或 `create_agenda_policy` 对未来 `effective_from` 默认不 activate；
  2. perception snapshot 的 id 应带内容 hash（或 coordinator 在同一天复用已登记 snapshot），否则同日改 policy 必然冲突。

## 明确没做

- 没有调真实 AlphaEngine，没有跑 `acquire_alphaengine_document`；live Core 的 connector 表仍为空。
- 没有在 live stage 任何 transcript 候选，Cockpit 没有新增待审项。
- 没有改 Agenda 的 policy 解析和 snapshot id 语义（见上两条待改点）。
- `alphaengine_acquisition_status` 探针用了格式不合法的 ticket ref，得到的是 `internal_error` 而不是 `rejected`，错误映射还可以再收紧；本切片没改。
- CI：`51d5820` 的 run 仍在跑 python 3.11 / 3.13 job；`9e78e2a`、`dc747de` 的 run 在 push 后约 40 分钟才被 GitHub 创建，
  部署时都未完成。本地定向回归是本次部署的依据，全仓慢回归没有在本地跑。
