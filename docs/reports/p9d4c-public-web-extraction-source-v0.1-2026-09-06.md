# P9d-4c：已获取网页作为可核验的抽取来源（只读）

日期：2026-09-06
状态：development candidate；专项、全仓与 live Core 只读副本 canary 通过；**未部署、未写 live、0 网络调用**。
基线：`4fea0e4`（P9d-4b）；分支：`p9d4c-public-web-extraction-source`。
上游：[P9d-4b](p9d4b-public-web-fetch-lane-v0.1-2026-09-06.md)、[P9d-4a](p9d4a-web-search-mission-discovery-v0.1-2026-09-06.md)、[P9d-2 人工抽取队列](p9d2-document-extraction-review-queue-v0.1-2026-09-04.md)、[ADR-0003 B](../adr/0003-transcript-candidate-admission.md)。

## 这一片解决什么

P9d-4b 之后，被搜索引用的网页原始字节已经进 Core connector authority，文档转 `acquired` 并进入人工抽取队列——
但队列里点开它会被拒：`document_extraction._source_context` 只接受 AlphaEngine 文档。本片让**人能真正读到那一页**：
按与 transcript 相同的严格程度重新核验每一张 Core 回执，证明 spool 里的字节就是那次 invocation 记录的字节，
再把它们确定性地渲染成文本，供既有的窗口/引文机制使用。

**本片只让人读，不让机器写。** 网页不能触发模型起草，也不能进入候选 staging：那条链绑定 transcript 修正权威与
AlphaEngine 文档谱系，属于另一片工作。人可以读、可以按既有路径带理由 `dismissed`。

## 冻结边界

- **只信 Core。** 从 fetch manifest 出发逐跳校验：manifest → invocation（ref+hash）→ call spec / profile（invocation 内
  hash 绑定）；manifest → source envelope（ref+hash）→ raw artifact 与 fetched record；spool 对象必须 hash 成 record
  自己命名的 body hash。physical attempt / usage / quota settlement 由 invocation、reservation 反查，要求恰好一条
  `succeeded` attempt 与 `consumed` settlement。任一断链是拒绝，不是尽力渲染。
  与 AlphaEngine 路径的差别已知并记录：AlphaEngine 每页 manifest 直接 hash 绑定 9 类回执，web fetch manifest 只
  hash 绑定 3 类（invocation / profile / source envelope），其余经 hash 绑定的链路或 invocation 反查抵达。
- **渲染是确定性的，且拒绝猜测。** 只渲染 `text/html`、`application/xhtml+xml`、`text/plain` 且 UTF-8；PDF、图片、
  非 UTF-8 声明或非法 UTF-8 一律拒绝并给出原因，绝不把二进制解码成人可能引用的乱码。渲染器有自己的标识
  （`html-visible-blocks:0.1`），随 context hash 绑定，改渲染即让旧 context 失效。
- **脚本与隐藏内容不进人眼。** `script/style/template/noscript/svg/math/iframe/object/canvas` 内容整体丢弃；
  零宽字符（`​‌‍⁠﻿`）被删除，Unicode 空白折叠为普通空格——否则它们会隐形地藏在引文里。
- **上限与截断如实声明。** Cockpit 控制面把窗口 offset 限制在 60 万字符内，超出部分会不可达，因此渲染上限
  `MAX_SOURCE_CHARS=600000`，截断时 `source_truncated=true` 出现在 context 里。
- **不改 AlphaEngine 行为。** transcript 分支逐字未动；context 的既有键集不变，网页分支只**追加**
  `canonical_url / host / raw_media_type / body_sha256 / source_renderer / source_truncated`。
  网页没有自带的 `declared_content_sha256`，其 `source_content_hash` 是渲染文本的 sha256——引文该绑定的正是人读到的文本。
- **起草与 staging 对网页关闭。** `view` 返回 `generation_enabled=false` 与 `gate_reason=
  public_web_extraction_drafting_not_supported`；`generate` 在任何 route/预留之前返回 gated（不花预算）；
  `stage` 明确拒绝并说明原因。ADR-0003 B 不变。

## 实现

- `public_web_extraction_source.py`（新）：`verified_public_web_source`（逐跳回执核验 + 渲染）、
  `render_public_web_text`（确定性 HTML/纯文本渲染、媒体类型与字符集守门、零宽清理、截断声明）、
  `normalized_media_type`、`_PublicWebHtmlParser`（通用块级可见文本，非 transcript 专用）。
