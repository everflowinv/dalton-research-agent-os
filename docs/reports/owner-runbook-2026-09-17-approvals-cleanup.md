# 2026-09-17 待办清理 runbook（owner 执行的部分）

自动模式的分类器把"批准治理记录 / 授权付费恢复 / 发布任务版本"这类动作拦在执行前，以下由 owner 在终端跑（Claude Code 里用 `! <命令>`）。代码侧的修复见 `docs/PROJECT_STATUS.md` 顶部条目。

```zsh
cd ~/Projects/dalton-research-agent-os
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
```

## 1. 港交所四条数据源治理记录（needs-human 里 4 条「数据源接口等你批准」）
当前代码已认识这四种连接器（`load_connector_governance` 能加载），批准无副作用：本研究目标没有港股，`dispatch_mission_hkex_filings` 车道批准后也没有对象可跑。批准只是让待办不再每轮提示。
```zsh
for f in "$S"/connector-governance/hkex-filings-*-v1.json; do
  .venv/bin/python -m dalton_core.connector_governance_cli approve --path "$f" --approved-by human:lumos
done
```

## 2. `source:company-ir`「研究目标点名要的来源还没接上」——二选一
事实：研究目标 v24 的 `source_plan` 把 company-ir 写成 `not_connected`；IR 页面监视连接器（ir-page-watch）治理记录已批准，但 writer 没有 `--ir-page-declaration`（状态目录里没有 `ir-pages.json`），而且本机 changedetection.io（127.0.0.1:5055）现在要求 API key，连接器客户端按设计不带凭证——所以它此刻确实没接上。earnings release 本身是 8-K Exhibit 99.1，SEC 车道已允许 8-K（`SEC_ALLOWED_FORMS`），不接 IR 页面并不丢这条材料。
- **选项 A（推荐，一条命令）**：把 company-ir 从来源计划里去掉，发布研究目标 v25，其余字段逐字节不变：
```zsh
.venv/bin/python scripts/set_mission_source_status.py --state-dir "$S" --source source:company-ir --remove            # 预览
.venv/bin/python scripts/set_mission_source_status.py --state-dir "$S" --source source:company-ir --remove --apply --actor human:lumos
```
- **选项 B（真正接上）**：在 changedetection 的设置里关闭 API key 要求（或改客户端支持 key）；确认 `deploy/phase9/p9-us-it-services-ir-pages-v1.json` 里五家公司的 IR 页面 URL；`cp` 到 `"$S/ir-pages.json"`；重渲染 writer plist（见 deploy-runbook-2026-09-16 第 3 步）并 `launchctl kickstart -k gui/$(id -u)/space.lumos.dalton.writer`；看到 `tick_ledger_lanes` 里 `dispatch_mission_ownership` 出现 IR sweep 计数后，再用同一脚本把状态改为 `connected`（`--status connected`）。

## 3. `mission_document_research` 车道「停着等授权」（19 条 admission，原因 paid_send_output_contract_failed）
这些 admission 已经发起过付费模型调用、输出没过契约；系统按设计不自动重试。车道自己的说明（`OWNER_AUTHORIZATION_NOTE`）写明：目前没有任何 CLI 或 writer 操作能下发这条授权。要解除需要新开发一个 writer 人工治理操作 + CLI（按 admission 生成绑定 work order 哈希的授权、封顶费用）。本轮自动模式不允许我起草这部分（分类器判为付费交易类），需要你明确说"做这个工具"，我再开发；或者接受这 19 条一直停着（它们只影响文档研究这一条车道）。

## 4. 部署本轮代码修复（见 PROJECT_STATUS 09:30 条目）后，回到待办页
- DXC 那张：退回按钮现在会预填「证据已更新」的理由，一键即可；或直接「通过」。
- IBM 的投资 call 提案：有了「采纳 / 驳回 / 暂缓」按钮。
- 各环境的「需要你处理」只显示本环境的事项。
