# Phase 1 Agenda Shadow 运营链路实施报告

日期：2026-08-14

## 结论

Phase 1 的运营链路已上线。Dalton 现在能把 durable outbox 中的 Agenda 卡投递到 OpenClaw Discord，
保存平台回执，重启后对账并避免重复外发；指定的人类监督者可以用 ✅ 或 ❌ 给决策打标签。公开看板已增加
Agenda 监督视图。系统仍只选研究问题，不执行研究，也不修改 Evidence、Claim、Thesis 或正式观点。

万华试点的首张卡已投递到 `#eve-dev`，Discord message id 为 `1537780641733673043`。确定性 marker 搜索
只返回这一条消息；控制器重启后再次运行，claimed 和 delivered 均为 0，没有重复投递。

## 已实现

- outbox 采用 append-only `pending → claimed → delivered|failed` 事件链。
- claim 带 attempt id、lease expiry、endpoint、retry time 和最大重试次数；过期 claim 可恢复，旧 attempt
  不能补写成功。
- 每张卡带由 payload hash 派生的 marker。发送前搜索 marker；若 Discord 已收消息但本地未写回执，
  bridge 会对账并补写 receipt，不再发送第二次。
- OpenClaw 调用只使用 argv，不经过 shell；错误输出受大小限制且不回显消息正文。
- 人工反馈使用独立 `feedback-bridge` principal，只允许读取待标注目标和写入 Agenda feedback；不能操作
  policy、mandate、pause、Evidence、Claim 或 Thesis。
- 反馈只接受配置白名单中的 Discord user id。✅ 记为 agree，❌ 记为 disagree，同时点两种不入账。
- feedback 按 decision 和 subject 形成 append-only 版本链，并保存来源事件引用；公开 projection 不输出
  Discord user id。
- 看板新增暂停状态、policy、cycle、候选问题、选择结果、投递状态、尝试次数、标签数和认可率。
- controller 以单线程 outbox lane 运行 bridge；bridge 错误会让服务进入 degraded，但不会越过 writer
  authority 直接写 SQLite。

## Live 验收

- 首张卡：1 次 claim、1 次 delivery、receipt `discord:1537780641733673043`。
- marker 对账结果：1 条；控制器重启后仍为 1 条。
- 重启后的 outbox 结果：claimed 0、delivered 0、failed 0、feedback target 1。
- 当前人工标签：0。尚未收到白名单监督者的 ✅ 或 ❌，因此看板显示 `unlabeled`。
- 公开看板：<https://eve.lumos.space/dalton/>，HTTP 200；显示 1 个 delivered cycle、3 个 selected、
  3 个 deferred、0 个 pending delivery。
- `dalton.writer` 与 `dalton.controller` LaunchAgent 均为 running；health 为 ok。
- 旧 `dalton-coverage-*` cron 共 10 条，10 条仍启用。
- Core、Scheduler、Model Router 三库 `PRAGMA integrity_check` 均为 `ok`。
- Scheduler 最新状态：2 个 failed、1 个 succeeded；ready/leased 为 0。
- Evidence、Claim、Thesis 均为 0；没有研究执行或正式观点写入。
- Python：191/191；OpenClaw model broker：15/15。

## 部署中发现的问题

第一次 live bootstrap 暴露了旧库迁移顺序错误：schema 在给旧 `agenda_feedback` 表补 `subject_ref` 前，
先创建了依赖该列的索引，SQLite 因此拒绝启动。安装脚本在 bootstrap 前已停止 LaunchAgent，所以没有继续
运行半升级服务，也没有修改业务数据。

修复后，新增索引统一在补列之后创建，并增加旧表迁移回归测试。上线前快照
`pre-agenda-operations-20260814T1100Z` 已完成独立目录 restore 校验；修复后两个 LaunchAgent 正常恢复。

## 当前边界

- 只有白名单监督者的反应会入账，bot 和其他频道成员的反应会被忽略。
- Phase 1 标签按当前反应生成最终记录；需要纠正时，由 owner 使用一次性治理 CLI 追加新版本。
- bridge 仍复用 OpenClaw `default` Discord account；broker 仍复用 `chem` identity。这不影响单公司只读
  shadow，但关闭任何旧 cron 前仍必须拆出 Dalton 专用 broker identity。
- 认可率阈值仍未冻结，`cutover_enabled=false`。
- 公开 Agenda 文本可能被抓取和缓存；projection 继续禁止 credential、prompt、raw model output、token、
  数据库路径和反馈者身份。

## 试点继续条件

从本次首张 delivered card 起累计 10 个工作日。扩到 3 家前仍需至少 20 个有人工标签的 decision，且满足
零重复外发、零越权 authority 写入、全部模型调用计入 Usage/Cost、pause 后零 broker 调用和一次故障恢复
演练。关闭旧 cron 的门槛不变：至少 4 周 shadow、覆盖一次真实财报或 filing、明确认可率阈值、Dalton
专用 broker identity，并完成 rollback 演练。
