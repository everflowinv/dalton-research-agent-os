# OpenClaw 数据源盘点：哪些该接进 Dalton 的 connector

日期：2026-09-09
状态：只读调研，供 S 线（来源补齐）排期；由 Opus 5 subagent 完成，主 agent 整理
范围：`~/.openclaw/workspace/skills/*`、gateway 配置结构、cron / tasks、`~/.agent-reach`；未读取任何密钥值
owner 已点名要接的：sales note、员工调研、Twitter、雪球、cn-hk-findata

---

## A. 数据源清单

| Skill / 资产 | 数据 | 市场 | 取法 | 认证（只列槽位名） | 配额 | 证据层级 | Dalton 模板 | transport | 建 lane 还缺 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **market-digest** | GS / Jefferies / BofA 的 sales 与研究**邮件**全文 | US、CN/HK、KR、宏观 | `gws` CLI → Gmail API，按发件域过滤；Gemini embedding；MBOX 回填 | `gws` OAuth store（`~/.config/gws/`）；`GEMINI_API_KEY` | 每次 50 封；**读过即打 label，只回一次** | **卖方（具名分析师）** | 无，新建 | `host_tool` | 治理记录、host bridge、child CLI、`gws` 凭证槽 |
| **company-wiki** | 55 份管理层会议纪要、43 份专家访谈、42 份券商报告、26 份季度笔记、2 份 NDR、2 份买方；135 家公司含 ACN；258 MB | US/HK/A | 本地 markdown + SQLite；经 Discord `#wiki` cron、`transcript-polish`、Flomo 导入 | `GEMINI_API_KEY` | tagger 30 RPM | **管理层 / 专家 / 卖方** | 无，新建（人工投喂） | `host_tool` | 治理记录、child CLI、文档类型 → 证据层级映射 |
| **guidepoint-transcript-search** | 专家访谈问答摘录（transcript_name / date / inquirer / respondent / question / answer / reference_url / source_attribution） | 全球 | MCP `search_library` → 本地 OAuth 代理 `127.0.0.1:8943/mcp`（LaunchAgent） | `GUIDEPOINT_CLIENT_ID` / `_SECRET` + token 缓存 | 未声明；**逐字引用 ≤20 词**的许可规则 | **专家 / 准一手** | `guidepoint` 已批 | `mcp_managed` | child CLI、discovery plan、launcher、writer / installer 接线、mission 版本置 connected |
| **alphaengine**（MCP；`-official` 已退役） | 卖方研报与评论、会议纪要、公告 | US/HK/A | MCP `127.0.0.1:8950/mcp`；token 只在 Desktop 进程内存 | Desktop 会话，无环境变量 | **每会话约 20 次 `get_document`**；owner cap 130/24h；`sort:newest` 会破坏相关性 | 卖方 | `alphaengine` 已接 | `mcp_managed` | 无（live） |
| **findata-analyst** | SEC filing、三表、XBRL facts、全文检索、附件、**Form 4** | US | `edgartools>=5.17.1` | `EDGAR_IDENTITY` | 被动退避 | **一手** | `sec` + `sec-financials` 已接 | `public_https` | 无（等价物已 live） |
| **company-filings-alert** | 8-K / 6-K / 10-K / 10-Q / DEF14A / S-1、**Form 4、SC 13D/G、Form 144**、A 股（akshare）、港股（Gemini `site:hkexnews.hk`） | US/A/HK | findata-analyst 子进程 + `edgar_enrich.py` 解析 `primary_doc.xml` | `EDGAR_IDENTITY` | 无 | **一手** | 扩 `sec`（新 operation） | `public_https` | 按 operation 各发一条治理记录；港股一路是检索而非一手 |
| **13f-tracker** | 13F-HR 持仓、季度变动 | US | `edgartools form="13F-HR"` | `EDGAR_IDENTITY` | 退避 | **一手（监管）** | 扩 `sec` | `public_https` | 新 operation + 治理 |
| **cn-hk-findata** | cninfo 公告列表 + 全文 / OCR；87 个 akshare intent：报表、资金流、融资融券、回购、股东、行情、AH 溢价、北向 | A / HK | cninfo `topSearch/query` + `hisAnnouncement/query`（POST、普通 UA、**无 cookie**）；`akshare>=1.16`；vendor 回退链 腾讯 → 新浪 → 同花顺 → 雪球 | **无** | 1 次重试；按 (source, capability) 健康度，3 次失败降级 5 分钟；「不要批量探测东财」 | 一手（交易所）/ vendor | `cninfo` 已有 + `xueqiu` 回退 `host-tool:cn-hk-findata-xq-hot-rank` | `public_https` + `host_tool` | akshare 的 op **不在** cninfo 模板里；需新模板或 host_tool child CLI + 治理 |
| **agent-reach**（pipx；`~/.agent-reach/config.yaml`） | 13 个渠道：雪球、B 站、V2EX、GitHub、YouTube、RSS、Exa、Jina reader、小红书 / LinkedIn（MCP 未注册）、Twitter（委托）、Reddit（委托） | CN / 全球 | Python CLI | `xueqiu_cookie`、`groq_api_key`、`twitter_auth_token` / `twitter_ct0`、代理 | doctor 报 7/15 可用 | 大众 | `xueqiu`（`host-tool:agent-reach-xueqiu-channel`） | `host_tool` | child CLI + `xueqiu_cookie` 凭证槽 |
| **`scripts/xueqiu_posts.py`** | 雪球帖子 / 评论 / 搜索 / 用户时间线 | A/HK | `api.xueqiu.com` 直连 | `xueqiu_cookie`（同一文件）；**无 cookie 返回 400** | — | **大众 / 匿名** | `xueqiu`（posts op） | `host_tool` | 主站在阿里云 WAF 后，只能走 API 主机 |
| **`xreach`**（npm，`/opt/homebrew/bin/xreach`） | X 时间线、单条、线程、关键词搜索 | 全球 | Node CLI | `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | 无 doctor | 大众 + 管理层 | `x-xreach`（shadow） | `host_tool` | child CLI、凭证槽、治理 |
| **`x_search`**（gateway 内置工具） | X 语义搜索 + 媒体分析，xAI SuperGrok OAuth；**不在** `openclaw.json` MCP 列表 | 全球 | gateway 内置 | gateway 持有的 OAuth | `allowed_x_handles` ≤20；cron 上限 2 次精确查询 | 大众（合成、不可枚举） | `x-x-search`（shadow） | `host_tool` | 需要能看见 gateway 工具的 host bridge（新 bridge ref / hash，不能复用 AlphaEngine 的） |
| **last30days** | HN、YouTube、TikTok、StockTwits、Polymarket、Bluesky、小红书、GitHub、web。**Reddit 已死** | 全球 | 约 50 个 provider 库 + vendored `bird-search` | `~/.config/last30days/.env` 多把 key；`FROM_BROWSER=off` 锁定 | 按源 `source_status` | 大众 / 匿名 | `reddit-last30days`（shadow） | `host_tool` | **Reddit 上游已死**（`.json` 403、`.rss` 空、OAuth 自助关闭）；模板应改指 HN / StockTwits / Polymarket 或撤回 |
| **employee-reviews** | Blind / Indeed / Glassdoor：六维评分序列、pros / cons、雇主聚合 | US | Blind：裸 HTTP `teamblind.com` 解析 Next.js flight payload；Indeed / Glassdoor：**Firecrawl** | `FIRECRAWL_API_KEY` | 1,000 credits / 月，**当前 −3** | **匿名** | 无，新建 | `host_tool` | 治理、child CLI、Firecrawl 额度决定 |
| **roic-transcript** | 财报电话会文字稿 | US+ | `httpx` + 伪装 Chrome UA，无 cookie / key / 浏览器 / 代理 | 无 | 30 天磁盘缓存（162 份） | 一手（文字稿） | `roic-transcript` 403 | `public_https` | **全站 Cloudflare 403，2026-08-29 确认**，与 Dalton 同一结论 |
| sentiment-dashboard / us-top-monitor / ah-premium / leverage monitors | RSI / regime 信号、融资与拥挤度（CN/JP/KR/TW）、MSCI、VIX 期限结构 | 全球 | yfinance、akshare、东财 `push2delay`、JPX / TAIFEX / KOFIA、Bloomberg CSV | 无 | — | 派生 | 无 | `host_tool` | 只属价格归因层 |
| **`scripts/bbg_remote_refresh.sh`** | Bloomberg BDH 导出 → `workspace/temp/bbg_data_latest.csv`（09-09 10:03 仍在更新） | 全球 | **SSH / scp 经 Tailscale 到 Windows BBG 机** | SSH key | 每日 2 次 cron | **授权 vendor** | 无 | `host_tool` | 需要许可 / 再分发裁决 |
| **tmeet-weekly-minutes** | 内部周会智能纪要 + 文字稿 | — | `tmeet` CLI（腾讯会议 API） | CLI 管理的 token | 7 天回看 | **内部 / 管理层** | 无 | `host_tool` | 文档里写的 cron 实际不存在 |
| findata-ciqticker / -netcash / -hiddenasset / -return | 代码映射、净现金、隐藏资产、股东回报 | US/HK/A | **只用 yfinance** 取市值 / 汇率，真实数字委托 findata-analyst / cn-hk-findata | 无 | — | 派生 | 扩 `sec-financials` | `host_tool` | ciqticker **不是** Capital IQ 数据 |
| screening-low-valuation | Capital IQ Comps 筛选 | ADR/HK | `openpyxl` 读**人工提供**的 `.xlsx` | 无 | — | vendor（人工） | 无 | `host_tool` | 纯人工投喂 |
| **`scripts/changedetection_client.py`** | 页面变化 diff（IR 页） | 任意 | 本地 changedetection.io `127.0.0.1:5055` | 本地 | — | 一手（若为 IR 页） | 补 `web-fetch` | `host_tool` | 适合做 filing / IR 监视 |
| **firecrawl MCP** | 被挡 / JS 页面的 markdown | 任意 | `mcp.servers.firecrawl` → `mcp.firecrawl.dev/v2/mcp` | `Authorization` header / `~/.config/firecrawl/api_key` | 1,000 / 月，**已用尽到约 09-26** | 是 transport 不是 source | `web-fetch` 的**新 transport** | `mcp_managed` | 新 profile 版本 + owner 重新批准（换 transport） |

不算数据源、已排除：`eve-hub`（Tailscale MCP 代理）、`eve-share`、`fin-hub`、`google-workspace`（`gws` 通用封装）、`alphaengine-secondcurve`（浏览器驱动的 FinGPT 生成式问答，不是检索）、各类 `*-monitor` / `screening-*` / `*-deep-dive` 报告生成器。

## B. 重点项

1. **Sales note 走 Gmail，不走 Feishu。** `market-digest` 调 `gws gmail users messages list`，发件域过滤 gs.com / marquee / jefferies / bofa / ml.com，`format=full` 取全文，然后给邮件打 label 保证只回一次。cron `market-digest-am`（工作日 09:00）与 `-pm`（21:00，Asia/Hong_Kong）各推一张 Feishu 卡片。**原文逐字保留**在 `skills/market-digest/output/digest_<date>_<AM|PM>.json`，252 份、到今天、每份约 230 KB，另有 1.3 GB `vectors.db`（含 `email_chunks` 原文层）。Dalton 的人工 / vendor 投喂 connector 应吃 `emails[]`（`id / from / subject / date / is_priority / body`），层级「卖方、具名分析师」，而不是吃 AI 摘要。Feishu 只是输出；唯一自动化的人工投递口是 **Discord `#wiki`**（cron `wiki-auto-ingest`，每日 23:00，最近一次 09-08）。
2. **employee-reviews 是 Blind / Indeed / Glassdoor**，不是脉脉。Blind 免认证可用（09-09 自测 30 条 / 1,171）；30 条之后 pros / cons 被替换为占位文本但评分与日期真实（`body_locked:true`）。Indeed / Glassdoor 要 Firecrawl，**当前失败**（credits −3）；Glassdoor 第 2 页 302 到反爬。层级：匿名，只能看趋势方向，不能作 Claim。
3. **X / Twitter 是三样东西。** `xreach`（Node CLI）可枚举时间线 / 线程 / 搜索，对应 Dalton `host-tool:xreach`；`x_search` 是 gateway 内置、SuperGrok OAuth、合成且不可枚举，对应 `host-tool:x-search`，AGENTS.md 禁止用它证明「没有」；agent-reach 自己的 `TwitterChannel` 是第三条更弱的路，AGENTS.md 说不要用。Dalton 模板里的 forbidden-route 已经正确编码了这个区分。
4. **雪球两条 lane 共用一个凭证。** 帖子 / 评论 / 搜索 / 用户时间线走 `scripts/xueqiu_posts.py` 打 `api.xueqiu.com`；行情 / 热股 / 搜索走 agent-reach `XueqiuChannel`。都要 `~/.agent-reach/config.yaml` 里的 `xueqiu_cookie`。主站在阿里云 WAF 滑块后；Firecrawl 只拿到壳。Dalton 的 `xueqiu` 模板已匹配，含 cn-hk-findata 热榜回退。
5. **cn-hk-findata。** cninfo 两个端点 POST、普通 UA、**无账号**，PDF 在 `static.cninfo.com.cn`，正是 Dalton `cninfo` 的允许主机。其余 87 个 intent 是 akshare，**不在任何 Dalton 模板里**。要带上的注意事项：`plan.fallback_used=true` 时源（同花顺 / 新浪 / 雪球）与东财口径不同，必须标注。
6. **Guidepoint。** skill 只调 `search_library`，OpenClaw 侧**没有 `get_document` / 全文稿 op**，返回的是摘录级问答加 `source_attribution.markdown`。Dalton 已批 `guidepoint-search-library-v1` 和 `guidepoint-get-transcript-v1` 两条，第二条上游今天没有对应物。全部经本地 OAuth 代理 `127.0.0.1:8943/mcp`。
7. **roic。** OpenClaw 也过不去：裸 `httpx` + 伪装 UA，无 cookie / key / 浏览器 / 代理；memory 记录 2026-08-29 起 NVDA、AAPL 全站 403，并标注 skill 自己的「付费订阅」提示有误导。162 份缓存早于封锁。文档化的回退：AlphaEngine 纪要 → IR webcast → 8-K / 6-K Ex-99 → 如实标缺口。与 Dalton 的判断互相印证。
8. **findata 家族**里真正取数的只有 `findata-analyst`（edgartools）和 `bdc-analyst`（裸 `data.sec.gov`）。`-netcash` / `-hiddenasset` / `-return` 只用 yfinance 取市值 / 汇率。`findata-ciqticker` 不碰 Capital IQ，是 Wind 代码解析 + yfinance 交易所 + 本地映射表。
9. **内幕交易 / 13F。** `company-filings-alert/scripts/edgar_enrich.py` 是唯一解析 SC 13D/13G（申报人、持股比例、修订号）与 Form 144（卖方、股数、市值）的地方；Form 4 两处都有。`13f-tracker` 用 edgartools `form="13F-HR"` 加 `compare_holdings()`。
10. **alphaengine-official 已退役**（`user-invocable:false`，`FINCHAT_*` 密钥失效）。`-secondcurve` 在用，但是对 `alphaengine.top` FinGPT 的浏览器自动化，是生成式问答面，不是文档检索，不能包装成检索。Dalton 的 `alphaengine` 模板只绑 MCP 检索库，是对的。
11. **网页 403。** gateway 没有代理、浏览器或 headless 标志。文档化的阶梯是 `web_fetch` → **Firecrawl** → `scripts/playwright_public.py`。Firecrawl 是唯一能过部分 403 / 挑战页的 transport，限公开页、禁 cookie / 认证 / 付费墙绕过，1,000 credits 购买 9 天后用尽。对 Dalton 是**换 transport**，要新 profile 版本 + 重新批准。
12. **Feishu** 是输出通道（market-digest 卡片、tmeet 纪要、filing 提醒）和 `transcript-polish` 的人工编辑回环面，不是 sales note 的入站通道。

