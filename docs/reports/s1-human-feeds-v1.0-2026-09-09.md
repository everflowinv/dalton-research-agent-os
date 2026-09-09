# S1：人工 / vendor 投喂两条 connector（sales-notes、company-wiki）v1.0

日期：2026-09-09
状态：development candidate；未接线、未部署、未发 mission 版本；四条治理记录为 `proposed`，等 owner 批准
分支：`s1-human-feeds`（基线 main `88c040b`，另 cherry-pick Agent A 的 `99f6a9b` 取 `build_connector_inventory.py --check`）
依据：[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) 第 3 节 S 线、[OpenClaw 数据源盘点](openclaw-data-source-survey-v1.0-2026-09-09.md) A 表 market-digest / company-wiki、B.1、B.12

---

## 0. 一句话

两条 `host_tool` connector 读的是**这台机器上已经落盘的字节**：卖方邮件由 host skill 每天两次从 Gmail 取回后原文写出，
wiki 是人自己写的 markdown。Dalton 不认证 Gmail、不跑 wiki 的 tagger、不持任何凭证，
所以 `auth=none`、`network=false`，唯一需要的权限是读一个目录。

**最重要的实测结论在第 6 节**：全量 2,923 封卖方邮件里，**只有 15 封（0.5%）的主题行点名了覆盖池里的公司**
（IBM 11、ACN 3、CTSH 1、EPAM 0、DXC 0）。按主题行归属是诚实的，但它给这条 lane 的产出定了上限，
owner 需要就此拍板（第 9 节）。

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

## 5. 归属、协调器与 launcher

`mission_feed_lane.py`：

- `FEED_DISCOVERY_SOURCES`：`coverage_mission.DISCOVERY_SOURCES` 需要的两行（见第 7 节），声明在这里，
  集成时是一次合并，测试里 patch 成集成后的样子。
- `feed_discovery_parameters(terms, since, as_of)` → `{query, date_after, date_before}`。
  **特意用 mission authority 已经在验的那个 windowed 形状**，所以 `validate_mission_source_discovery`
  **不需要新分支**（`coverage_mission.py` 是 S1 的禁改文件，这条约束反过来选出了正确的设计）。
- `attribute_notes(notes, universe)`：只读**主题行**，因为发现时只有主题行——正文要 acquisition 之后才存在。
  这是真实的限制，写下来而不是糊过去。点到覆盖公司的进该公司队列；点不到的（宏观、利率、指数调整）
  进 `unattributed`，**不入任何队列**。覆盖池里出现 `document_subject.COMPANY_NAMES` 不认识的 ticker 时**在开头就报错**：
  它会「匹配自己」从而全程静默零归属，而修法只是加一行名字表。
- `attribute_wiki_documents(...)`：不做文本匹配，直接用语料自己的 `company_tags` ∩ 覆盖池 ticker。
  人已经把它归档到某公司或某行业了，这个归档比任何对正文的正则都强。行业笔记 `company_tags=[]`，保持为空。
- `wiki_spec_ref(doc_type_key)` → `wiki-expert-interview` 等；sales note 的 spec 是 `sales-note`。
  **特意不加进 `document_figure_grade.GRADE_BY_SPEC`**：这些都不是 filed statement 也不是 transcript，
  不该从里面读出任何数字；没有 grade 的 spec 只被当散文读，对专家访谈和券商笔记正是对的。
- `FeedDiscoveryCoordinator`：`authorize()` **调用**（不是重写）`missions.authorize_source_discovery`，
  即 `source_plan` 必须 `connected`、`may_write` 必须同时含 `source_discovery` 与 `observation` 的那道闸；
  `enumerate()` 走 governed child（lane 自己直接读目录会成为绕过治理的第二条路）；
  `dispatch_once()` 先 settle 后 launch（反过来会永远撞上自己上一次的单槽而报 busy），每 tick 最多
  `ACQUISITIONS_PER_TICK=8` 份。

