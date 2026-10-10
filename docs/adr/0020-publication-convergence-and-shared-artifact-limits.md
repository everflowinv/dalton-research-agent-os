# ADR-0020：发布流水线收敛与原稿额度

状态：已实现并部署；实际产物完成情况与长期观察单独验收。

同一研究原稿多次进入本地化、检查、修订和核验，旧初稿/修复开关也允许多轮支出。
预算必须约束真实发送，不能只展示一个配置值或按 chunk 重置。

决策：

- `publication-limits.sqlite3` 保存滚动 24 小时的 append-only 运行、调用、结果与停放事件；
  UPDATE/DELETE trigger 拒绝改历史。每个 source hash 最多 2 次准备运行、6 次调用。
  新版本、每日重试、owner 重试与复审原因留档；owner 重试不豁免额度。
- worker、CLI build、UI 批次及问答的语言运行器都建立原稿 scope。
  整份原稿的 hash 与 tick 显式贯穿线程池；所有 chunk 共用额度。
- governed call 先占一个调用位，真实 `CockpitModel` adapter 的后续 fallback/provider/transport
  dispatch 再占位。第 7 次拒绝发生在 adapter 前；属于本地 definitely-not-sent，
  不记 provider 故障、不进入传输重试。预算已准入时，走正常终态与结算；
  不能释放此前确实可能计费的 fallback。
- scheduler 的无发送重放和 broker definitely-not-sent 有 append-only 取消证据；
  不确定发送保留位。旧阶段回执按唯一调用身份在首次打开时导入，避免新计数器重置旧支出。
- 语言最多初次修订加一次局部修复；旧第三轮开关不能放宽。初稿最多两次。
  拒绝必须给章节与 source/candidate 唯一引句及差异代码；`changed_number` 要由原有数字
  差异检查确定存在实际差异，等价单位换算不算。语义理由本身仍需独立模型/人工审阅；
  “可复核”表示锚定与数值差异可机械重放，绝不声称代码证明全部语义判断。
- 局部 patch 不得改其他章节、未被拒句子、标题或 gaps；保留原调用与展开绑定、历史反馈。
  最终数字/结构校验和独立核验仍必须通过。两轮仍失败或理由无法锚定则保留草稿。
- 缓存共同绑定 `source_hash`、final-text rules、substance rules、convergence version。
  已独立核验的未变化章节可复用；起草、检查、修订仅处理变化章节。
  合并新版本必须再做完整 source + merged candidate 的独立核验，排除全部旧/新作者 family。
  这是保真所需的上下文例外，不能为了 token 节约把未知章节当作已通过。
- 全产品进入源稿 substance gate；cached build 和旧 completed worker 状态也重新检查。
  复盘表头、lane/pool 标签不再成为单独正文，改为有数值的投入指标表。
- fee admission 的 immutable JSON 记录 publication source/rules/stage/prompt bytes。
  管理健康与费用页按原稿显示运行、调用、停放原因、阶段大小和重复运行榜；费用优先读
  day ledger 的结算/校正/未结算预留，包含 fallback，旧回执标为回执来源。

已有 mission/day/pool/owner 费用授权保持原样。本方案新增更紧的发布额度，不提高预算授权。
超额保留草稿及 retry-after，达到窗口后可以继续已缓存工作，不删除旧失败证据。

本地真实 stage 日志只读估算见 [交付报告](../reports/ay-publication-convergence-2026-10-10.md)。
运行继续观察新事件、预算终态、并发分块与问答路径；真实拒绝及停放保留，不因部署成功标研究产物已完成。
