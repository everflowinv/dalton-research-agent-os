# S1：人工 / vendor 投喂两条 connector（sales-notes、company-wiki）v1.0

日期：2026-09-09
状态：development candidate；已按 Wave 0 的 registry 接线（writer / driver / launchagent 三处零改动）；
未部署、未发 mission 版本；四条治理记录为 `proposed`，等 owner 批准
分支：`s1-human-feeds`（基线 main `88c040b` → cherry-pick Agent A `99f6a9b` → merge Wave 0 main `888a814`）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 S 线、[OpenClaw 数据源盘点](openclaw-data-source-survey-v1.0-2026-09-09.md) A 表 market-digest / company-wiki、B.1、B.12

---

## 0. 一句话

两条 `host_tool` connector 读的是**这台机器上已经落盘的字节**：卖方邮件由 host skill 每天两次从 Gmail 取回后原文写出，
wiki 是人自己写的 markdown。Dalton 不认证 Gmail、不跑 wiki 的 tagger、不持任何凭证，
所以 `auth=none`、`network=false`，唯一需要的权限是读一个目录。

本切片还补上了这条 transport 一直缺的执行器（第 5 节）：`host_tool_runner.py` 把一次子进程读
变成真正的 `ConnectorInvocation` + `SourceEnvelope`，所以 `record_source_discovery` 现在是真的在跑，
端到端有测试。S3 的三条大众源连接器复用同一个 runner，需要做的六件事列在第 5 节末尾。

**最重要的实测结论在第 7 节**：全量 2,923 封卖方邮件里，只有 **15 封**的主题行点名了覆盖池里的公司；
**读正文之后是 231 封**（ACN 54、CTSH 76、EPAM 34、IBM 124、DXC 9），另有 605 封只提行业、
2,087 封两者都不提且带原因丢弃。正文就在本地、免费，所以这条 lane 读正文——产出差 **15 倍**。

## 1. 落盘的数据长什么样（只读核实）

| | sales-notes | company-wiki |
| --- | --- | --- |
| 位置 | `~/.openclaw/workspace/skills/market-digest/output/digest_<YYYY-MM-DD>_<AM\|PM>.json` | 索引 `~/.openclaw/workspace/wiki/vectors.db`；语料 `~/.openclaw/workspace/wiki/` |
| 规模 | 246 份 digest（另有 `digest_pretty.json`、`all_emails.txt`、`emails/` 9 份、`split/` 14 份，均非 digest run，已被文件名正则排除）；2,952 条 email，**2,923 条不重复** | `documents` 1,010 行；1,014 份 md（另 159 份 `index.md`）；258 MB |
| 窗口 | 2026-03-13 → 2026-09-09 | `date` 2021-07-22 → 2026-09-04 |
| 每条字段 | `id / from / subject / date / is_priority / body_length / body`，全 2,952 条一致 | `category_type / category_name / content_type / date / filename / filepath / created_at / tags / related`；**没有正文列**，正文只在磁盘上 |
| 发件域 | 只有三个：`bofa.com` 1,619、`mail.marquee.gs.com` 1,166、`jefferies.com` 138；41 个不同发件人 | 公司 747 份 / 135 家；行业 263 份 / 24 个 |
| 日期格式 | RFC 2822 带偏移（`-0400 (EDT)` / `+0000`），**没有一条是 ISO** | `date` 为 `YYYY-MM-DD` |
| 正文 | 纯文本、CRLF；中位 13,277 字符，最长 112,717 | md 全文含 YAML frontmatter |

两个纠正 brief 的事实：

1. **`skills/company-wiki/wiki.db` 是 0 字节的占位文件。** 真正的索引是 `~/.openclaw/workspace/wiki/vectors.db`（183 MB）。
   `filepath` 是**相对 workspace 根**的（`wiki/companies/ACN/...`），所以 `--corpus-root` 要给
   `~/.openclaw/workspace`，不是 `wiki/`。
2. **同一封邮件会出现在两份 digest 里**：2,952 条里 29 个 id 各出现 2 次，全部是相邻 run 的结转（AM→PM 或跨日）。
   这就是去重规则存在的理由，不是理论上的。

`content_type` 是自由文本，实测 **42 个不同取值**（`Flomo笔记` 561、`专家访谈` 93、`研究笔记` 85、`管理层会议纪要` 69、
`券商研报` 50、`季度研究笔记` 49……，其余 30 个是单例，且有 `Earnings Update` / `Earnings-Update`、
`Investment Memo` / `投资备忘录` / `投资Memo` / `memo` 这类未规范化的近重复）。

## 2. 两条 connector 的身份

`connector_inventory.PROFILE_DEFINITIONS` 追加两块（模板总数 12 → 14），
`scripts/build_connector_inventory.py` 重新生成 index / profiles / fixtures / proposals，**没有手写任何哈希**。

| | sales-notes | company-wiki |
| --- | --- | --- |
| `connector_ref` | `connector:sales-notes` | `connector:company-wiki` |
| `source_ref` | `source:sales-notes` | `source:company-wiki` |
| `source_type` | `authenticated_library` | `authenticated_library` |
| transport / target | `host_tool` / `host-tool:market-digest-output` | `host_tool` / `host-tool:company-wiki-corpus` |
| auth | `none` | `none` |
| gate | `host_tool_runner_v0.2` | `host_tool_runner_v0.2` |
| forbidden routes | `route:gmail-api`、`route:market-digest-ai-summary` | `route:wiki-embedding-search`、`route:wiki-gemini-tagger` |
| operations | `list_notes`、`get_note`（都 `enumerated`、`pagination=none`） | `list_documents`、`get_document`（同上） |

