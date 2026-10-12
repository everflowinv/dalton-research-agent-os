# 外国发行人附件与正式财务输入：1b 最小交付设计

这是下一阶段的开发设计。当前系统已能识别外国发行人的申报类型；下一步要从申报主文件找到同次申报的财务报表和管理层讨论附件，让分析师及研究任务能直接阅读这些原件。正式建模还需要进一步核对报表口径、单位、期间和计算关系。

本报告依据已有本地资料与代码检查，功能尚未上线。

## 已证明的边界

NTR 的已保存 SEC submissions 原件（52,666 bytes，SHA256 a3eda661a136d334ee071fa18097873c40448f6ca62afd4d88e7e88e647def3d）含 40-F 8 条、6-K 221 条及 6-K/A 2 条，不含 10-K/10-Q/8-K。已强核 annual descriptor 为 2026-02-27 的 40-F，accession 0001193125-26-081326，主文件 d56746d40f.htm。这只证明真实申报家族和文件描述，不证明附件、三表、会计准则或季度齐备。

本轮 installed Python -I 只读检查该 profile 原 mission：按索引取最多 8 个 source records、每条最多 2 个 exact mission/document key，共 0.035 秒。该 scope 仍只有旧 10-K/8-K 空查询，没有外国附件原件可用于内容验证。该观察限定在已证明的原 mission；不冒称所有历史/其它 mission 均无附件。company-only 路径 EXPLAIN 为全表 SCAN，未执行；已改为 mission+company indexed SEARCH。

## 现有架构断点

1. `sec_issuer_reporting_profile.py` / `sec_plan_specs.py` 已提供强 submissions profile 和真实 20-F/40-F/6-K 查询身份。它们不识别财报内容。
2. `acquired_sec_filings.py:_read_candidates` 可读 foreign 主文件，但明确拒绝与 primary_document 不同的文件；同 accession 并不等于已获附件授权。
3. `public_web_fetch_cli.py:fetch_press_release_exhibit` 与 `earnings_release_intake.py:exhibit_name_from_filing_index` 面向 8-K 收益公告，优先单个 EX-99.1。40-F/6-K 常有多个不同文件；不能直接把所有 EX-99.1 当收益公告或完整财表。
4. `sec_annual_report_exhibit.py:verified_exhibit_source` 是 10-K/Exhibit 13、incorporated-by-reference 的窄合同，强原件、issuer/accession、链接和 receipt 校验可复用思路；不能伪造 10-K 身份套用。
5. 正式三表的 launcher.start、CLI --form、CoverageMission dispatch/ingest 和 ledger form CHECK 仍是国内表单，v5 output 最多包含 10-K/10-Q 及修订。`parse_live` 只接受 filing.xbrl() 的 statement/facts；无 XBRL 的 HTML/PDF 不会自动产生三表。
6. annual calendar 只认 10-K；model inputs 有明确 us-gaap 角色。外国表单家族不能证明 IFRS，更不能通过重命名概念变成 us-gaap。此阶段不能消除正式模型所有后续门。

## 1b 交付范围：附件原件，保持 native form

新增版本化、只读的 `sec_foreign_filing_documents.py`，从强已取得 primary 和其同 accession filing-index 原件枚举文档成员。记录公司/CIK、accession、native form、reporting-profile ref/hash、primary discovery/query/envelope/manifest/body 哈希、index manifest/body 哈希、索引精确行的 filename/type/sequence/description 和 member hash。合同建议 `sec-foreign-filing-document-set:0.1`。

只从真实 index document table 或 primary 原件内精确附件链接派生 URL；文件必须是 SEC HTTPS 同 issuer、同 accession 目录的普通 basename，不接受跨目录/issuer/host、查询参数、路径穿越或未知重定向。index.json 的普通文件名列表不能单独证明申报类型/附件角色。链接证明和 typed document table 来源必须明确，不能仅凭文件名或自由文本。

先取得全部合格成员清单，保留申报人声明的 type/description；EX-99.x/EX-13.x 只算候选披露文件，不认定财表。不能只挑一个 EX-99.1：年报可能把财务报表、管理层讨论及其它资料分为多件。未知类型明确保留未支持原因，不把清单裁成成功。附件及 primary 使用不同 document refs；不合并原文 bytes/哈希，不将所有成员冒充一次完整财报。