`feed_launcher.py`：`FeedChildLauncher(LaneChildLauncher)` + `SalesNotesFeedLauncher` / `CompanyWikiFeedLauncher`。
**用继承而不是再抄一遍**：现有两个 acquisition launcher 早于 `LaneChildLauncher`，各自带着同一份四十行的拷贝。
额外提供 review 路径需要的 `read_completed_manifest(ticket_ref, document_ref)` / `locate_completed_manifest(document_ref)`：
`O_NOFOLLOW` 打开 ticket / summary / manifest 三个文件，拒绝非 owner-only、非常规、超界的文件，
再要求三者对「取的是哪份文档、哪份 manifest 描述它」完全一致，不一致是拒绝而不是告警。
ticket 目录用 `<state>/feed-acquisitions-{sales-notes,company-wiki}/`，**避开** AlphaEngine 已占的 `<state>/acquisitions/`。

## 6. Smoke（只读，真实数据，只报计数）

`list_notes`，真实 `market-digest/output`，`--limit 500`：

| since | 去重后条数 | bofa.com | mail.marquee.gs.com | jefferies.com |
| --- | --- | --- | --- | --- |
| 2026-09-01 | 142 | 86 | 49 | 7 |
| 2026-08-01 | 500（触顶） | 264 | 215 | 21 |
| 2026-03-01 | 500（触顶） | 276 | 198 | 26 |

整个 feed 扫一遍（按 7 天步长取窗口再并集，`enumerate_notes` 单次上限 500）：
**2,923 条不重复**（原始 2,952，结转去重 29），bofa.com 1,619 / mail.marquee.gs.com 1,166 / jefferies.com 138，
41 个发件人，315 条 `is_priority`。一次全目录枚举约 1.0 秒。

**主题行归属（关键数字）**：2,923 条里归属到覆盖池的只有 **15 条**——IBM 11、ACN 3、CTSH 1、EPAM 0、DXC 0；
其余 2,908 条 `unattributed`。2026-09 那 142 条窗口里**一条都没有**。

`list_documents`，`/tmp` 上的 `vectors.db` 只读副本 + `--corpus-root ~/.openclaw/workspace`：

- `--company ACN --since 2021-01-01`：**1 份**，`expert_interview` / `expert`（与盘点一致：ACN 在 wiki 里只有一份专家访谈）。
- 全语料 1,010 行按 `doc_type_key`：`research_note` 646、`expert_interview` 100、`broker_report` 78、
  `management_meeting_minutes` 76、`other` 50、`quarterly_note` 49、`buy_side_note` 9、`ndr` 2。
- 按 `evidence_tier`：`internal` 704、`expert` 100、`management_statement` 78、`sell_side` 78、`unclassified` 50。

`get_document` 对那份 ACN 专家访谈跑通全链：manifest 写出、spool 对象落地、
`verified_feed_source` 复核 15,257 字符一致，`subject_tickers` 取到语料自己的 9 个标签（含 ACN）。
报告里不含任何邮件或 wiki 正文。

## 7. 集成待办（按依赖顺序）

1. **owner 批准四条治理记录**（`proposed` → `approved`）。没有它 child 一步都不走。
2. **`coverage_mission.DISCOVERY_SOURCES` 加两行**（禁改文件，故未动；`mission_feed_lane.FEED_DISCOVERY_SOURCES` 就是它）：
   ```python
   "source:sales-notes": MappingProxyType({
       "connector_source_ref": "source:sales-notes",
       "operation": "list_notes",
       "document_ref_prefix": "sales-note:",
   }),
   "source:company-wiki": MappingProxyType({
       "connector_source_ref": "source:company-wiki",
       "operation": "list_documents",
       "document_ref_prefix": "company-wiki-doc:sha256:",
   }),
   ```
   `validate_mission_source_discovery` 的 `parameters` 分支**不用改**：两条都落在已有的
   `{query, date_after, date_before}` 分支上。
3. **mission 新版本**：`source_plan` 加 `source:sales-notes` 与 `source:company-wiki` 且 `status=connected`；
   `autonomy.may_write` 必须已含 `source_discovery` 与 `observation`（live v4 已有）。