三个需要解释的取舍：

- **`source_type=authenticated_library`。** 冻结词表只有六个值，没有「人放进来的」。这两个源确实是「某人的凭证背后的
  一批文档」，只是凭证在 Dalton 看到字节之前就已经花掉了。为两行加一个共享 enum 的代价大于收益，所以用现有的词并写下理由。
- **`auth=none` 而不是 `host_owned`。** child 读文件，不读任何 secret；声明一个用不到的 credential slot 是假话。
  代价是碰到 `_validate_connector_profile_template` 里「只有 Reddit 那条 keyless host route 可以不带 host-owned auth」
  的硬编码。**我把它改成从 `PROFILE_DEFINITIONS` 派生**（`transport == host_tool and auth == none` 的 target 集合），
  行为对 Reddit 完全不变，同时不再是「一份真相的第二份拷贝」。这是 S1 唯一一处改动共享校验逻辑，请集成时重点看。
- **`get_note` 的完整性是 `enumerated`。** 一封邮件的正文在本地是完整的，没有分页也没有截断；
  上游 skill 自己的 30,000 字符截断只影响 AlphaEngine 那条链，不影响这里。

### 冻结 output schema（`connector_inventory._output_schema` 追加一支）

`list_notes` →
```
{schema_version, since, sender_domain|null, notes[], note_count, source_record_refs, next_cursor, provider_status}
note = {note_id, sender, sender_address, sender_domain, subject, sent_at, is_priority,
        body_sha256, body_chars, digest_ref, evidence_tier, analyst_named}
```
`get_note` → `{schema_version, note, body, source_record_refs, next_cursor, provider_status}`（`note` 复用同一闭合对象）。

`list_documents` →
```
{schema_version, since, company|null, industry|null, documents[], document_count,
 source_record_refs, next_cursor, provider_status}
document = {document_id, doc_type, doc_type_key, evidence_tier, doc_date, category_type,
            category_name, company_tags[], sector_tags[], topic_tags[], text_sha256, text_chars}
```
`get_document` → `{schema_version, document, text, source_record_refs, next_cursor, provider_status}`。

设计上的两个决定：

- **每行 header 都带正文的 sha256。** 这是「connector 说这份文档存在」和「acquisition 把这些字节写进了 spool」之间的接缝；
  没有它，一行 feed 记录就只是一个文件名加一句主张。
- **`evidence_tier` 上 wire，不在下游推。** 层级是关于**来源**的事实，不是关于文本的：卖方 note 不管写了什么都是卖方，
  管理层纪要不管是谁整理的都是管理层原话。放在两个地方推导迟早会漂移。
- schema 里不能出现 `/`（`_assert_no_sensitive_material` 会拒），所以时间戳 pattern 用 `[+][0-9]{2}:[0-9]{2}` 收尾。

`_field_schema` **没有改**：`since` / `sender_domain` / `note_id` / `company` / `industry` / `document_id` 都落在默认的
`_string()`，`limit` 落在已有的 `_integer(1)`。这是特意选的输入字段名，为的是不碰另外三个 agent 也会碰的那个函数。

### 证据层级映射（`company_wiki_core.DOC_TYPE_RULES`，有序，先匹配先赢）

| 关键词 | doc_type_key | evidence_tier |
| --- | --- | --- |
| 专家 / expert / fireside | `expert_interview` | `expert` |
| 卖方 / 券商 / 分析师 | `broker_report` | `sell_side` |
| earnings update / earnings-update | `broker_report` | `sell_side` |
| ndr | `ndr` | `management_statement` |
| 管理层 / 业绩后 / ir会议 / 路演纪要 | `management_meeting_minutes` | `management_statement` |
| 季度 | `quarterly_note` | `internal` |
| 买方 / memo / 投资备忘录 | `buy_side_note` | `internal` |
| flomo / 研究笔记 | `research_note` | `internal` |
| 其余 | `other` | `unclassified` |

规则写得**窄**：认不出来就是 `unclassified`，不往最像的那一档四舍五入。错的层级比没有层级更糟，
因为 Wave 1B 的 claim 索引把层级当 provenance 强度读。实测 42 个原始标签里有 13 个落到 `other`（占 1,010 行里的 50 行）。

sales-notes 的层级恒为 `sell_side`、`analyst_named=true`：三家投行、41 个具名发件人或具名台席。

## 3. 治理与配额

四条 `proposed` 记录（一个 schema hash 只绑一个 operation，所以批准 index 不等于批准读文档）：

```
deploy/connector-governance/sales-notes-list-notes-v1.json
deploy/connector-governance/sales-notes-get-note-v1.json
deploy/connector-governance/company-wiki-list-documents-v1.json
deploy/connector-governance/company-wiki-get-document-v1.json
```

`connector_governance.GOVERNANCE_KIND_REGISTRY` 追加四个 kind（惰性回调，避免模块环）、
`build_governance_record` 追加两支 dispatch。permissions 都是：

