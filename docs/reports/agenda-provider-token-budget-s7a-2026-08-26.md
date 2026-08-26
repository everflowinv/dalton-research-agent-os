# S7a：live Agenda 预算失败根因与 provider-unit 预算修复

日期：2026-08-26
状态：development candidate；CI 绿后部署 controller / writer；不改 policy、不改冻结 tokenizer、不改 ContextPack 合同

## 结论

万华 Agenda Shadow 在 2026-08-25、08-26 两个 cycle 连续 `failed`，原因都是
`PROVIDER_BUDGET_EXCEEDED: provider max_input_tokens telemetry exceeds WorkOrder budget`。这不是模型或 broker 故障，
是**同一个预算数字被两个单位分别使用**：

- coordinator 用冻结的 `tokenizer:dalton-search-token:0.1` 预检提示词。这个 tokenizer 的正则是
  `[a-z0-9_.:-]+|[一-鿿]+`，一整段连续中文只算 1 个 token。
- `OpenClawModelAdapter._assert_budget` 在模型返回后，用 provider 上报的 `inputTokens` 与同一个
  `policy.max_input_tokens = 8000` 比较，超过就把已经付费的回答标成 failed。

Scheduler 里三条真实 WorkOrder 的对照（DeepSeek V4 Flash，profile `broker-deepseek-v4-flash:5`）：

| cycle | 提示词字符 | Dalton token | provider input token | 结果 |
| --- | --- | --- | --- | --- |
| 2026-08-24 | 17,537 | 2,121 | 7,251 | delivered |
| 2026-08-25 | 20,300 | 2,495 | 8,480 | failed |
| 2026-08-26 | 22,119 | 2,719 | 9,284 | failed |

provider 计数是 Dalton 计数的 3.4 倍，字符/token 稳定在 2.38-2.42。万华 perception snapshot 随 coverage.db 里的
evidence 累积（08-23 起每天多约 3k 字符），08-25 越过 8,000 provider token 后每天都会失败。两次失败各付了
USD 0.0025-0.0029，回答被丢弃，Agenda 当天没有产出。

## 修复

1. **`provider_token_estimate.py`**：冻结 `estimator:provider-input-chars-per-token:2.2`，
   `estimate = ceil(len(text) / 2.2)`，对三条观测各留约 8% 余量；三条观测作为回归锚点写进模块和测试，估计值不得低于任一观测。
2. **perception snapshot 按预算 bounding**：`LegacyCoveragePerceptionAdapter.build/write(max_estimated_tokens=...)`。
   估计覆盖将要注册的完整 snapshot wire（header、company、三个 section、bounding 记录本身和等长的 content_hash 占位）；
   超预算时按「最旧 evidence → 最旧 catalyst → 最旧 filing」逐条丢弃（每个列表本来就是 newest-first，丢弃即 pop 尾部）。
   丢弃过程写进 snapshot 的 `bounding` 记录：`bounding_ref`、`estimator_ref`、`max_estimated_tokens`、
   `estimated_tokens`、`fetched`、`dropped`。`validate_snapshot` 校验记录与正文一致（fetched − dropped = 实际行数、
   estimated ≤ max、estimator ref 精确匹配），篡改 fail closed。不传预算时 snapshot 没有 `bounding` 键，旧记录照常校验。
3. **coordinator**：
   - perception 预算 = `policy.max_input_tokens` − 固定 wrapper 估计 − mandate wire 估计 −
     `MATERIALIZATION_FRAMING_RESERVE_TOKENS = 1000`（materialization 的 refs / hash / binding / envelope 标记在测试
     fixture 上实测约 1,700 字符 ≈ 800 token）。预算 < 1 时不 bounding，让 cycle 照旧启动并在下游 fail closed，
     保持既有「40 token 预算必须记录失败」的测试语义。
   - materialize 之后新增第二道预检：`estimate(prompt) > max_input_tokens` 时以 `prompt_input_budget_exceeded` 失败，
     metadata 同时记录 Dalton 计数、provider 估计、两个 ref 和预算；**不再付费后丢弃**。
   - 路由的 `estimated_input_tokens` 改用 provider 估计；WorkOrder metadata 新增 `prompt_tokens`、
     `estimated_provider_input_tokens`、`provider_input_estimator_ref`。

没有动的东西：`agenda-policy-version:phase1-shadow-v2`、`tokenizer:dalton-search-token:0.1`、ContextPack /
materializer 合同（其 `truncation:whole-input-drop:0.1` 仍然是「要么整段进要么整段丢」，perception 只在 adapter 层被
bounding）。

## 验证

- 新增 `tests/test_provider_token_estimate.py` 7 项：估计器不低于三条真实观测；中文提示词 provider 估计 > 3× Dalton
  计数；未传预算无 `bounding`；bounding 先丢最旧 evidence、记录完整、结果确定；evidence 耗尽才丢 catalyst；预算放不下
  company 行时 fail closed；三种篡改 bounding 记录均被 `validate_snapshot` 拒绝。
- `tests/test_agenda_coordinator.py` 新增 `test_cjk_heavy_snapshot_is_bounded_to_the_provider_token_budget`：复现 live
  形状（40 条中文 evidence，provider 估计 > 8,000 而 Dalton 计数 < 8,000）；修复后 cycle `decided`，模型只调 1 次，
  提示词 provider 估计 ≤ 8,000，注册进 Core 的 snapshot 带 `bounding`（只丢 evidence、不丢 catalyst），被丢的最旧
  evidence 不在提示词里，WorkOrder metadata 记录估计器 ref。
- 定向回归 83/83：`test_provider_token_estimate`、`test_perception_backup`、`test_agenda_coordinator`、
  `test_agenda_context`、`test_agenda`、`test_openclaw_agenda_bridge`、`test_context_materializer`；
  另跑 writer / AlphaEngine Core acquisition / dashboard projector / service 见提交记录。`compileall`、`git diff --check` 通过。
  全仓慢回归交同提交 CI。

## 对 live 的影响与建议

- 按当前 policy 8,000 token，部署后万华 snapshot 每天会被裁到约 6,000 provider token（约 13k 字符），大致保留最新
  两周的 evidence；Agenda 恢复产出，但看到的证据比 08-24 之前少约三分之一。
- **建议 owner 另行发布 `agenda-policy-version:phase1-shadow-v3`，把 `max_input_tokens` 提到 16,000**：DeepSeek V4 Flash
  输入价 USD 0.22 / M token，一个 cycle 多花不到 0.002 美元，日预算 0.5 美元完全够；policy 是 human governance 对象，
  Eve 不代发。

## 明确没做

- 未部署（等 `f7d8f98` 与本提交的 CI）。部署会同时把 S6b 的 writer connector schema（additive `CREATE TABLE IF NOT
  EXISTS`）带上 live。
- 未改 policy、未回填 08-25 / 08-26 两个失败 cycle。
- 未处理 coverage.db 的 evidence 无限累积；adapter 的 `LIMIT 80` 仍是硬上限，bounding 只保证提示词不超预算。