4. **host-tool runner**（本切片最大的缺口，见第 8 节）。在它落地之前 `record_source_discovery` 无法调用，
   因为它硬性要求 Core 里有真实的 `ConnectorInvocation` + `SourceEnvelope`（且 envelope 的
   `source_record_refs` 要与 `document_refs` 逐位相等）。`FeedDiscoveryCoordinator.record_discoveries`
   因此接一个 `receipts` 提供者，没有它就带原因拒绝，而不是写一条什么都没绑的 discovery。
5. **抽取侧 dispatch（一行分支，我没有改 `document_extraction.py`）**。需要三处：
   - `SUPPORTED_SOURCE_REFS |= FEED_SOURCE_REFS`（`feed_acquisition.FEED_SOURCE_REFS`）；
   - `_document_text` 里在 AlphaEngine 分支后加：
     ```python
     if review["source_ref"] in FEED_SOURCE_REFS:
         launcher = self.writer.feed_launcher(review["source_ref"])
         manifest = (launcher.read_completed_manifest(row["ticket_ref"], review["document_ref"])
                     if row["ticket_ref"] else
                     launcher.locate_completed_manifest(review["document_ref"]))
         _, text = verified_feed_source(self.writer.store, self.writer._transcript_spool, manifest)
         return text
     ```
   - `_source_context` 里同形状的一支，`source_content_hash = manifest["declared_content_sha256"]`，`web_fields` 为空。
   
   之所以没做：它依赖 `writer.feed_launcher`，而 `writer_server.py` 是 S1 的禁改文件；
   只改 `document_extraction.py` 会留下一个引用不存在属性的分支，那不是「纯追加」。
6. **`lane_registry.LaneSpec` 登记**（Wave 0 产物，本分支上没有）。两条 lane 各一行，参数如下：

   | LaneSpec 字段 | sales-notes | company-wiki |
   | --- | --- | --- |
   | `operation` | `dispatch_sales_notes_feed` | `dispatch_company_wiki_feed` |
   | `core_only` | `False`（要起子进程） | `False` |
   | `param_fields` | `("since", "sender_domain", "limit")` | `("since", "company", "industry", "limit")` |
   | `build_coordinator` | `mission_feed_lane.FeedDiscoveryCoordinator(missions=…, launcher=…, source_ref="source:sales-notes", companies=plan_companies)` | 同左，`source_ref="source:company-wiki"` |
   | `argv_fragment` | `--sales-notes-digest-dir <dir> --sales-notes-governance-list <p> --sales-notes-governance-get <p>` | `--company-wiki-index-db <p> --company-wiki-corpus-root <dir> --company-wiki-governance-list <p> --company-wiki-governance-get <p>` |
   | `launcher_factory` | `feed_launcher.SalesNotesFeedLauncher(digest_dir=…, state_dir=…, governance_paths={…}, spool_dir=<writer transcript spool>)` | `feed_launcher.CompanyWikiFeedLauncher(index_db=…, corpus_root=…, …)` |
   | `driver_key` | `sales_notes_feed` | `company_wiki_feed` |

   `spool_dir` 必须是 writer 的 `_transcript_spool` 目录，否则 review 侧读不到 acquisition 落的字节。
7. **`deploy/macos/install.sh`**：四条治理记录的种子块（照 `sec-financial-statements` 那两条的写法）。
   本切片没有模型配置，所以 `cockpit_model.PURPOSES` 与 `raise_day_budget_cap.MODEL_CONFIG_NAMES` 不用动。
8. **cockpit**：两条 feed 的 discovered/acquired/review 计数应出现在来源面板；集成时统一做。

### 与另外三个 agent 的冲突点（都在共享文件里，都是追加块）

- `connector_inventory.py`：`PROFILE_DEFINITIONS` 尾部 + `_output_schema` 尾支 + `_validate_connector_profile_template`
  里那条 keyless host route 规则（**只有我改了它**，改成从定义派生）。
- `connector_inventory/index.json`：不要手工合并，取任一侧后重跑
  `PYTHONPATH=src .venv/bin/python scripts/build_connector_inventory.py`，读它打印的 diff 摘要。