```json
{"risk_class":"low","network":false,"filesystem_read":["host-feed:market-digest-output"],
 "filesystem_write":["runner:raw-sink"],"credential_slot_refs":[],"core_db":false,
 "side_effects":["read:host-feed-directory"]}
```

`filesystem_read` 用逻辑名而不是真实路径：治理记录是会被拷来拷去的 owner 产物，
里面放一条真实路径是白白泄露这台机器的形状，而 child 的路径本来就由 argv 给。

配额（`connector_quota_policy`）：`list_notes` / `list_documents` 各 500 search/日，
`get_note` / `get_document` 各 1,000 document/日，`max_physical_calls_per_unit=1`。
本地文件读没有上游要礼貌，也没有钱要花，所以给得宽；**仍然声明**，因为 admission 对没有治理配额的路由是拒绝而不是放行，
而且没有配额的 lane 是「跑飞了也没人发现」的 lane。1,000 正好等于「把整个语料/整个归档读一遍」。

## 4. 两个 child CLI：approval first、artifact always、contract last

`sales_notes_cli.py` / `company_wiki_cli.py`，顺序照 `sec_financials_cli.py`：

1. **先批准**。载入治理记录 → 必须 `approved` → `capability_id` 必须等于**这一个 operation** 的 → 
   `expected_source_hash` / `expected_schema_hash` 必须仍然描述打包好的 contract。任一条不满足，一个文件都不打开。
   （测试里：拿 `list_notes` 的批准去跑 `get_note`，报 `governance record covers a different capability`。）
2. **再落 artifact**。读到的东西 canonical 化、sha256、写进 RawSpool，**然后**才从里面取东西。
   `get_*` 落两个对象：raw 记录（文档是怎么到的）和正文字节（文档本身，即 `assembled_object`）。
3. **最后校合同**。`authority_resolver._schema_matches(wire, 冻结的 output schema)`；描述不了的 observation 直接拒，
   不是先存下来再解释。
4. **`summary.json` 每条路径都写**，exit code 从 `status` 派生。

`[project.scripts]` 加了 `dalton-sales-notes`、`dalton-company-wiki`（这是我唯一改 `pyproject.toml` 的地方）。

### acquisition manifest 与 `verified_feed_source`（新模块 `feed_acquisition.py`）

web lane 绑 URL + body hash，AlphaEngine 绑 provider 声明的文档哈希 + 分页。本地 feed 两个都绑不了。
它**能**绑、也确实绑的是：哪个 feed、哪个 operation、哪份文档、哪条治理记录授权的、以及 spool 里那段正文的 sha256。

```
{schema_version, id, created_at, source_ref, operation, document_ref, target_ref,
 governance_ref, governance_hash, status:"complete", doc_kind, evidence_tier, doc_date,
 origin_ref, subject_tickers[], content_chars, declared_content_sha256,
 assembled_object:{content_hash,size_bytes,storage_locator},
 connector_invocation_ref:null, connector_invocation_hash:null, content_hash}
```

`id` 由 `(document_ref, 正文 sha256)` 派生：同一份没变的文档重取得到同一个 manifest，变了的文档冒充不了同一次 acquisition。
connector authority 两个字段**成对可空、现在是 null**：等 host-tool runner 落地后它们会像 fetch 一样带真 invocation，
`verified_feed_source` 已经写好了「非空就必须回 Core 复核」的分支。显式为空好过悄悄没有。

`verified_feed_source(core, spool, manifest, receipt_reader=None) -> (manifest, text)`：
校验闭合 manifest → 从 spool 按 `assembled_object.content_hash` 取字节 → 复核字节数 → **重算 sha256** →
UTF-8 解码 → 复核正文 sha256 与字符数。签名与 `verified_source` / `verified_public_web_source` 一致，
所以抽取侧的 dispatch 是一个分支而不是一个形状不同的特例。

## 5. host_tool runner：这条 transport 第一次有了执行器

公开 HTTPS 有执行器，loopback MCP 有执行器，`host_tool`——「在这台机器上是一个程序，而不是网络上的一个服务」
那一类——没有。库存里每一个 `host_tool` 模板（两条 feed，以及排在后面的 xueqiu 与 x-xreach shadow）
因此都是「有冻结合同、没有办法满足它」：子进程能把数据读出来，但没有东西把这次读变成
`ConnectorInvocation` 和 `SourceEnvelope`，而没有这两样，mission 的 discovery 就没有东西可绑。

新增 `src/dalton_core/host_tool_runner.py`。**先说它不是什么**：它不是 `ConnectorTransportExecutor` 的第二份拷贝。
那个执行器对付费网络调用是对的——Scheduler lease、五道崩溃屏障的 durable journal、孤儿预留恢复——
因为一次可能已经扣过费的调用绝不能被静默重放。host_tool 是另一种风险形状：**本地文件读、免费、幂等、
没有会被重复计费的上游、也没有会和我们漂移的供应商计量表**。所以这个 runner 直接经 `ConnectorStore`
写同一条 authority 链，不假装持有一份它用不上的 lease。**放弃了什么写在模块顶上而不是留给以后发现**：
没有 lease 强制、没有 journal 重放、没有孤儿恢复；预留与 attempt 之间崩溃会留下一条未结算预留，
这正是现成的 `unsettled_reservations` 扫描要报的东西。

