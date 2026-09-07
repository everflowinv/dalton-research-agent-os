# P9d-7：PDF 抽取来源 + web search/fetch 自主化上线

日期：2026-09-07
状态：**已部署 live**；mission v4 已发布（`source:web-search` = `connected`）；
自动化已在 live 上自主完成 搜索 → 抓取 → 结算 → 人工审阅入队 的完整闭环。
提交：`d9c0e01`。上游：[P9d-5/6](p9d5-web-chain-deployment-and-live-rehearsal-2026-09-07.md)。

## 一、PDF 抽取来源

首次真实抓取拿回的是 PDF（第一手 IR 材料的常见格式），审阅面此前只能如实拒绝。本片补上：

- `application/pdf` 进入可渲染集合，经 **pypdf** 抽取；作为**可选 extra `pdf`** 加入 pyproject，
  核心依赖面保持为零（pypdf 在 Python 3.11+ 无运行时依赖），`install.sh` 改装 `[deploy,pdf]`。
- **fail closed 不猜**：extractor 未安装、文件加密、解析失败、超过 400 页上限、抽不出文本，一律带原因拒绝。
- **渲染器身份带抽取器版本**（`pdf-pypdf-6.17.0:0.1`）：升级 pypdf 会让旧 context 失效，而不是悄悄改变
  引文背后的文本。
- 真实验证：对 live authority 里那份 **14 页 Accenture 三季度业绩发布 PDF** 抽出 **35,471 字**，两次运行逐字一致，
  首段即 "New bookings of $21.1 billion…" 等研究可用内容。
- 单元测试自建最小合法 PDF（不依赖库构造），覆盖渲染、确定性、畸形/空/未安装 extractor 的拒绝路径。
- 全仓 **1166/1168**（两条既有环境路径断言）。

## 二、部署与开闸

1. `install.sh` 重装（含 pypdf 6.17.0）并重启四个服务，`dalton-health` `ok: true`。
2. 备份快照 `pre-web-connected-20260907`（core.sqlite，sha256 `e6202447…`）。
3. `dalton-gov create_coverage_mission`（`human:lumos`）发布
   **`coverage-mission-version:us-it-services:4`**（hash `ef242f1a…`）：只把 `source:web-search`
   由 `probe_only` 改为 **`connected`**，其余原样继承。

## 三、live 自主运行观测（开闸后约 20 分钟）

controller tick 每轮最多一次搜索 + 一次抓取，实际观测到：

- **4 次真实 Gemini 搜索、3 次真实公网抓取**（trailing-24h 合计 7，计划上限 40）；
- 文档状态推进：`discovered` → `acquisition_launched` → `acquired`；
- **1 条 `awaiting_human_extraction` 审阅入队**；
- 人工审阅面成功渲染自动抓取的页面：`quartr.com` 的 Accenture IR 页，`text/html`，
  渲染器 `html-visible-blocks:0.1`，**9,579 字、8 段引文**，窗口与引文 hash 正常；
- **模型起草仍 gated**（`public_web_extraction_drafting_not_supported`）、候选 staging 仍拒绝；
- **Evidence 6 / Claim 6 / Thesis 2 全程不变**，`integrity_check = ok`。

即：机器现在自己找资料、自己取原文、把可核验的原文排进人工队列；**没有任何东西自动变成 Claim 或 Evidence**，
ADR-0003 B 的人工准入边界未被触碰。

## 四、本轮暴露的两个既有设计后果（未修，需 owner 决定）

1. **发布新 mission 版本会"孤立"上一版本发现的文档。** `next_discovered_document` /
   `launched_discovered_documents` / `retryable_failed_document` 都 join `coverage_mission_pointer`，
   因此只看当前版本的行。v3 下人工发现的 **10 个 URL 现在是孤儿**（`mission_version_ref` = v3），
   自动化永远不会去取它们。这与 P9d-2「被替换的 mission 只是让文档没有 review」的既有语义一致，
   属于"权威绑定版本"的自然结果，但运营上意味着**每次发版都会丢下在飞文档**。
   可选修法（未做）：让获取路径接受"同一 mission_ref 的旧版本文档，只要当前版本仍授予该来源"。
   实践中影响有限：自动化在新版本下会重新搜到同样的 URL 并正常取回。
2. **搜索时已在 authority 的文档被记为 `already_in_authority`，永远不进人工队列**
   （`register_document_review` 只接受 `acquired`）。我手工抓的那份 Accenture PDF 正是如此：
   字节在 authority、可渲染，但没有 review 行。AlphaEngine 沿用同一语义。

## 五、用量与边界

- 计划上限 40 次/24h 由搜索与抓取**共用**；按每 tick 至多一次调用的节奏，满负荷下约一小时内用尽，
  之后该 lane 会 idle 到窗口滚动。这是 owner 设定的上限在起作用，不是故障。
- 抓取走无凭据公网 HTTPS；**transport 不读 robots.txt**，当前只按 mission 计划与配额约束访问频率。
- PDF 之外的二进制（图片等）仍被拒绝渲染。