- `connector_governance.py`：常量块、惰性回调块、registry 尾部、`build_governance_record` 尾支、`__all__`。
- `connector_quota_policy.py`：`_DAILY_QUOTAS` 尾部。
- `tests/test_connector_inventory.py`：slug 集合加 `"sales-notes", "company-wiki"`。
- `tests/test_connector_quota_policy.py`：精确列表按字母序插入四条（`company-wiki` 在 `gemini-web-search` 前，
  `sales-notes` 在 `sec` 前）。
- `contracts/connector-inventory-index.schema.json` **没有改**：`maxItems: 64`，14 条还差得远。

## 8. 没做的事（明确列出）

- **host-tool runner / Core connector authority 链没有建。** 一次 feed 枚举目前不产生
  `ConnectorInvocation` / `SourceEnvelope` / `PhysicalAttempt` / `Usage` / `Cost` / `QuotaSettlement`。
  照 `AlphaEngineCoreSearch` 的量级估计，这是 250–400 行加一个 host bridge 与 `host_tool_runner_v0.2` gate 的实现，
  超出本切片，也正是并行计划把「不接线」划出去的那部分。**后果是**：`record_source_discovery` 不能真跑，
  所以协调器的记录半边由 `receipts` 提供者接出去、缺它就拒绝；tick 的采集半边（选取 → 起子进程 → settle → 开 review）
  是真的，测试里用一个与权威语义一致的替身承载队列，并有一条 `AuthoritySeamTests` 用
  `inspect.signature(...).bind(...)` 断言替身接受的每一次调用真权威也接受，防止替身悄悄漂移。
- **没有改 `document_extraction.py`**（理由见 7.5）、`writer_server.py`、`coverage_mission.py`、
  `bounded_planner_driver.py`、`macos_launchagent.py`、`install.sh`、cockpit、`PROJECT_STATUS.md`。
- **没有跑 openclaw 的任何 skill、没有碰 Gmail、没有写 live 状态目录、没有部署、没有发 mission 版本。**
- 卖方 note 的**正文归属**没做（只有主题行）。理由与代价见第 9 节第 1 条。

## 9. 需要 owner 裁决的开放问题

1. **主题行归属只捞到 15/2,923，这条 lane 值不值得按现在的形状接。** 三个选项：
   (a) 保持现状——只要覆盖公司被点名的那 15 封，其余 2,908 封不进队列，投入产出很低；
   (b) 先按覆盖行业的关键词（"IT services"、"consulting"、"offshore"、"AI adoption"…）扩一层主题行匹配，
   命中的先 acquire 再按正文归属，代价是每份多花一次本地读（免费）和一份 review 队列位置；
   (c) 全量 acquire 后按正文归属——2,923 份 × 中位 13.8k 字符本地读是几秒的事，真正的成本在 review 队列与抽取模型预算。
   我的判断是 (b)：正文归属需要正文，而按行业词过滤是唯一能在不花模型钱的前提下把候选面从 2,923 收到几百的办法。
   但这改变了「一份文档为哪个公司排队」的语义，应该由 owner 定。
2. **Gmail 来源的卖方邮件的再分发层级。** owner 已定「要接、层级卖方具名」，本切片按 `sell_side` + `analyst_named=true` 实现。
   仍未定的是：逐字引用能不能进 deliverable？Guidepoint 有「逐字 ≤20 词」的许可规则写进 contract，
   投行邮件底部的免责声明通常更严。建议在 mission 版本里对 `source:sales-notes` 加一条引用长度上限，
   或明确「只作为 driver 观察的依据，不逐字引用」。
3. **wiki 的 `unclassified` 那 50 行（13 个原始标签）**：是补规则还是让它们停在 `unclassified`？
   补规则要人来读那 13 个标签各是什么；停在 `unclassified` 的后果是 claim 索引把它们当最弱证据。
   我倾向后者，因为这些标签基本是单例。
