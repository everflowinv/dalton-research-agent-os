# AlphaEngine 与待接来源只读审计（2026-09-14）

## 范围与口径

只读查询 `core.sqlite` 的 `connector_invocations`、`connector_physical_attempts`、`connector_usage_entries`、`connector_source_envelopes`，窗口为 **2026-09-07 00:00:00 UTC 至库内最后一条 2026-09-13 17:52:56 UTC**。另于 **2026-09-14 07:33:02 UTC** 计算 trailing 24h。未调用来源、未改配置或权限。

## AlphaEngine 实际消耗

- 7 日窗口创建 **565** 个 AlphaEngine invocation：`search_library` 143，`get_document` 422。
- 实际产生 physical attempt / usage 的是 **518** 个，runner 计量均为 1 call，共 **518 charged call units**、23,401,112 bytes、1,055 records；其中 **421 succeeded、97 failed**。
- **47 个 invocation 从未 dispatch**（search 46、document 1），没有 physical attempt、usage 或 provider call。但当前 `count_recent_alphaengine_calls()` 按 `connector_invocations` 计数，因此这些记录会占用 mission 的 130/24h 显示额度。这是可证实的 **47 个虚占额度**，不是供应商实际调用。
- 97 个实际失败调用全部计为 1 call、records=0、无 source envelope：search 53、document 44。它们是可证实的实际额度消耗但没有取得资料。
- 失败高度集中：同一 CTSH sell-side cursor 查询共 82 个 invocation，其中 36 次实际失败、46 次未派发；同一 CTSH earnings-call cursor 又实际失败 17 次；同一 document `alphaengine-doc:50001790274782` 实际失败 44 次。三组合计覆盖全部 97 个失败及46/47个未派发。这是主要的确定性浪费，而不是五家公司正常分散取数。
- 成功调用不能一概视为浪费：44 次搜索各返回20 records；377次成功文档页中175次记录1个文档，另外202次虽usage records=0，仍保存 complete/partial envelope和bytes，可能是分页正文，现有计量不足以判定无价值。重复query共有157个额外invocation，但除上述失败循环外，现有账本无法区分必要分页/重新获取与无效重复，故不计入“已证明浪费”。
- 当前 trailing 24h 只有 **1 invocation / 1 physical attempt**，即按mission 130上限尚余129。五家公司缺口现在已不是“额度仍满”；若仍无任务，应该检查正常 planner/source-discovery 在部署后的重新规划/调度结果，而不是扩大额度。

## 为什么五家公司仍缺电话会

项目已接受的只读统计仍是有效季度 **ACN 0 / CTSH 1 / EPAM 2 / IBM 0 / DXC 4**，因此欠缺分别为4/3/2/4/0。此前 mission v14 在额度满时保存了停止计划；账本证明额度后来释放，但 9月13日仅发生1次调用。最小修复应是：

1. 将24h消耗口径改为实际 physical attempt / final usage，而不是尚未派发的 invocation；
2. 对相同 query hash + cursor/document 的终态失败做冷却或确定性去重，尤其上述三组；
3. 在额度从0恢复为正数后，让既有 planner/source-discovery 正常重算并优先缺季度，不改130上限。

失败 attempt 的正式记录没有 provider error/detail 或 provider_request_id，所以无法仅凭Core说明这97次是额度拒绝、无权限还是上游错误；这是明确的缺失统计，不能猜测。

## Guidepoint、销售快报、公司 Wiki

- Guidepoint 本机代理确实安装且运行：LaunchAgent `ai.openclaw.guidepoint-mcp-proxy`，PID 54745，脚本读取既有环境后启动Python代理；两条治理文件 `guidepoint-search-library-v1.json` 与 `guidepoint-get-transcript-v1.json` 均为 `approved`。但当前 mission v14 的 `source:guidepoint` 仍明确为 `not_connected`，因此“代理在线/治理批准”没有转化为该mission可调度来源。最小修复是按既有批准把mission/lane接线落实并验证，不需要重复索取批准，也不能仅凭8943在线宣称已接通。
- 销售快报：`sales-notes-list-notes-v1.json` 与 `sales-notes-get-note-v1.json` 的真实 `status` 均为 `proposed`；字段里的 `approved_by`/`approval_ref` 是提案模板身份，不把状态提升为approved。现运行态明确报 `list_notes governance record is not approved`，因此确实在等现有治理决定。
- 公司 Wiki：没有 Dalton company-wiki LaunchAgent，也没有当前writer的 `source:company-wiki` lane；激活演练明确为 `company_wiki_feed unconfigured`，且期望的 `/Users/everflow/.openclaw/workspace/wiki-index.sqlite` 不存在。与此同时 OpenClaw 的 skill、资料和向量资产分别存在：`workspace/skills/company-wiki/wiki.db`、`workspace/wiki/vectors.db` 与 `workspace/wiki/`。这证明“资料/skill已安装”，不证明 Dalton runtime connector 已启用。最小修复是建立既有资料索引所需的明确 runtime connector/lane并通过治理门禁；不能把OpenClaw本地资产直接当成已连接来源。

## 证据位置

- Core: `/Users/everflow/Library/Application Support/Dalton/state/dalton-core/core.sqlite`
- Mission v14: `coverage-mission-version:us-it-services:14`, hash `0bae07a75bf1a7685d3dc9896317502aa3468eaa187e84ba9108c957983e6bb5`
- Guidepoint LaunchAgent: `/Users/everflow/Library/LaunchAgents/ai.openclaw.guidepoint-mcp-proxy.plist`
- Governance: `/Users/everflow/Library/Application Support/Dalton/state/dalton-core/connector-governance/`
- Existing accepted quota audit: `docs/reports/ctsh-alphaengine-rolling-quota-audit-2026-09-10.md`
- Source status: `docs/PROJECT_STATUS.md`（电话会有效季度与Guidepoint/Wiki边界）
# 2026-09-14 v16 运行更新

截至本次只读核对，v16 并未耗尽 AlphaEngine 配额。最近 24 小时共有 6 个连接器调用和 6 次物理尝试，全部成功，每个调用都只有一次物理尝试；当前任务依次完成 ACN、CTSH、EPAM，并已启动 IBM。最新调度 tick 的 `pool_exhausted` 为 0。界面中的 `carried_forward=3` 表示跨任务版本接续的三份资料，并非三次失败调用。

历史 v14 确有浪费：CTSH 卖方研报查询（查询哈希前缀 `df9b67c75d`）在供应商返回 `ConnectorQuotaExceeded` 后，约每五分钟重复一次。代码原因是资料仍有缺口时，较早成功页留下的分页游标可以绕过最新失败调度的重试间隔。R25e 候选提交 `ff6e6f78` 将即时续页限定为“最近一次调度成功且仍需下一页”；失败调度必须等待既定冷却期。该修复已集成到候选 `73bbfb00`，尚未部署。两项定向测试证明失败分页即使仍有游标也会等待，而成功分页仍能继续。
