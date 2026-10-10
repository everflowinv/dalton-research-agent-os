# ADR-0016: Cockpit v2, a workbench for non-technical financial analysts. One shell at `/v2` with five analyst pages and a module contract that the administrator interface and the financial charts plug into

*2026-10-08*  ·  Status: proposed (owner decision of 2026-10-08: build a new version of the cockpit for analysts, per `docs/reports/cockpit-analyst-ux-review-2026-10-08.md`)  ·  Builds on ADR-0006 (owner cockpit), the 2026-10-08 analyst UX review and its controls/evidence companions, and batch z (fresh-workspace reads never 5xx)

## Context

The review concludes that the cockpit is still organised around running the research system, not around reading research and making decisions. The main findings:

- Navigation follows internal stages (G01).
- One green light covers blocked research (G02).
- URLs do not move (G03), scroll positions leak between pages (G04), and layouts overflow at 1024/768 (G05).
- Clickable `div`s cannot be reached by keyboard (G08).
- Drafts are lost on refresh (H06, S05, X03).
- Company research lives in a 900 px dialog (C01, G11).
- To-do cards ask analysts to choose model chains or run `--apply` (D02).

The owner decided to build a new version rather than keep patching the old page. Three builders work at once:

- **A** builds the shell, the information architecture, the analyst pages and the copy (this ADR).
- **B** builds the administrator interface.
- **C** builds the financial charts and tables.

They need one contract so that nobody edits anybody else's files. The old cockpit stays at `/` while batch ab fixes its interaction defects.

## Decision

### 1. Information architecture

```
研究项目（顶栏切换器：/v1/cockpit/workspaces，切换 = 打开另一项目的 /v2）
状态条：后台在线 · 研究受阻 N 项 · 数据更新于 …（三件事分开，G02）
├── 研究总览   #/overview          研究结论 → 重要变化 → 待我决定 → 公司比较
├── 公司研究   #/companies         可排序公司表（回报/变化/风险，“无法比较”标明）
│   └── 公司页 #/company/<代码>[/<章节>]   独立页面 + 目录（不是弹窗）
│        投资观点 → 估值与回报 → 风险与下一验证事件 → 证据 → 财务模型 →
│        事件 → 情景与触发 → 回报归因 → 研究报告；来源/原文在可返回的侧栏
├── 研究问答   #/ask               结论 1–3 条 / 尚不能确认 / 主要依据；历史可搜索、重问、追问
├── 待我处理   #/tasks             一个列表，按影响和紧急程度排序；按 研究决策 / 资料补充 /
│                                  服务问题（由管理员处理）/ 系统正在处理 分组
└── 研究设置   #/settings          只有 研究范围（目标、覆盖名单）、费用、投资建议规则；
                                   管理员入口由 settings.admin_link 插槽提供
管理员界面   /v2/admin（B）         AI 服务、连接器、凭证、诊断、重启 —— 分析师页面不出现
```

Every page, company, section and filter has a stable hash URL. Examples:

- `#/company/ACN/model`
- `#/companies?sort=change&dir=desc`
- `#/tasks?group=urgency`
- `#/company/ACN/evidence?aspect=demand_drivers`

A link pushes a history entry. A sort or filter replaces the current entry. Navigation rules:

- **Back/forward:** restores the page's own scroll position.
- **New page:** opens at the top, with focus on its `h1`.
- **Section link:** scrolls to the section and focuses its heading.

### 2. Serving and the route contract

`agenda_control._handler` calls `cockpit_v2.routes.register_v2_routes(application)` once. It hands every `GET /v2…` to the router, and every `POST /v2…` that an extension registered. This happens after the same host check, Tailscale login and session as `/`, so v2 has the same authentication. JSON from the router gets the session's `csrf_token`, exactly like `/v1/cockpit/*`.

v2 pages read the existing `/v1/cockpit/*` payloads and write through the existing `POST /v1/cockpit/*` actions (`ask`, `goal`, `publish`, `decide`, `stop_line`). Payload shapes are not changed. A new endpoint goes under `/v2/data/*`, and only when an existing payload lacks a field.