它写的链（每一步都是既有 `ConnectorStore` / `ObservabilityStore` 的方法，没有新表）：

```
approval → register_profile(auth_mode=host_tool) → register_price_rate(0) → register_rate_policy(治理配额)
        → register_call_spec → register_invocation(ExecutionInvocation) → reserve_quota
        → 子进程 → RawSpool(stdout) → record_physical_attempt → 解析 + 冻结 schema 校验
        → record_usage → record_cost_for_runner → settle_quota_for_runner
        → register_artifact_version_v2 → record_source_envelope
```

四个设计点：

1. **`auth_mode` 增加 `host_tool`。** `ConnectorStore.register_profile` 对 `auth_mode != "mcp_managed"`
   强制要求非空的**公网** `allowed_hosts` 和一份 https `network_policy`。一个本地子进程两样都没有，
   而为了通过检查去声明一个虚构的公网主机，会在 owner 用来判断「这东西能碰到什么」的唯一一份记录里写下假话。
   `auth_mode` 这个字段本来就已经在承担这个职责——`mcp_managed` 就是靠它说「我不是 HTTP 传输」——
   所以 `host_tool` 是同一处的同一种声明。改动是三处 `mcp_managed` 特判改为 `hostless = auth_mode in {mcp_managed, host_tool}`，
   外加「host_tool 的 credential slot 可有可无」（读目录不需要，S3 的 xueqiu 需要一个）。
2. **凭证只能来自 slot。** 子进程的环境是 runner **构造**的，不是继承的：只有解释器自己的执行变量
   （`PATH` / `HOME` / `PYTHONPATH` 等，白名单）加上 profile 声明的 credential slot，由调用方解析。
   子进程永远看不到 `os.environ`。这才让「这条 connector 不持有任何凭证」变成可核对的陈述而不是愿望。
3. **stdout 就是 raw response。** 子进程打印闭合 observation wire，别的什么都不打印；这些**确切的字节**
   先哈希进 spool，然后才被解析，再然后才对着冻结 output schema 校验。合同描述不了的 wire 在字节安全之后才失败，
   并且仍然如实结算（`indeterminate`），而不是凭空消失。两个 CLI 因此新增 `--emit-wire`。
4. **计量按合同说回来了什么算。** document 单位的配额预留 1 份文档、search 单位预留 profile 的记录上限；
   把上限预留在 document 配额上会让一次读花掉一天的额度（第一次跑就撞上了）。

**S3 要复用它需要做的**（runner 里没有任何 feed 专属逻辑）：

1. 在 `PROFILE_DEFINITIONS` 里给自己的 slug 声明 `host_tool` 模板与冻结 output schema（xueqiu / x-xreach 已有）；
2. 写一个 `<slug>_core.py` 暴露 `<slug>_identity(operation)`（与 `sales_notes_core` 同形），
   以及每个 operation 一条治理记录；
3. 写一个 child CLI，`--emit-wire` 时把闭合 wire 打到 stdout；
4. 在 `connector_quota_policy` 里为每个 (slug, operation) 声明配额——**没有配额的路由 admission 直接拒**；
5. 提供一个 `command(parameters, output_dir) -> argv` 的构造器（feed 用 `FeedChildLauncher.child_command`），
   以及需要凭证时的 `credential_slot_refs` + `credential_resolver`；
6. `HostToolRunner(store=…, connectors=…, observability=…, spool=…, template_key=…, identity=…,
   governance=…, command=…, connector_slug=…)`，然后 `run(parameters=…, work_ref=…, output_dir=…)`。
   返回的 `HostToolReceipt` 里全是 ref 与 hash，可以直接用 `ConnectorCompletionReceiptReader` 复核。

## 6. 归属：主题行不够，正文是本地的而且免费

**主 agent 的裁决**（按分析师工作流，不需要 owner 拍板）：分析师会读任何提到这家公司**或**这个行业的 note，
而正文就在本地、免费。落地时实测把这条规则又推进了一步——**主题行连行业都不提**：

| 规则 | 全量 2,923 封归属到覆盖池 |
| --- | --- |
| 只看主题行 + 发件人的公司名 | **15** |
| 主题行 + 发件人的行业词（7 个行业词 + 4 个同业） | 再加 **14** 个候选，其中 2 个正文点名了覆盖公司 |
| **读正文**（本切片实现的） | **231** |

所以两段式是这样定的，而不是计划里写的那样：

- **第一段（表头，免费）**只用来**排序和免读**：主题行或发件人点名了覆盖公司 → 直接归属该公司（15 封，不用读也知道）；
  点名了行业词 → 排到队列前面；都没有 → 排在后面。**这一段不丢弃任何东西**，因为主题行什么都没说
  不等于正文什么都没说。
- **第二段（读正文，每 tick 上限 50，由 plan 冻结）**：读一份文档，用同一套 `document_subject` 规则判定：
  点名覆盖公司 → 归属（company）；只点名行业词 → 记为行业级（industry），**不入任何公司队列**；
  两者都没有 → 丢弃并**带原因**（`names no coverage company and no industry term`）。