## C. 建议接入顺序（按对分析师工作流的证据价值）

1. **market-digest（sales note，人工 / vendor 投喂）**：252 天具名卖方邮件全文已在本地且每日两次增长；首次覆盖的单位工作量收益最高。
2. **company-wiki**：135 家公司的管理层纪要、专家访谈、券商笔记含 ACN；本地读取无上游风险。
3. **Guidepoint lane**：身份与两条治理记录已批，只缺 child CLI / launcher / 接线；`get_transcript` op 上游无实现。
4. **cn-hk-findata 的 akshare op**：让 `cninfo` 从只有公告变成 A / H 完整基本面；任何中国名字进完整覆盖前必需。
5. **雪球**：shadow → connected；两条 lane 已映射到 Dalton 目标，一个 cookie 解锁两条。
6. **x-xreach，再 x-x-search**：`xreach` 先，因为它可枚举（Dalton 要 `completeness=enumerated`）；`x_search` 作 ranked 补充。
7. **company-filings-alert + 13f-tracker 作为 `sec` 新 operation**：Form 4 / 13D-G / 144 / 13F 是持续跟踪层，复用已信任的源身份。
8. **employee-reviews（只 Blind）**：便宜的匿名佐证；Indeed / Glassdoor 等 Firecrawl 额度决定。
9. **changedetection.io 作 IR 页监视**：本地、无 key，补 `web-fetch` 的一手源变化检测。
10. **Bloomberg CSV / yfinance**：价格归因层，证据价值排最后；Bloomberg 还要再分发裁决。
11. **撤回或换 transport `roic-transcript`**：两边都确认站点封锁，保留批准是唯一悬着的治理债。
12. **不建 `reddit-last30days`**：改指 HN / StockTwits / Polymarket 或撤回。

