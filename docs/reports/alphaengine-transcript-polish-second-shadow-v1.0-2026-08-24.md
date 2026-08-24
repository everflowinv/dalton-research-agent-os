# AlphaEngine TranscriptPolish 第二份 shadow canary v1.0

日期：2026-08-24
状态：第二份独立 artifact gate 已通过；Claim admission gate 未通过；production 不启用

## 结论

第二份真实样本改用 `Nebius Q2 2026`。原文 42,279 字、16 个 speaker label，长度是第一份 Flowers Foods
样本的 2.4 倍。AlphaEngine 两页采集、Terra 生成和 Core conservation/source-lineage gate 已完整通过，产出
42,632 字 verified artifact。

这次默认使用未人工审阅模式，没有发布 correction set，也没有生成 Claim citation binding。正式 Evidence、Claim、
Thesis 写入均为 0。第二份样本证明了长文本 artifact 链可以通过，但没有满足 Claim admission 的人工权限条件，因此
production pointer 继续关闭。

## 权限修复

旧 canary 把自动挑出的疑似术语写入 correction set，并沿用执行消息所属 owner 的 `human:` actor。这不能证明 owner
审阅过具体术语。runner 现在只支持 `review-mode=none`：不接受 `unresolved_term`、`actor_ref` 或 Claim quote，不发布
correction set，Claim binding 必须阻断。人工 correction 必须通过单独的认证 review path，canary CLI 不能代填人类身份。

未审阅模式的成功只会设置 `shadow_artifact_gate_pass=true`。`claim_admission_gate_pass` 和 `hard_gate_pass` 仍为 false。
第一份 canary 的报告和机器证据也已按这个口径更正。

## 第二份真实结果

- 来源：AlphaEngine `Nebius Q2 2026`，document id `130000105789517`，发布日期 2026-08-12；
- 结构：42,279 字、16 个 speaker label、两次物理分页调用、一次文档配额；
- review mode：`none`，correction set 0，human actor 0；
- Terra：development policy v2，`profile:gpt-5-6-terra`；
- 成功调用：输入 11,072 tokens，输出 15,079 tokens，合计 26,151 tokens；实际成本 USD 0.203092；
- artifact：42,632 字，source/polished 比例 1.008349，verification status 为 `verified`；
- Claim binding：`blocked`，原因是 `human_correction_review_required`；
- 正式 authority：Evidence 0、Claim 0、Thesis 0。

原始逐字稿、模型结果和 SQLite canary store 仍留在 owner-only 临时目录。仓库只保存 hash、authority ref、usage、成本和
gate 结果。

## 本轮发现并修复的问题

AlphaEngine 在最后一页返回了 `offset=30,000`、`returned_chars=12,279`、`content_chars=42,279` 和
`next_offset=null`，但错误地保留 `complete=false`。adapter 现在只在 offset、长度和空 continuation 三项同时证明已经
到达精确末尾时，把该页规范化为 terminal；原始 JSON-RPC bytes 仍原样保存。其他非连续分页继续 fail closed。

第一次模型启动使用 900 秒 timeout，超过 broker 协议的 600 秒硬上限，因此在 provider 调用前被拒绝，成本为 0。runner
现在会在采集前拒绝超过 600 秒的配置。

Terra 的第一份长文本候选暴露了两个 conservation 边界：

- 模型删除一个 segment 的末尾空格，使 `We cannot` 在拼接后变成 `Wecannot`；Core 正确拒绝该候选。模型任务 v0.4
  明确要求逐段保留首尾空白，本地 verifier 也新增 exact boundary whitespace gate；
- 旧 mixed-case 规则把格式调整后的 `CPU-heavy` 当成新增专名。规则 v0.4 只把含小写前缀和后续大写的真实 mixed-case
  词视为自动专名，仍保护 `CapEx`，同时允许 acronym 后的普通连字符格式。

失败候选实际成本 USD 0.179294。修复后的成功候选成本 USD 0.203092；第二份样本模型总成本 USD 0.382386。

## Production 决定

当前决定是 `no-go pending explicit human correction review`：

- 第二份独立真实逐字稿已经通过 artifact gate；
- development policy v2 继续固定 Terra；
- production pointer 不启用；
- 下一步必须由人工明确审阅具体 correction，随后才能运行 Claim binding gate；
- 正式启用前仍需单独审核 production worker 的 lease policy、预算和部署范围。

机器可读证据：
`docs/review-evidence/alphaengine-transcript-polish-nebius-shadow-summary-2026-08-24.json`。