4. **ACN 在 wiki 里只有 1 份文档。** 如果 wiki 的价值预期是「补 ACN 的管理层与专家视角」，
   那么这条 connector 对当前覆盖池的即时产出是 1 份专家访谈；它的价值主要在**未来**（人继续往里写）
   和**行业层**（263 份 sector 文档，其中 `us-it-services` 相关的可作行业框架素材，但不归属到公司）。
5. **`source_type` 词表要不要加一个值**（例如 `human_curated`）。现在两条都是 `authenticated_library`，
   语义上勉强；加词要动 `_validate_connector_profile_template` 的冻结集合，值不值得由集成时统一判断。

## 10. 验收

全量：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .`

```
----------------------------------------------------------------------
Ran 2072 tests in 304.178s

OK (skipped=1)
```

基线 2,034 + Agent A `99f6a9b` 的 2 项 + 本切片 36 项 = 2,072，与实际一致。

新增 `tests/test_s1_human_feeds.py`，36 项，全部离线：

- 身份：两个 operation 的 schema hash 不同、source hash 共享、跨 feed 的 source hash 不同；
  packaged profile 是 keyless `host_tool` 且带 forbidden routes；四条治理记录 round-trip 且声明零 credential slot；
  仓库里那四份 `proposed` 与 builder 逐字节一致。
- sales-notes：枚举计数、`body_sha256` 等于正文的 sha256、**结转重复的那封只出现一次且 `digest_ref` 指向最早的 run**、
  两次枚举完全相同、`since` 按邮件日期而不是 run 日期过滤、发件域过滤、坏窗口与未知 id 被拒。
- company-wiki：九种 doc type → 层级映射（含「认不出就 `unclassified`」）、`text_sha256` 等于文件内容、
  语料自己的 tag 被读出、**行业文档 `company_tags=[]`**、越出语料根的 filepath 被拒。
- child：未批准 / 错 capability 的记录在开文件之前就拒且仍写 `summary.json`；`list_notes` 通过冻结合同且原文进 spool；
  `get_note` 的 manifest 经 `verified_feed_source` 复核回同一段正文；**manifest 指向别的字节时报冲突**；
  wiki child 同链路且 `subject_tickers` 来自语料标签。
- 归属：ACN / EPAM / CTSH 的 note 各入各队列，**宏观 note 进 `unattributed`**；
  wiki 用语料标签归属，行业文档与池外公司都不入队；名字表不认识的 ticker 在开头就报错；
  spec_ref 一律没有 figure grade。
- discovery wire：手工构造的完整记录通过 `validate_mission_source_discovery`（patch 过 `DISCOVERY_SOURCES`），
  证明 `{query, date_after, date_before}` 形状与 document ref 前缀都对；前缀不对的被拒。
- grant：真 `CoverageMissionAuthority` + 真 mission 三个版本——未 connected 拒、connected 但缺
  `source_discovery` 拒、齐了才过；协调器调用权威而不是自己判断。
- tick：三份待取文档，`acquisitions_per_tick=2` → 第一 tick 取 2 份并开 2 条 review，第三份仍在队列；
  第二 tick 取完、第三 tick `idle`；manifest 能按队列记的 ticket 读回；取不到的文档 settle 成
  `acquisition_failed` 且不开 review；没有 receipt 时记录 discovery 带原因拒绝。
- launcher：缺记录 / 未批准 / 非法 principal / 非法 ticket 全拒；ticket 与 manifest 不一致（要另一份文档）被拒；
  wiki launcher 的 argv 构造正确。
- seam：tick 用到的每个权威方法都能用真签名 `bind`。

fixtures 全部合成：`tests/fixtures/s1_feeds/` 两份 digest（四封虚构银行的虚构邮件，其中一封在 PM run 里结转）
加一份 `wiki_corpus.json`（五份文档，含一份 ACN 专家访谈、一份 ACN 管理层纪要、一份 EPAM 券商研报、
一份无公司标签的行业纪要、一份窗口外的季度笔记），wiki 索引与 md 在测试里于临时目录现建。
**仓库里没有任何真实邮件或 wiki 正文。**