**关键的形状决定：对本地 feed，acquisition 就是 discovery。** 一封卖方邮件的主题行不说它讲的是哪家公司，
唯一的办法是读正文，而读正文本身就是 acquisition。所以 `FEED_DISCOVERY_SOURCES` 的 operation 是
**`get_note` / `get_document`**（文档读），不是 index 读：一条 discovery 记录恰好指名它读过的那一份文档，
它绑的 envelope 的 `source_record_refs` 也恰好是那一份。若用 listing 形状的 envelope，就等于宣称
「这次枚举到的每一封邮件都属于它被记录到的每一家公司」。

wiki 不做正文归属：**人已经把文档归档到某个 ticker 或某个 sector 了**，这份归档比任何对散文的正则都强——
一份提到 Accenture 两次的行业专家访谈仍然是行业文档。正文只用来判定「行业级还是丢弃」。

**跨 tick 去重**：mission 已经持有的文档不再读第二遍（第二个 tick 对同一窗口只花「每条新 note 一次读」）。
读过但没入库的（行业级与丢弃）**会被重读**，因为账本里没有一行叫「看过，什么都没留下」；
每 tick 上限就是这件事的护栏。补一行这样的账是集成待办（第 8 节第 6 条）。

### 冻结的 feed discovery plan

`deploy/phase9/p9-us-it-services-feeds-v1.json`，闭合、内容哈希自绑，校验器
`mission_feed_lane.validate_feed_discovery_plan`（自己的 plan，不是扩 `mission_source_discovery` 的那个：
搜索计划冻结的是 query 模板与 lookback，因为一次搜索要花钱；feed 根本没有 query——整个窗口都在盘上——
它需要冻结的是相反的东西：**哪些词让一份文档值得一读，以及每 tick 读几份**）。

```
industry_keywords: GenAI services / IT services / consulting / offshore / outsourcing /
                   system integrator / systems integrator
peer_names:        Capgemini / Infosys / TCS / Wipro
lookback_days: 400    body_reads_per_tick: 50    companies: 五家（company_ref → search_terms）
```

覆盖公司的名字**不在 plan 里**：它们由 `document_subject.COMPANY_NAMES` 匹配，那里本来就是
「Accenture 都叫什么」的所在，放第二份必然过期。

### 协调器与 launcher

`mission_feed_lane.FeedDiscoveryCoordinator`：`authorize()` **调用**（不重写）
`missions.authorize_source_discovery`（`source_plan` 必须 `connected`，`may_write` 必须同时含
`source_discovery` 与 `observation`）；`enumerate_via_runner()` 走 runner，**index 读也是一次被记录的调用**；
`triage()` → `resolve_documents()`（有界批次）→ 每份 company 文档 `record_source_discovery` →
`mark_discovered_document_launched` → `settle_discovered_document(acquired)` → `register_document_review`。
没有 runner 时，tick 仍做队列那一半并如实说自己没装全，而不是让一条 lane 弄挂整个 controller tick。

`feed_launcher.py`：`FeedChildLauncher(LaneChildLauncher)`——**继承而不是再抄一遍**那四十行
（现有两个 acquisition launcher 早于 `LaneChildLauncher`，各自带着自己的拷贝）。新增
`prepare_run` / `settle_run`：runner 拥有进程与 authority 链，launcher 拥有 ticket，
因为 ticket 目录才是 review 侧用来找「哪次运行产出了这份文档的字节」的东西。这样**一份文档只读一次**：
同一个子进程的 stdout 被 runner 记成 raw response，它写出的 `manifest.json` 落在 launcher 的 ticket 目录里。
`read_completed_manifest` 用 `O_NOFOLLOW` 打开 ticket / summary / manifest 三个文件，
拒绝非 owner-only、非常规、超界的文件，再要求三者完全一致。

### lane 注册（Wave 0 之后，禁令解除）

`mission_feed_lane.py` 末尾两条 `LaneSpec`，`lane_registry.LANE_MODULES` 加一行
`"dalton_core.mission_feed_lane"`：

| | sales-notes | company-wiki |
| --- | --- | --- |
| `operation` | `dispatch_sales_notes_feed` | `dispatch_company_wiki_feed` |
| `order` / `driver_key` | 120 / `sales_notes_feed` | 130 / `company_wiki_feed` |
| `init_kwarg` | `sales_notes_feed_launcher` | `company_wiki_feed_launcher` |
| argparse | `--sales-notes-digest-dir`、`--sales-notes-governance-{list,get}`、`--feed-discovery-plan` | `--company-wiki-{index-db,corpus-root}`、`--company-wiki-governance-{list,get}` |
| `argv_fragment` | plan、两条治理记录、digest 目录都在 `<state>` 下才开 | 同左，corpus 与索引都在才开 |

`writer_server.py` / `bounded_planner_driver.py` / `macos_launchagent.py` **一行都没改**——这正是 Wave 0 要买的东西。
`tests/test_lane_registry.py` 的四处「迁移前后一致」快照按 Wave 0 的写法补上了这两条（保持精确集合，
而不是给新 lane 开豁免）。

## 7. Smoke（只读，真实数据，只报计数）

### sales-notes

`list_notes` 走真实 `market-digest/output`（一次全目录枚举约 1.0 秒）：

| since | 去重后条数 | bofa.com | mail.marquee.gs.com | jefferies.com |
| --- | --- | --- | --- | --- |
| 2026-09-01 | 142 | 86 | 49 | 7 |
| 2026-08-01 | 500（触顶 `--limit`） | 264 | 215 | 21 |
| 2026-03-01 | 500（触顶） | 276 | 198 | 26 |