| Route | Owner | Served by |
|---|---|---|
| `/v2`, `/v2/` | A | `cockpit_v2/shell.html` |
| `/v2/static/<path>` | all | files under `cockpit_v2/` (`.js .mjs .css .html .json .svg`; no `..`, no dot-files, never `.py`) |
| `/v2/data/manifest` | A | the shell's modules, extension modules, extension pages |
| `/v2/admin` | B | registered by `cockpit_v2/admin/routes.py` |
| `POST /v2/data/admin/action` | B | registered by `cockpit_v2/admin/routes.py` |
| `/v2/data/charts/...`, `/v2/assets/charts/...` | C | registered by `cockpit_v2/charts/routes.py` (handlers in `cockpit_v2/charts_api.py`) |

Extensions join by module name, not by editing `routes.py`. `register_v2_routes` imports `dalton_core.cockpit_v2.admin.routes` and `dalton_core.cockpit_v2.charts.routes` if present (`ImportError` for a missing package is quiet). For each module it then:

1. calls `register(router)`, or `register_admin_routes(router)` / `register_chart_routes(router)` when the module has no `register`;
2. adds every URL in the module's `V2_MODULES` and `V2_STYLES`, if it has them;
3. adds the extension's known front end from `EXTENSION_FRONTENDS`, but only when the router actually serves those URLs. For the charts this is `/v2/assets/charts/charts.mjs` and `charts.css`, so an absent extension costs no 404.

All of this runs inside a `try`, so a broken extension cannot take the cockpit down. The outcome is recorded in `manifest.extensions`.

The router API:

```python
router.add_page("/v2/admin", Path(...)/"index.html")          # a page under /v2
router.add_json("/v2/data/charts/", handler, prefix=True)      # handler(app, login, query, path) -> dict
router.add_get("/v2/assets/charts/charts.mjs", raw)            # raw(query, context) -> (status, content_type, bytes);
                                                               # context = app.cockpit_plane; served as is
router.add_post("/v2/data/admin/action", handler)              # handler(app, login, session, csrf, body, path) -> dict
router.add_module("/v2/assets/charts/charts.mjs")              # ES module (.js/.mjs under /v2/) the shell imports
# The router checks the session's CSRF token before any POST handler runs; the
# HTTP surface checks content type (application/json) and body size.
# CockpitError -> 400 with display_message; CockpitConflict -> 409;
# PermissionError -> 403; unknown path -> 404.
```

### 3. The front-end contract (`window.DaltonV2`)

`shell.js` is an ES module.

**Boot sequence.** It reads `/v2/data/manifest`, adds `manifest.styles` as stylesheets, imports the shell's page modules and then every module in `manifest.modules`, and then routes. The chart module calls `window.DaltonV2.registerWidget` itself when it loads.

**Events.** The company page listens for `dalton:statement-line` (`detail: {ref, concept, accession, label}`), which the profit-bridge chart bubbles up. It responds by opening 证据 filtered to the company's filed statements, with the matching lines first and marked: `#/company/<代码>/evidence?importance=filing&line=…`.

**Rendering.** A page's `render(container, ctx)` is awaited. When a page has `update(ctx)` and only the section or filters changed, `update` is called instead and the page is not re-rendered.

**Registration functions:**

| Function | What it does |
|---|---|
| `registerPage(id, {title, nav, render, route, params, parent, update})` | `nav` is `{label, order}` or `null`. `params` names the hash segments (the company page uses `["company","tab"]`). `parent` decides which navigation item is highlighted. |
| `registerWidget(slot, render)` | `render(container, data, ctx)`. Returning `false` means "not applicable". A thrown error is logged and the page's fallback table is drawn instead. |

**Widget slots and the data each receives:**

| Slot | Data |
|---|---|
| `overview.header` | `{overview, needsHuman}` |
| `overview.compare` | `{overview, rows}` |
| `company.summary` | `{company, research}` |
| `company.valuation` | `{company}` |
| `company.model` | `{company, model}` |
| `company.scenarios` | `{company}` |
| `company.returns` | `{company}` |
| `settings.admin_link` | `{overview}` |

