# 旧 OpenClaw agent 工作流的迁移取舍

旧 Dalton 资产来自 OpenClaw agent 主导的工作流。新架构由 Dalton Core 管理任务、状态、研究权威和验证，因此旧文件只能作为迁移输入，不能原样成为新系统的指令或运行要求。

## 处理原则

- `AGENTS.md`、`SOUL.md`、`TOOLS.md`、`USER.md`、`HEARTBEAT.md` 等文件保留原始 artifact 和 hash，只提取仍适用的治理原则。它们不进入 Core prompt，也不控制 runtime。
- 旧研究产出保留为未验证 evidence/artifact 候选。只有重新建立来源、claim、verification 和 commit 链后，内容才能进入 Research Ledger。
- 旧脚本用于行为对照、fixture 或兼容性分析。未经新契约、权限和 sandbox 验证，不能登记为 active capability。
- 旧 Coverage SQLite 保留只读快照。任何转换都要版本化，不能在快照上继续写入。
- 旧 cron 的时间表达式只证明过去如何触发任务，不证明新系统仍需要同样的任务、频率或 agent turn。

## 17 个旧 cron 的初步分类

### 删除运行语义，只留历史

7 个已停用的一次性任务属于历史执行记录，不迁移为 Scheduler job：

- `chem-template-upgrade-20260727`
- `dalton-task2-ethylene-tracker`
- `dalton-task3-initial-screens`
- `dalton-task4-model-template`
- `dalton-task4-resume`
- `dalton-task4-sol`
- `dalton-task4-fix`

### 改成 Core 生命周期或事件，不再运行 agent cron

- `dalton-coverage-dispatcher`：由 Agenda/Scheduler 在 WorkOrder 入队和状态变化时驱动。
- `dalton-coverage-maintenance`：由 lease 到期、重试和 service lifecycle 处理；只保留确定性的安全扫描。

### 改成连接器事件，必要时才保留确定性轮询

- `dalton-coverage-filings`：优先使用 filing feed/subscription；源端没有事件能力时才轮询。
- `dalton-coverage-market`：由市场数据连接器产生新数据事件；轮询频率属于 connector policy，不属于 agent prompt。
- `dalton-coverage-industry`：由行业数据源更新触发；若只能按日抓取，使用确定性 connector schedule。
- `dalton-coverage-source-monitor`：改成无 LLM 的来源健康检查和 freshness probe。
- `dalton-coverage-health`：改成无 LLM 的服务健康检查、账本一致性检查和告警。

### 只保留业务截止时间，内容由 Core 工作状态决定

- `dalton-coverage-daily-report`：保留人类接收日报的时间窗，不复用旧 agent command。
- `dalton-coverage-weekly-pack`：保留周报交付时间窗，内容从已验证事件和未完成工作生成。
- `dalton-coverage-zero-base`：月度复核属于 governance due date，由 Core 创建 WorkOrder；不继承旧 OpenClaw cron payload。

## 迁移门槛

每项旧行为只有满足以下条件，才能进入新运行面：

1. 明确业务目的和取消条件，证明任务仍有必要。
2. 把输入、输出、副作用、预算和失败语义写成 WorkOrder/Capability contract。
3. 选择事件、确定性 schedule 或人工触发，不默认沿用 cron。
4. 建立幂等、重试、验证、成本和产物归属。
5. shadow 结果与旧系统对账后，再单项切换；可独立回滚。

当前归档只完成来源保全和审计基线。它不代表上述任务已经获批、重写或切换。