整个 feed（按 7 天步长取窗口再并集）：**2,923 条不重复**（原始 2,952，结转去重 29），
bofa.com 1,619 / mail.marquee.gs.com 1,166 / jefferies.com 138，41 个发件人，315 条 `is_priority`。

**两段式归属的产出**（同一套代码，全量 2,923 与 2026-09 窗口）：

| | 全量 2,923 | 2026-09（142） |
| --- | --- | --- |
| 表头就点名覆盖公司 | 15 | 0 |
| **读正文后归属到公司** | **231**（ACN 54、CTSH 76、EPAM 34、IBM 124、DXC 9） | **5**（ACN 2、CTSH 3、EPAM 2、IBM 4、DXC 0） |
| 行业级（正文只提行业词） | 605 | 42 |
| 丢弃（都不提，带原因） | 2,087 | 95 |

读正文把产出从 15 提到 231，**15 倍**。代价是每份文档一次本地读；按 plan 的 50 份/tick，
把六个月归档跑完约 59 个 tick，全部免费、无网络。

### company-wiki

`/tmp` 上的 `vectors.db` 只读副本 + `--corpus-root ~/.openclaw/workspace`：

- `--company ACN --since 2021-01-01`：**1 份**，`expert_interview` / `expert`（与盘点一致）。
- 同一套两段式规则跑全语料 1,010 行：**公司标签命中 5 份**（IBM 3、ACN 1、CTSH 1，EPAM / DXC 各 0）、
  **行业级 33 份**、丢弃 972 份。
- 全语料按 `doc_type_key`：`research_note` 646、`expert_interview` 100、`broker_report` 78、
  `management_meeting_minutes` 76、`other` 50、`quarterly_note` 49、`buy_side_note` 9、`ndr` 2。
- 按 `evidence_tier`：`internal` 704、`expert` 100、`management_statement` 78、`sell_side` 78、`unclassified` 50。

`get_document` 对那份 ACN 专家访谈跑通全链：manifest 写出、spool 对象落地、
`verified_feed_source` 复核 15,257 字符一致，`subject_tickers` 取到语料自己的 9 个标签（含 ACN）。

### host_tool runner

对合成 fixture 跑通 `list_notes` 与 `get_note` 全链，`ConnectorCompletionReceiptReader`
复核 invocation 与 envelope 的 `content_hash` 均一致，envelope 的 `operation=get_note`、
`source_record_refs` 恰为那一份文档。报告里不含任何邮件或 wiki 正文。

## 8. 集成待办（按依赖顺序）

1. **owner 批准四条治理记录**（`proposed` → `approved`）。没有它 child 一步都不走。
2. **`coverage_mission.DISCOVERY_SOURCES` 加两行**（禁改文件，故未动；
   `mission_feed_lane.FEED_DISCOVERY_SOURCES` 就是要合并的那两行）：
   ```python
   "source:sales-notes": MappingProxyType({
       "connector_source_ref": "source:sales-notes",
       "operation": "get_note",
       "document_ref_prefix": "sales-note:",
   }),
   "source:company-wiki": MappingProxyType({
       "connector_source_ref": "source:company-wiki",
       "operation": "get_document",
       "document_ref_prefix": "company-wiki-doc:sha256:",
   }),
   ```
   **这是唯一还没做的必需改动**，因为 `coverage_mission.py` 是 S1 的禁改文件。
   `validate_mission_source_discovery` 的 `parameters` 分支**不用改**：discovery 记录的 parameters
   是「这条 lane 为这家公司看的窗口」，落在已有的 `{query, date_after, date_before}` 分支上。
   测试用 `mock.patch.object` 把这两行装上，跑的是真 `CoverageMissionAuthority`。
3. **mission 新版本**：`source_plan` 加 `source:sales-notes` 与 `source:company-wiki` 且 `status=connected`；
   `autonomy.may_write` 必须已含 `source_discovery` 与 `observation`（live v4 已有）。
4. **plan 与源目录进 `<state>`**：`<state>/feed-plans/p9-us-it-services-feeds-v1.json`、
   `<state>/feeds/market-digest-output`（指向或同步自 OpenClaw workspace 的 digest 输出目录）、
   `<state>/feeds/company-wiki/{wiki-index.sqlite,…}`。`argv_fragment` 以这些文件是否存在来决定 lane 开不开，
   所以没有 OpenClaw workspace 的 Core 自然没有 feed lane。
5. **抽取侧 dispatch（我没有改 `document_extraction.py`）**。三处：
   - `SUPPORTED_SOURCE_REFS |= FEED_SOURCE_REFS`（`feed_acquisition.FEED_SOURCE_REFS`）；
   - `_document_text` 里在 AlphaEngine 分支后加：
     ```python
     if review["source_ref"] in FEED_SOURCE_REFS:
         launcher = self.writer.lane_launcher(FEED_LAUNCHER_KWARGS[review["source_ref"]])
         manifest = (launcher.read_completed_manifest(row["ticket_ref"], review["document_ref"])
                     if row["ticket_ref"] else
                     launcher.locate_completed_manifest(review["document_ref"]))
         _, text = verified_feed_source(self.writer.store, self.writer._transcript_spool, manifest)
         return text
     ```
   - `_source_context` 里同形状的一支，`source_content_hash = manifest["declared_content_sha256"]`，
     `web_fields` 为空。
   
   之所以仍然没做：`writer_server.py` 是禁改文件，而这段要经 `lane_launcher(...)` 拿到 launcher；
   只改 `document_extraction.py` 会留下一个引用不存在 kwarg 的分支，那不是「纯追加」。
   Wave 0 之后这一步已经很小了——launcher 就在 `lane_launcher(init_kwarg)` 里。