In `company.summary` and `company.valuation` the widget is added below the page's own content: the view, its review state, and the valuation and return tables always stay. In every other slot the widget replaces the fallback table.

`company` is one entry of `/v1/cockpit/overview` `companies[]`, `research` is `/v1/cockpit/research`, and `model` is `/v1/cockpit/model`. Every `company.*` slot also receives `company_ref`.

**Data access:**

- `api(path, {fresh, timeoutMs})` is a cached GET (30 s). It throws an `Error` whose `display_message` is in Chinese. A successful read of a `/v1` payload also resolves reviewed UI translations, keyed by the same 16-hex SHA-256 the server and the old cockpit use, so existing translations apply unchanged.
- `api.post(path, body)` sends the CSRF header.
- `api.job(id)` polls `/v1/cockpit/job`.
- `api.text(s)` returns the reviewed translation, or `s` if there is none.

**Helpers:**

- **`fmt`** — `money`, `usd`, `percent`, `change`, `pp`, `eps`, `ratio`, `fraction`, `probability`, `number`, `typed`, `unitHeader`, `date`, `datetime`, `ago`, `zone`. `money`, `percent`, `eps` and `typed` are ports of `numeric_display.format_display_number` and `format_typed_value`, and tests check that the two agree.
- **`state`** — `get()`, `set(patch, {replace})`, `setFilter(k, v)`, `href(patch)`, `subscribe(fn)`. It holds `{environment, page, company, tab, filters}`, all of it in the URL hash except `environment`, which is the server's current project.
- **Other helpers** — `copy`/`say`, `ui` (`section`, `table`, `field`, `kv`, `tag`, `alert`, `button`, `link`, `errorBox`, `receipt`, `glossed`, `paragraphs`, `download`), `h`, `side.open({heading, render})`/`side.close()`, `drafts.bind(input, key)` (sessionStorage per project and object), `adminHref(params)`, `navigate`, `setBadge`, `announce`, `requestId`, `slots`, `manifest`.

### 4. Copy

`copy.js` carries every row of section 8 of the review in `COPY_TABLE`: current wording, suggested wording, note, how it is applied, and the rules. `copy()` applies those rules to server-written text, never to text inside 「」 (names people gave, quotations), and is idempotent. Not every row is a text replacement:

| Row | How v2 handles it |
|---|---|
| 「少数股东损益」 | kept as is |
| 「USD 与 million 混合」 | handled by `fmt` |
| 「同一“研究内容”标题重复」 | handled by the page layout: debate sections are grouped by question, with 看多/看空/市场观点/我们的判断 |
| 「不加 --apply 是只读预览」 | dropped from analyst pages |

Finance terms (EPS, ROIC, CapEx, 10-K, 10-Q, 8-K, EBITDA, GAAP, FCF, P/E, TTM) are kept and glossed on first use per page. No v2 page offers an action the system does not implement:

- **No admin page installed:** the hand-off is "copy a summary for the administrator" and does not pretend to send anything.
- **Choosing a company to ask about:** writes the company name into the question, and the page says so.
- **Confirming a goal:** says it does not change the coverage list.

### 5. Accessibility and layout

- **Colour:** one set of semantic colour variables. Every text/background pair is at least 4.5:1, and tests check it. Colour never carries meaning alone: changes carry a sign and states carry words.
- **Controls:** real `button` and `a` elements throughout. Section links and sort controls carry `aria-current`, `aria-pressed` and `aria-sort`.
- **Fields:** labels are persistent, and validation messages appear inline next to the field and are announced.
- **Status:** status messages are announced, and results stay on the object they belong to.
- **Side panel:** Escape closes it and focus returns to the control that opened it.
- **Breakpoints:** layouts are tested at 1280, 1024 and 768. The main column has `min-width:0`, and only tables scroll sideways.

## Consequences

- The old cockpit is unchanged except for one 「试用新版」 link in its top bar.
- B and C can ship independently:
  - A missing extension leaves the fallback tables.
  - A broken extension is reported in the manifest and never breaks `/v2`.
- The fresh-workspace canary covers v2 in two ways:
  - It scans `agenda_control.py` for `path == "/v2"`.
  - `tests/test_cockpit_v2_routes.py` reads every `/v1` path the v2 front end uses from its source.
