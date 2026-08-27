# S7f Weekly Brief coordinator v0.1

日期：2026-08-27
状态：development candidate；代码、全量测试、打包和隔离 live-copy canary 均通过；未部署、未激活 live 自动发布

## 结果

S7f 已增加一条 policy 控制、可重放的 Weekly Brief 定时链：

```text
daltond 周期唤醒
→ Core 校验 exact schedule plan hash 和 active governance policy
→ 写 append-only CycleAdmission，冻结 plan / policy / period / prior issue
→ 生成 WeeklyBriefIssueVersion
→ 把 exact Markdown 写入现有 Agenda outbox
→ OpenClaw bridge 以 Markdown 附件投递 Discord
→ 先写 WeeklyBriefDelivery，再把通用 outbox 标为 delivered
```

这条链没有把 `core` 伪装成人类。原有 `publish_weekly_brief` 和 `record_weekly_brief_delivery` 仍只允许认证的人类主体；自动链使用两个新的 core-only operation：

- `run_weekly_brief_cycle`
- `record_scheduled_weekly_brief_delivery`

## 治理与重放

- schedule plan 是闭合合同，绑定时区、星期、时刻、`effective_from`、Evidence Pack、四个 company overlay、company→thesis mapping 和唯一投递 endpoint。
- `weekly_brief_auto_publish` policy 必须绑定 exact `plan_ref + plan_hash`，并固定 `max_issues_per_week = 1`；缺规则、规则关闭或 plan hash 漂移都在写 admission 前失败。
- `effective_from` 阻止 activation 前的旧窗口回填。
- 每个窗口只生成一个确定性的 cycle ref、issue ref 和 outbox message ref。
- CycleAdmission 先落盘并冻结当时的 policy version/hash。若 issue 后、outbox 前崩溃，重跑从同一个 admission 继续，不重新授权，也不生成第二个 issue。
- 外部投递用 payload hash marker 做 search-before-send。CLI 在 Discord 已成功、但本地 receipt 尚未提交时崩溃，重跑会找到原消息，不再次发送。
- Discord message snowflake 推导稳定的 `delivered_at`；DeliveryReceipt 绑定 exact issue hash、Markdown SHA-256、destination 和 external message ref。
- Agenda reaction feedback 查询只读取 `agenda.shadow.decision` topic，Weekly Brief outbox 不会被误当成 Agenda decision。

## Controller 与附件

Weekly coordinator 复用现有 `daltond`，没有增加新的 LaunchAgent 或常驻 LLM session。`ServiceConfig` 新增可选的 `weekly_brief` block；heartbeat 增加 `weekly_brief` 状态。

OpenClaw outbox bridge 新增可选 `weekly_brief_attachment_dir`。Weekly Brief 投递时，bridge 把 exact body 原子写成权限 `0600` 的 Markdown 文件，再调用 `openclaw message send --media`。未配置附件目录时 fail closed，Agenda 通知仍保持原行为。
若在 `daltond` 启用 weekly coordinator，配置层会强制要求 outbox 同时启用、destination 与 endpoint 完全相同、附件目录非空。

当前候选 plan：

- 文件：`deploy/phase1/weekly-brief-schedule-us-it-services-v1.json`
- `plan_ref`：`weekly-brief-plan:us-it-services:v1`
- plan hash：`dde12a2fc29b7325a85dedc98c8044a4f7d102b3b2dec8268c774c7b7be846fc`
- 首个可执行窗口：2026-09-03 07:00 America/New_York
- Evidence Pack：`industry-evidence-pack-version:us-it-services:live-sec-lane-v1`
- overlay：ACN / CTSH / EPAM / IBM 的 `live-sec-lane-v1`
- Thesis mapping：空；没有正式 ThesisVersion 的公司继续标 `insufficient`

候选 policy 在 `deploy/phase1/governance-policy-v3-weekly-brief.candidate.params.json`。文件本身不等于 activation，本轮没有执行它。
候选 policy 的 `effective_from` 保持 `null`，避免重演“把 active pointer 提前指向未来版本”的事故；真正阻止提前生成 issue 的是
plan 自己的 `effective_from = 2026-09-03T11:00:00Z`。只有 owner 执行 policy activation 后它才生效。

## 隔离 canary

`scripts/run_weekly_brief_coordinator_canary.py` 以只读方式打开 live Core，再用 SQLite backup 复制到临时目录。候选 policy、CycleAdmission、第二期 issue 和 outbox 都只写临时副本；没有外部投递。

2026-08-27 canary 结果：

- `ok=true`
- 首跑：admission / issue / outbox 均为 `fresh`
- 同窗口重跑：三者均为 `duplicate`
- matching pending outbox：1
- 临时副本 integrity：2 issues / 1 delivery / 1 feedback / 1 admission，0 问题
- live Core 复核：active policy 仍为 `policy-2`，仍为 1 issue / 1 delivery

测试另覆盖：

- 无 policy rule 时零写入；
- plan hash 漂移在新 admission 前失败；
- `effective_from` 阻止 backfill；
- issue 后、outbox 前故障可从冻结 admission 恢复；
- Markdown 附件逐字节一致，WeeklyBriefDelivery 先于通用 outbox receipt；
- 已存在 marker 时不二次发送；
- writer RPC 只允许 core 调 coordinator，其他 principal 返回 forbidden；
- 旧 outbox 配置不含附件目录时仍向后兼容。

## 验收

2026-08-27 在当前提交候选上完成：

- S7f 与相邻模块专项回归：49 / 49；
- 全仓测试：926 / 926；
- `git diff --check`：通过；
- sdist / wheel：构建通过；
- wheel 内容检查：包含 `weekly_brief_coordinator.py` 和 `weekly_brief_schema.sql`；
- wheel-only import：从安装后的 wheel 读取 plan，得到 exact hash `dde12a2f…846fc`；
- 隔离 live-copy canary：再次通过，未尝试外部投递。

## 尚未完成

- **没有部署代码，也没有修改 live service config 或 active governance policy。**自动发布仍需 owner 对候选 plan、policy、执行时间和投递 endpoint 做单独确认。
- 候选 plan 固定绑定当前 Evidence Pack v1。若下一周没有新的 pack / overlay 版本，第二期会如实显示 0 条新增，而不会把新 Claim 自动纳入。自动刷新 pack 是后续认知循环的工作，不在本切片中暗中完成。
- S7f 的第 5 家 policy 自动提交 SEC Claim 仍未完成；当前候选未加入 DXC。
- live activation 前还要把 owner-only workspace 附件目录加入 outbox config，并做一次投递前故障演练。

## 下一步

1. 提交并推送 development candidate，等待 CI。
2. 保留 automatic delivery activation owner gate；未获批准前继续保持 live `policy-2`。
3. live activation 前补 owner-only 附件目录配置，并做一次投递前故障演练。
4. S7f 下一笔研究数据工作是 DXC SEC lane；其 live run 仍是独立 owner gate。