- `document_extraction.py`：`_source_context` 按 `review["source_ref"]` 分支（`SUPPORTED_SOURCE_REFS`），网页走
  `web_fetch_launcher.read_completed_manifest` + 新核验器；`view`/`generate`/`stage` 对网页分别只读、gated、拒绝。
- `cockpit_control.html`：待抽取队列把网页项显示为「公开网页 <短 hash>」而不是整条 opaque ref。

## 验收

- 新增专项 **10 条**（`tests/test_public_web_extraction_source.py`）：渲染（块级可见文本、脚本/样式/noscript 不入文本、
  确定性、纯文本、媒体类型解析、零宽与 NBSP 清理、截断声明）；四类不可渲染输入被拒；核验器 happy path 渲染出
  与原字节一致的文本；回执/谱系漂移（invocation hash、profile hash、host、profile ref、artifact ref）fail closed；
  借用另一页 envelope 被拒；spool 返回不匹配字节被拒；失败的 fetch 没有 manifest。
- `tests/test_public_web_fetch_lane.py` 的 writer 用例改写为真实端到端：经 writer socket `mission_document_evidence`
  返回带 `canonical_url`/`host`/`source_renderer` 的可核验窗口，引文文本与 `source_content_hash` 与渲染逐字一致；
  `generate_document_extraction` 返回 gated 且 `formal_authority_writes=0`；`stage_document_extraction` 被拒；
  过期 review hash 仍 fail closed。
- 既有 `tests/test_document_extraction.py` **17/17 原样通过**（AlphaEngine 路径未变）；邻接共 107/107。
- 全仓 unittest **1151/1153**：两条失败仍是既有 `test_document_extraction_preflight` 的 `/private/var` 路径断言，与本片无关。`git diff --check`、compileall 通过。
- wheel/sdist 构建；干净 venv `pip --no-index --no-deps` 安装后以 installed package 跑 36 条相关专项通过；
  wheel SHA-256 `55fcc42114385832ab21369fe5d6896129a29e8d314f146038dcf2248c09f039`。
- live Core 只读副本 canary `scripts/run_p9d4c_public_web_extraction_canary.py`
  （`temp/p9d4c/live-copy-canary.json`，`ok=true`，条件具名，全部为真）：副本发布 `probe_only` mission 版本后，
  真实 search child + 真实 fetch child 把一页（343 字节 rehearsal fixture）收进 authority 并登记
  `awaiting_human_extraction`；核验器渲染出 187 字符文本，两次渲染逐字一致，脚本/样式被排除，引文 hash 绑定
  精确文本；篡改 host 的 manifest 被 `PublicWebSourceConflict` 拒绝；Claim/Evidence/Thesis 与 AlphaEngine 行数不变，
  副本新增 1 条 web review，integrity ok；0 网络、0 付费、0 live 写入。

## 未做与边界

- **网页不能成为候选/Evidence。** staging 链绑定 transcript 修正权威（`transcript_correction_set_versions`、
  `stage_transcript_qualitative_candidate`）与 AlphaEngine 谱系；让网页进入候选需要它自己的引文/修正语义与
  一次 ADR 复核（ADR-0003 B 现在只讲 transcript）。本片明确拒绝而不是绕过。
- **模型起草对网页关闭**，因此没有验证模型在网页上的抽取质量或抗提示注入表现。
- **仍未部署，且 live 上没有网页可读**：live mission 的 `source:web-search` 仍 `not_connected`，且真实 OpenClaw
  gateway `web_search` handle（P9d-4a 遗留）未接线，因此没有真实获取发生过。
- 渲染只覆盖 HTML/纯文本 UTF-8；PDF 与其他编码的网页会被如实拒绝，接它们是后续工作。
- owner 已批准 `gemini-web-search-v1.json` 与 `web-fetch-v1.json`（live state 内两条记录现为 `approved`，
  approved_by `human:lumos`）；仓库 deploy 模板仍为 `proposed`。启用仍需发布 mission 新版本改 `source:web-search` 状态。

## 复跑

```bash
.venv/bin/python -m unittest tests.test_public_web_extraction_source tests.test_public_web_fetch_lane tests.test_document_extraction -v
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/run_p9d4c_public_web_extraction_canary.py \
  --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
  --output temp/p9d4c/live-copy-canary.json
.venv/bin/python -m build --no-isolation --outdir temp/p9d4c/dist
```
