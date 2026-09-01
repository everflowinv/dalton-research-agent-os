# P8c-4a Doctrine ops、Constitution v2 与 9/3 投递演练 v0.1

日期：2026-09-01（owner 恢复会话；live 状态自 2026-08-27 起持续运行）  
状态：已部署 live；doctrine pack v1 与 Constitution v2 已发布；9/3 附件投递路径已真实演练通过

## 结果

### 1. Doctrine writer ops（关闭 P8a 遗留的 doctrine 空缺）

- 新增 human-governed `publish_doctrine_pack`（自然键去重，幂等 duplicate）与读 op
  `get_doctrine_pack`；writer 现在真正持有 doctrine 准入面。
- 错误映射补齐 `ResearchDoctrine*` 四类；同时发现并修复 P8a 的另一处遗漏：`_error_code` 的
  conflict/not_found 元组从未包含 `ResearchConstitutionConflict/NotFound`（message 侧有、code 侧缺），
  一并补上。writer ops 测试 20/20、doctrine 邻接全绿、全仓 **959/959**。

### 2. live 发布（2026-09-01，`human:lumos`）

- **DoctrinePack v1** `doctrine-pack-version:13a018582c…`（hash `81f0fbf7…426174`）：
  需求拐点透镜（bookings 轨迹 / AI reinvention / 托管服务转化 / 咨询下滑），evidence standard 与
  P8a manifest 一致（最低独立源 1、负面 Claim 只能候选）。
- **ResearchConstitution v2** `constitution-version:us-it-services:2`（prior=v1）：绑定升级为
  mandate p8a + thesis pack v2 + **policy-3**（现行 active，含 weekly brief 自动发布规则）+
  **doctrine pack v1**（P8a 时的 null 空缺就此关闭）+ weekly plan v3 hash。研究方法七要素正文不变。

### 3. 9/3 自动投递路径演练（真实执行）

首个自动 weekly brief 窗口（2026-09-03 07:00 America/New_York）前最大的未验证环节是 OpenClaw
bridge 的附件投递（`openclaw message send --media` 从未在 live 跑过）。演练按 bridge 的精确代码路径
执行：0600 权限 Markdown 附件写入 owner-only 附件目录 →
`openclaw message send --channel discord --account default --target channel:1481…7566 --message … --media …`
→ 返回 messageId `1544275074763329619`（含 Discord snowflake 时间推导，与 bridge 的
`_discord_delivered_at` 同源）。消息正文明确标注 `[DRILL]`、说明不复现。

## live 状态（2026-09-01）

- health `ok/running`；`bounded_planner: idle`（loop v2 已于 8/27 终态
  `evidence_observed_for_review`：ACN/CTSH/EPAM/IBM observed + DXC source_unavailable + EPAM 自动
  观察问题）；`weekly_brief: waiting`（正确等待 9/3 窗口）。
- 至此 9/3 窗口的全链条均已各自验证：coordinator 准入/issue/outbox（隔离 canary fresh→duplicate）、
  本演练的 --media 附件投递、以及 DeliveryReceipt 写入（S7f 测试 + 8/27 live 手工投递）。

## 下一步

1. **9/3 窗口值守**：观察首期自动 brief（delta 对照 w35、附件、DeliveryReceipt、心跳状态）。
2. 观察问题 → lane 触发的策略裁决（owner 决定 lane 的 binding actor 语义）。
3. P8c-4b：LLM planner（DeepSeek V4 Flash）与 doctrine ContextPack（含 CompanyResearchView 切片）
   接入循环。
