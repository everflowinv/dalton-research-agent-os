# Dalton Core Slice 6：模型接入与旧架构影子迁移

日期：2026-08-14

## 结果

Dalton Core 已登记并限制使用六条 exact model route：

- `deepseek/deepseek-v4-flash`
- `openai/gpt-5.6-sol`
- `openai/gpt-5.6-terra`
- `openai/gpt-5.6-luna`
- `claude-cli-gateway/claude-fable-5`
- `claude-cli-gateway/claude-opus-5`

OpenClaw broker 使用 dedicated agent `chem`，只接受上面六条 model ref。worker 只能传
Core 已接受的 profile 和 route decision，不能传 agent、API key、header、base URL 或 auth
profile。六条模型都已通过真实 completion smoke test，没有 fallback。

OpenAI 官方把 Sol 定位为复杂、高价值任务，Terra 定位为日常通用任务，Luna 定位为清晰、
可重复、高吞吐任务。本次 Core profile 据此把 `research-hard/adjudicate` 放在 Sol，把一般
`research/verify` 放在 Terra，把 `summarize/extract/format` 放在 Luna。来源：

- https://learn.chatgpt.com/docs/models#choosing-sol-terra-and-luna
- https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p6-sol

## 旧 Dalton 迁移

旧 `workspace-chem` 已完成只读、影子迁移：

- 72 份逻辑 artifact，共 1,219,834 bytes；
- 约束、研究产出、Excel 模型、wiki、references、memory、scripts、config、price data 全部归档；
- Coverage SQLite 用 backup API 取得一致快照，未复制 WAL/SHM；
- 每份文件写入 owner-only content-addressed store，并登记 `ArtifactVersion`、producer invocation
  和 ResultEnvelope hash；
- Coverage DB 当前包含 3 家公司、11 条 thesis、27 条 evidence、10 个 task、9 个 decision、
  5 个 event、25 份 filing、19 次 run；
- 10 个启用中的 Coverage cron 和 7 个已停用的一次性历史任务均已归档。

迁移没有改动旧工作区，也没有启动第二套 cron。旧 cron 仍是唯一 live producer；Core 内只做
`shadow_registered_not_scheduled` 登记。旧 thesis 标为
`quarantined_pending_core_verification`，不能跳过 Claim/Evidence/Verification gate 直接提交。

## 迁移边界

本 slice 不是 cutover。以下工作仍未完成：

- native schedule/runner 和旧 cron 的逐项 reconciliation；
- 旧 thesis/evidence 转换为 Core Claim/Evidence contract 后的独立复核；
- 切换、观察期和 rollback 演练；
- broker 与普通 worker 的独立 OS/container identity；
- model profile 的周期性 availability probe。当前 smoke attestation 的 TTL 是 24 小时。

Gateway 已收到安全重启请求。因为当前回复本身仍占用 embedded run，重启会在活跃工作排空后
执行；没有使用强制重启。

## 验证

- Dalton Core：168/168 tests passed；
- OpenClaw broker：14/14 tests passed；
- OpenClaw config：valid；plugin doctor：no issues；
- Core SQLite：`PRAGMA integrity_check = ok`；
- Artifact registry：72/72 logical artifacts registered；
- 迁移 manifest hash：
  `fb74952b8a80ffcb87cbae3f87c8f2af5113e1ff61ac07902436b9b6ff783b0e`；
- Python wheel：`dalton_core-0.1.0.dev0-py3-none-any.whl` built successfully。
