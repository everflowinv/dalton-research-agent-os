# P9d-3b：原文审阅与抽取建议离线链

日期：2026-09-05
状态：本报告记录 2026-09-05 的离线阶段，当时部分完成、未部署。后续预算准入、真实 broker 接线和人工 citation/staging 已在 [2026-09-06 接续报告](p9d3b-budgeted-broker-and-human-staging-v0.1-2026-09-06.md) 更新；真实模型 canary 仍未执行。以下保留当时记录，不代表接续开发后的现状。
上游：[P9d-3a](p9d3a-cockpit-document-review-v0.1-2026-09-05.md)、[ADR-0003](../adr/0003-transcript-candidate-admission.md)

## 本轮结果

Cockpit 待审文档新增原文分页与抽取建议展示。原文读取逐页核对 Core 连接器记录、原始字节、获取 manifest 和全文 hash；建议绑定文档版本、原文坐标、引文 hash 与模型执行记录。页面把原文当文字显示，不执行其中的 HTML。

抽取流程复用 Scheduler、ModelRouter 及现有模型 worker 的执行、记账和重放机制。当前只允许零费用 HermeticExtractionAdapter：生产 writer 不配置生成 worker，生成接口明确返回 gated。离线示例贯穿真实本地 HTTP 与 writer RPC，但不代表真实 LLM 已可用。

建议不是 CandidateStaging 或正式 Claim。qualitative 的 value/unit/scale 为 null，不自动建立 citation、不自动入 staging、不关闭文档 review、不 accept Claim。后续 citation 与正式入库仍需人工确认。

## 验收证据

- 全仓 unittest：1056/1056；日志 `temp/p9d3b/full-regression-final.log`。
- 专项与关联测试：88/88；日志 `temp/p9d3b/targeted.log`。与全仓有重叠，不相加。
- OpenClaw model broker：25/25；日志 `temp/p9d3b/broker.log`。
- 真实本地 HTTP / writer RPC canary：原文读取、生成关闭、CSRF、automation 拒绝、过期状态均通过。fixture 前后 Claim、Evidence、citation、correction 及连接器调用计数不变，integrity ok。
- 浏览器自动交互：1280px 桌面和 390px 手机通过翻页、重载重放、过期后清空旧原文及 XSS 测试；无横向溢出、页面错误或外部请求。结果 `temp/p9d3b/browser-final/result.json`。
- wheel/sdist 构建成功；干净 Python 3.13 安装后的 canary 通过，实际 import 来自新环境 site-packages。结果 `temp/p9d3b/installed-canary/result.json`。
- 既有 hermetic research replay 通过，0 provider calls。结果 `temp/p9d3b/hermetic-replay.json`。
- 父会话读回以上日志、JSON 与关键安全路径；`git diff --check` 通过。

首轮两个 schema 元信息错误已修复，以上数字均来自最终日志。

## 未完成与限制

1. 真实模型的 mission 费用预算准入及 broker 接线未实现，生产生成硬关闭；真实抽取质量和抗 prompt injection 表现未验证。不能称 P9d-3b 全部完成。
2. 人工 citation / CandidateStaging 编辑入口未实现；本片只有原文与建议审阅，不是完整的人工抽取提交链。
3. 截图人工目视检查未完成：view_image 拒绝项目路径访问，未复制截图或换工具绕过限制。浏览器程序化交互测试已通过，不等于人工视觉验收。
4. 验收仅使用离线 fixture，未做 live 数据副本验收。没有真实研究模型或连接器调用、live 数据写入、服务重启、部署、远端 push 或 PR / CI 验证；未改 mission、预算、权限配置与 docs/external。

## 复跑

```bash
.venv/bin/python -m unittest discover -s tests -v
(cd integrations/openclaw-model-broker && npm run check)
.venv/bin/python scripts/run_hermetic_research_replay_canary.py
.venv/bin/python -m build --no-isolation --outdir temp/p9d3b/dist
.venv/bin/python scripts/run_p9d3b_document_extraction_canary.py --output temp/p9d3b/replay
```

带浏览器运行时，先查 canary 的 `--help`，传入本机已安装且允许访问的 Chromium；不下载浏览器，也不为看图修改权限。下一开发片先补真实模型费用准入和人工 staging 入口，继续保留人工 accept 边界。