HTML/TXT 复用已有受控 fetch/render；PDF 需新的 parent-bound attachment URL helper 显式允许，并沿已有 PDF renderer 页数/解压/字符上限；不顺带扩大旧 Exhibit13/8-K helper 的权限。不能自动 OCR 猜表格。原件完整而渲染截断时原件可取得，但 reader 不能宣称材料完整。

fetch 在原单子进程/单队列名额和权限预算内逐项执行，不能在主文件 child 中偷偷追加无界 HTTP 串。建议每轮最多 1 个 index 或附件，索引原件 ≤4 MiB / 最多512 document rows，每原件沿现16 MiB上限；eligible 清单持久分页，不以最近 N 截断。超上限显式 overflow/partial，未完成成员保留待办。当前包不新增付费模型；未来正常公开读取仍按现治理及调用账处理。

成功边界沿现 fetch manifest + Core receipt + configured spool 强读，新增亲属关系 sidecar/record 只可追加在已完成 ticket 上，由 single-writer 注册。reader 重算 parent/index/member/attachment 证明；历史国内票据继续旧分支。新关系证明不是正式财务事实。缺 primary/index/附件或 hash 失配为可诊断 missing/refused，不能退到猜 URL、未绑定缓存或捏造季度。

## 文件与迁移边界

1b 候选源文件：新 `sec_foreign_filing_documents.py`；`public_web_fetch_cli.py` 的受控 index/attachment 获取；`public_web_fetch_launcher.py` 的 exact parent 成员参数；`sec_filings_index.py` 的新增 parent-bound URL builder；`mission_source_discovery.py` 有界公平选择与 outcome 接线；`acquired_sec_filings.py` 和 `document_research.py` 的独立附件 reader/source binding。尽量不改旧函数默认行为。

优先复用 fetch manifest、ticket 和 Core 现有 raw-artifact/receipt 表；新 sidecar 有版本和强 hash。若成员必须进入持久 acquisition queue，应使用 writer owner 的专门 append-only 关系表及 schema migration，闭合 parent/index/member 身份、唯一键与游标；不从 child 另写 Core。不能弱化 `coverage_mission_discovered_documents` 原 unique(mission,document_ref)，也不能让普通 public URL 无来源冒充 SEC 附件。quiet 阶段预建必要 index，不在 running constructor 扫建大表。

1b 不扩大 sec-financials v5、不修改旧 statement rows/Work、source proof、schema hash、表单 enum、权限或失败历史。NTR 只是诊断样本，没有 ticker 分支。

## 后续 1c：原始事实到正式三表

1c 是独立正式合同，而不是给 1b 放开 enum：从 exact filing/document-set 证明报表角色、会计准则（若原文未证明则 unknown）、currency/unit、instant/duration、完整 dimensions 和 presentation/calc 关系。XBRL 可复用确定性 parser，但必须验证它确实覆盖附件，保留原 taxonomy；HTML/PDF 需 source-bound table/line + quote/cell/context 证明，不能把大模型摘要直接写 statement ledger。

6-K 只在报表本身明确的起止日期和披露角色证明下产生 annual/interim/duration；filingDate/reportDate 不等于季度。H1/9M 保留 cumulative；季度差分只有同会计基础/币种/dimensions、完整已知期间可严格推导时才成立。其余为明确缺口。缺三表、无 XBRL或冲突不是公司没有财报。

需新 governed sec-financials 版本/相关 output + ingest proof、exact owner ledger migration；旧 v2–v5/hash继续可读。后续 consumers 包括 sec_financials_core/normalise/CLI/launcher、CoverageMission、mission_statement_lane、sec_statement_form_schema/periodic_filings、calendar/quarter derivation、statement_context、company_model_inputs。IFRS 的利润/税/EPS/现金流角色映射要显式 source-bound 与完整历史 tie，不按英文 label 或 ticker 自动套 us-gaap。

## 最小验收

跨 synthetic 20-F、40-F、6-K：两个/多个附件均保留；40-F  financial statements + MD&A 分件；纯公告6-K不产生季度；真正 interim 的 duration 保留H1；同 accession 错 issuer、错 query/profile/parent/index/raw/member/hash、未列文件/重定向/路径穿越均拒；missing/corrupt与PDF/截断状态明确；单slot/原预算/公平游标、restart已完成原件免费复读且不重复 fetch，overflow未丢队列；旧8-K/10-K tickets及manifest逐字兼容。

1b 真实验收必须等待正常队列取得一份 foreign primary/index/附件后只读重放；目前不具备此样本，不能宣称NTR的三表已可建模。
