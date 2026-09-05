# P9d-3a：Cockpit 文档抽取审阅入口与历史队列恢复

日期：2026-09-05
状态：开发完成；尚未部署 live
上游：[P9d-2](p9d2-document-extraction-review-queue-v0.1-2026-09-04.md)、[ADR-0003](../adr/0003-transcript-candidate-admission.md)

## 结果

Cockpit「待审」页现在可显示当前任务的待抽取文档，按公司 ticker、来源和入队时间定位；用户可选择同一份文档的已有候选并登记抽取完成，也可写明原因后忽略文档。登记完成不等于接受候选，更不会直接生成正式 Claim。

本轮也发现并修复了历史队列遗漏：live 在 9/5 只读检查时已有 7 份 `acquired` 文档、2 份获取失败、124 份待获取，但 `coverage_mission_document_reviews` 仍为 0 行。P9d-2 只在新获取任务 settle 时注册 review，部署前已获取的文档及注册前中断的任务不会自动补齐。9/4 报告中「部署后首个 tick 已生效」没有证明 review 真正落库，本报告纠正这一点。

## 实现

- `GET /v1/mission-document-review`：读取 discovery plan 对应 active mission 的最早 100 条待处理记录，达到上限时明确提示；候选最多扫描 500 条，同样提示可能未列全。读取失败单独显示，不影响其他 Cockpit 页签。
- `POST /v1/mission-document-review/decision`：复用既有 Tailscale 身份、session、CSRF 和 ephemeral human governance；身份由服务器派生，客户端不能指定 actor。没有新增常驻 principal 或权限集。
- Core 读投影给每行附上原始 review 的 hash 和匹配候选。正式登记在 writer 重读 CandidateStaging 与 Core citation authority：必须是 exact、staged/committed 的 qualitative candidate，同公司、同来源、同原文，citation hash 一致。拒绝用稳定 ref 暗中解析到其他版本。
- `expected_review_hash` 在实际状态迁移的事务中复核；页面状态改变后拒绝写入。完全相同的决定仍可幂等重放，但相同 resolution 携带不同候选或理由会冲突。Core 也强制 `dismissed` 必须有理由，不仅是页面校验。
- `backfill_document_reviews` 每 tick 最多补 100 条 active mission 中「acquired 但没有 review」的文档。每条仍调用原注册路径重验 `source_discovery` 授权；失败如实记录为 `not_registered`。补队列不使用获取预算、不发网络请求、不重新打开已处理记录。

## 验收

- 全仓 unittest：**1039/1039**。新增实际 Core citation + staging 的匹配/拒绝测试，以及模拟「获取已完成、队列注册中断」的恢复测试；原 HTTP 测试补新路由、共享 session 与 CSRF 拒绝。
- OpenClaw broker：**25/25**。
- live Core 与 CandidateStaging 各自 SQLite 只读 backup 后，在临时副本启动真实 writer RPC：历史 7/7 补齐，重跑无重复；测试页 hash 过期、automation 越权、首次决定、重复决定和改变理由的冲突。
- 副本前后：Claim 6、Evidence 6、Thesis 2、connector invocation 38，均未改变；`PRAGMA integrity_check=ok`。0 付费、0 外部网络、0 live 写入。
- 浏览器：本地 HTTP + 已缓存 Chromium，1280px 桌面与 390px 手机；没有横向溢出或 JavaScript 错误。无候选时禁止登记完成、空理由不提交、选择候选后发送 exact ref/hash、成功后队列刷新、单个接口失败不影响其他区域，均通过。页面验收使用明确的示例文案，不代表真实研究结论。
- wheel/sdist、干净 Python 3.13 安装与 hermetic replay 结果记录在本片验收产物中。

验收产物：`temp/p9d3a-unittest-final.log`、`temp/p9d3a-live-copy-canary-final.json`、`temp/p9d3a-browser-result.json`、`temp/p9d3a-desktop.png`、`temp/p9d3a-mobile.png`、`temp/p9d3a-build.log`。临时文件不进 Git；可重放的副本 canary 在 `scripts/run_p9d_document_review_canary.py`。

## 边界与下一片

本片没有自动抽取、生成 correction set、绑定 citation 或接受 Claim；也没有新增付费源、提高预算、修改 mission 或重启 live。7 份文档目前仍只在验收副本中补队列，部署新 controller 后才会恢复线上队列。

当前页面是任务清单与已有候选的关联入口，不是原文全文阅读器或抽取编辑器。下一片 P9d-3b 接 LLM 抽取建议及其原文证据展示，正式入库继续经过人工确认。web search 先于 Guidepoint、M2 市场数据等待 owner 解冻的顺序不变。