- Not done here:
  - The administrator interface (B).
  - Real financial charts and the semantic model table (C). Until then the model shows the lane's text table, with row names cleaned up.
  - A unified scope-change draft that adds companies (H08).
  - Uploading a missing transcript from the analyst side (D03). The card hands this to the administrator instead.
  - Analyst usability testing (section 10).

## 增补（批次 ad，2026-10-08）：v2 成为默认入口

owner 2026-10-08 指示「用 v2 替换原先的旧版 cockpit」。自批次 ad 起：

| 路径 | 批次 ac | 批次 ad 起 |
|---|---|---|
| `/` | 旧版控制台 `cockpit_control.html` | v2 分析师外壳（与 `/v2` 同内容） |
| `/admin` | 404 | v2 管理员页（与 `/v2/admin` 同内容） |
| `/v2`、`/v2/`、`/v2/admin` | v2 | 不变，作为别名继续可用 |
| `/v2/static/…`、`/v2/assets/charts/…`、`/v2/data/…` | v2 | 不变（前端文件无需改路径） |
| `/legacy` | ADR-0006 运维审阅页 `cockpit_control_legacy.html` | 旧版控制台 `cockpit_control.html`，`/v1/cockpit/*` 全部不变 |
| `/legacy/review` | — | ADR-0006 运维审阅页（原 `/legacy`），只读历史 |
| `/weekly-brief` | 每周简报页 | 不变 |

- 别名由 `cockpit_v2/routes.py` 的 `V2_ALIASES` 定义，直接返回同一份内容而不是 302，所以 `/#/company/<代码>/<章节>` 这类 hash 地址在 `/` 与 `/v2` 下都成立；扩展模块仍只在 `/v2` 下注册。认证（Tailscale 身份、Host 检查、会话 cookie）与其它路径完全相同。
- 旧版顶栏「试用新版」改为「返回新版」，指向 `/#/overview`；v2 外壳的「返回旧版」指向 `/legacy`，管理员入口指向 `/admin`。
- 飞书推送里的链接改为新版深链：「需要你处理」摘要 → `/#/tasks`；紧急停机/恢复全部运行 → `/admin#run`；模型回退通知 → `/admin#ai`；每周简报 → `/weekly-brief`（不变）。
- 仍只在 `/legacy` 的功能：上传新的基金 Excel 模板；自然语言方向调整（steer）；编辑发布投资建议策略；单条结论反馈；情景修订；研究轨迹、运行复盘与运行详情面板；每周简报设置页的入口链接。
- 空白工作区与新工作区 canary 覆盖 `/`、`/admin`、`/legacy`、`/legacy/review`，不得出现 5xx。

## 增补（迁移批次，2026-10-08）：旧版独有功能全部迁入新版

owner 指示「你说的那些项目也都迁移过来吧」。上文「仍只在 `/legacy` 的功能」清单清空：模板上传、方向调整、投资建议规则编辑进入「研究设置」（新增「报告模板」「每周简报」两区）；单条反馈（初步研究报告、每条证据、问答回答）、情景整组修订（`?edit=probabilities`）、单家公司导出模板、事后复盘/专项研究/跟踪频率进入公司页；研究轨迹与每周复盘成为研究总览的抽屉（`?panel=trajectory|reflection`，外壳 `side.open` 增加 `onClose`，换页时静默关闭）；逐件失败台账进入管理员「健康与故障」；审批卡补齐投资建议三部分与核验、复盘依据。所有写入复用既有 `/v1/cockpit/*` writer 操作（`feedback`、`scenario_revise`、`conviction_policy`、`steer`/`publish`/`decide`、`fund_template_register`、`fund_template_select`），唯一新增的是只读的 `GET /v2/data/feedback`（为证据条目提供反馈所需的内容哈希，`cockpit_v2/feedback_data.py`）。`cockpit_control.html` 行为不变，作为后备留在 `/legacy`；`/legacy/review` 作为已退役流程的历史保留。细节与地址见 `docs/reports/cockpit-v2-migration-2026-10-08.md`。