6. **一行「看过，什么都没留下」的账**。行业级与丢弃的文档在账本里没有行，所以每个 tick 会重读它们
   （每 tick 上限 50，免费，但是浪费）。最小的做法是给 feed 加一个 owner-only 的
   `<state>/feed-read-ledger/<source>.json`（document_ref → 上次读的 content hash 与 outcome）；
   更正的做法是给 mission 一个 `industry_ref` 维度的 discovery，那要动 `coverage_mission.py`。
7. **`deploy/macos/install.sh`**：四条治理记录的种子块 + plan 文件（照 `sec-financial-statements` 的写法）。
   本切片没有模型配置，所以 `cockpit_model.PURPOSES` 与 `raise_day_budget_cap.MODEL_CONFIG_NAMES` 不用动。
8. **cockpit**：两条 feed 的 read / company / industry / dropped 计数应出现在来源面板；集成时统一做。

### 与另外三个 agent 的冲突点

- `connector_inventory.py`：`PROFILE_DEFINITIONS` 尾部 + `_output_schema` 尾支 +
  `_validate_connector_profile_template` 里那条 keyless host route 规则（**只有我改了它**，改成从定义派生）。
- `connector_inventory/index.json`：不要手工合并，取任一侧后重跑
  `PYTHONPATH=src .venv/bin/python scripts/build_connector_inventory.py`，读它打印的 diff 摘要。
- `connector_governance.py`：常量块、惰性回调块、registry 尾部、`build_governance_record` 尾支、`__all__`。
- `connector_quota_policy.py`：`_DAILY_QUOTAS` 尾部。
- **`connector.py`：`register_profile` 的 `auth_mode` 增加 `host_tool`**（三处 `mcp_managed` 特判改为
  `hostless`，加上 host_tool 的 credential slot 可选）。这是本切片对共享 authority 校验的唯一改动，
  也是 S3 需要的同一处；集成时请重点看，并注意它**没有**放宽 public transport 的任何一条。
- `lane_registry.LANE_MODULES`：一行 `"dalton_core.mission_feed_lane"`。
- `tests/test_connector_inventory.py`（slug 集合）、`tests/test_connector_quota_policy.py`（精确列表按字母序插四条）、
  `tests/test_lane_registry.py`（四处迁移快照补两条 lane）。
- `contracts/connector-inventory-index.schema.json` **没有改**：`maxItems: 64`，14 条还差得远。

## 9. 没做的事（明确列出）

- **没有改 `document_extraction.py`**（理由见 8.5）、`writer_server.py`、`coverage_mission.py`、
  `bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、cockpit、`PROJECT_STATUS.md`。
  两条 lane 是经 `lane_registry` 注册的，所以 writer / driver / launchagent 三处**一行都不用改**。
- **host_tool runner 没有走 Scheduler / CapabilityCatalog / RunnerJournal。** 它直接写 authority 链
  （这条路 `tests/test_connector.py` 已经证明是合法的），因此没有 lease 强制、没有 journal 重放、
  没有孤儿恢复。对本地免费幂等读这是划算的取舍，写在模块顶上；要把它升级成完整执行器，
  路径是给 `ConnectorRunnerAdmissionGate` / `validate_connector_adapter_request` 加一条
  host_tool 分支（它们同样硬编码了 https network policy），然后照 `public_web_core_fetch` 的骨架接上。
- **行业级文档没有账本行**：`record_source_discovery` 要求一个在 universe 里的 `company_ref`，
  所以「关于行业、不关于任何公司」的 605 封只出现在 tick 摘要里。见 8.6。
- **没有跑 openclaw 的任何 skill、没有碰 Gmail、没有写 live 状态目录、没有部署、没有发 mission 版本。**

## 10. 需要 owner 裁决的开放问题

1. **Gmail 来源的卖方邮件的再分发层级。** owner 已定「要接、层级卖方具名」，本切片按
   `sell_side` + `analyst_named=true` 实现。仍未定的是：**逐字引用能不能进 deliverable**？
   Guidepoint 有「逐字 ≤20 词」的许可规则写进 contract，投行邮件底部的免责声明通常更严。
   建议在 mission 版本里对 `source:sales-notes` 加一条引用长度上限，或明确「只作为 driver 观察的依据，
   不逐字引用」。**这条不定，231 封已归属的 note 就不该进抽取。**
2. **行业级的 605 封怎么办**（见 8.6）。它们是真的行业素材（IT services 需求、同业评论），
   但账本里没有位置放。要么加一行 read-ledger 只做去重，要么给 mission 加 `industry_ref` 维度的 discovery，
   后者能让行业框架这一交付物有一手素材。
3. **wiki 的 `unclassified` 那 50 行（13 个原始标签）**：补规则还是停在 `unclassified`？
   补规则要人来读那 13 个标签各是什么；停在 `unclassified` 的后果是 claim 索引把它们当最弱证据。
   我倾向后者，因为这些标签基本是单例。
4. **ACN 在 wiki 里只有 1 份文档**（全语料公司标签命中覆盖池的只有 5 份）。
   这条 connector 对当前覆盖池的即时产出很小；它的价值主要在**未来**（人继续往里写）
   和**行业层**（263 份 sector 文档）。要不要现在就接，还是等 wiki 里 US IT services 的密度上来。
5. **`source_type` 词表要不要加一个值**（例如 `human_curated`）。现在两条都是 `authenticated_library`，
   语义上勉强；加词要动 `_validate_connector_profile_template` 与 `connector._SOURCE_TYPES` 两处冻结集合。

## 11. 验收

全量：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`