## D. 需要的凭证槽与 owner 裁决（只列名）

**凭证槽**：`gws` OAuth store · `GEMINI_API_KEY` · `GUIDEPOINT_CLIENT_ID` / `GUIDEPOINT_CLIENT_SECRET` + token 缓存 · AlphaEngine Desktop 会话（进程绑定，无槽） · `EDGAR_IDENTITY` · `xueqiu_cookie` · `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` · `x_search` 背后的 SuperGrok OAuth（gateway 持有） · `FIRECRAWL_API_KEY` · Feishu `appId` / `appSecret` · `tmeet` token · Bloomberg 主机 SSH key · `EVE_HUB_TOKEN`。

**owner 裁决**：(1) Gmail 来源的卖方邮件能否进 Dalton 账本、什么层级；(2) Bloomberg BDH 导出同问；(3) roic 两条批准撤回还是换抓取 transport 重发；(4) 是否批准 Firecrawl 作 `web-fetch` 的 `mcp_managed` 回退 transport，额度怎么定；(5) 接受 `guidepoint-get-transcript` 无上游 op，收窄还是自建；(6) `reddit-last30days` 撤回还是改指；(7) 匿名员工评价的证据层级；(8) 当 `get_document` 每会话约 20 次成为约束时 AlphaEngine 130/24h 是否调整。

主 agent 注：owner 已点名 sales note、员工调研、Twitter、雪球、cn-hk-findata 要接，故 (1) 与 (7) 视为已定方向（sales note 层级「卖方具名」、员工评价「匿名，只作趋势」）；其余攒到最后一并提出。