```
----------------------------------------------------------------------
Ran 2124 tests in 306.441s

OK (skipped=1)
```

Wave 0 合并后的基线 2,080 + 本切片 44 项 = 2,124，与实际一致。

新增 `tests/test_s1_human_feeds.py`，44 项，全部离线：

- **身份**：两个 operation 的 schema hash 不同、source hash 共享、跨 feed 的 source hash 不同；
  packaged profile 是 keyless `host_tool` 且带 forbidden routes；四条治理记录 round-trip 且声明零 credential slot；
  仓库里那四份 `proposed` 与 builder 逐字节一致。
- **sales-notes**：枚举计数、`body_sha256` 等于正文的 sha256、**结转重复的那封只出现一次且 `digest_ref`
  指向最早的 run**、两次枚举完全相同、`since` 按邮件日期而不是 run 日期过滤、发件域过滤、
  坏窗口与未知 id 被拒。
- **company-wiki**：九种 doc type → 层级映射（含「认不出就 `unclassified`」）、`text_sha256` 等于文件内容、
  语料自己的 tag 被读出、**行业文档 `company_tags=[]`**、越出语料根的 filepath 被拒。
- **child**：未批准 / 错 capability 的记录在开文件之前就拒且仍写 `summary.json`；
  `list_notes` 通过冻结合同且原文进 spool；`get_note` 的 manifest 经 `verified_feed_source` 复核回同一段正文；
  **manifest 指向别的字节时报冲突**；wiki child 同链路且 `subject_tickers` 来自语料标签。
- **plan**：仓库里那份 plan 加载且自绑哈希；覆盖公司的名字不在 plan 的词表里；
  被篡改的、未排序的、多一个字段的 plan 都被拒。
- **两段式归属**：表头命中直接归属且**表头不丢弃任何东西**（队列覆盖全部 note，命中的排在前）；
  正文判定 company / industry（宏观 note 命中 `IT services`）/ dropped（带原因）；
  表头命中在正文不重复名字时仍然成立。
- **wiki 归属**：用语料标签，行业文档与池外公司都不入队；名字表不认识的 ticker 在开头就报错；
  spec_ref 一律没有 figure grade。
- **discovery wire**：手工构造的完整记录通过 `validate_mission_source_discovery`（patch 过
  `DISCOVERY_SOURCES`），证明 `{query, date_after, date_before}` 形状与 document ref 前缀都对；
  前缀不对的被拒。
- **grant**：真 `CoverageMissionAuthority` + 真 mission 三个版本——未 connected 拒、connected 但缺
  `source_discovery` 拒、齐了才过；协调器调用权威而不是自己判断。
- **端到端（真权威 + 真 runner + 真子进程）**：一个 tick 读 6 份、4 份归属公司、1 份行业级、1 份丢弃；
  4 份进队列并 `acquired`、开 4 条 review；**每条 discovery 绑的 envelope 真的在 Core 里、
  `content_hash` 对得上、`operation=get_note`、`source_record_refs` 恰为那一份文档**；
  每份的 manifest 都能经 `verified_feed_source` 复核出正文。
  第二个 tick **不产生新 discovery**：4 份已持有的不再读，只重读那 2 份什么都没留下的。
  `body_reads_per_tick=2` 时这一批就是 2 份。读不到的文档 settle 成失败且**不入队列**。
- **旧队列路径**（没有 runner 时）：三份待取文档、`acquisitions_per_tick=2` → 第一 tick 取 2 份开 2 条 review，
  第三份仍在队列；第二 tick 取完、第三 tick `idle`；没有 runner 时 `resolve_documents` /
  `enumerate_via_runner` 带原因拒绝。
- **launcher**：缺记录 / 未批准 / 非法 principal / 非法 ticket 全拒；ticket 与 manifest 不一致
  （要另一份文档）被拒；wiki launcher 的 argv 构造正确。
- **seam**：tick 用到的每个权威方法都能用真签名 `bind`。

fixtures 全部合成：`tests/fixtures/s1_feeds/` 两份 digest（六封虚构银行的虚构邮件——一封在 PM run 里结转、
一封只提行业、一封两者都不提、一封在窗口外）加一份 `wiki_corpus.json`（五份文档，含一份 ACN 专家访谈、
一份 ACN 管理层纪要、一份 EPAM 券商研报、一份无公司标签的行业纪要、一份窗口外的季度笔记），
wiki 索引与 md 在测试里于临时目录现建。**仓库里没有任何真实邮件或 wiki 正文。**
