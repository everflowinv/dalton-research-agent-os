# DocumentIndex FTS5 thin slice（2026-08-15）

## 结论

DocumentIndex FTS5 已完成开发候选，当前只在隔离测试中运行，未部署、未接 Agenda、未改 cron，
也没有接 ContextPack materializer。它是 ArtifactVersion 的可重建、可删除、只读检索投影，不是
新的 authority；正式 Evidence/Claim/Ledger 仍不由它写入。

## 设计边界

- `DocumentIndexInput` 是 closed contract，只指定 exact ArtifactVersion ref/hash、创建时间和
  内置 `utf8`/`json` extractor。caller 不能传正文、公司 facet 或 source metadata。
- builder 必须从 exact `ObservabilityStore` 读取 ArtifactVersion，再从 exact `RawSpool` 重读
  content-addressed bytes，复核 SHA-256 与 size；提取文本的 ref/hash/size 写入 disposable
  projection record。raw bytes 不写进 authority。
- 有 SourceEnvelope 时，builder 沿 SourceEnvelope → ConnectorInvocation 的 SQL
  `execution_ref/execution_hash` → ConnectorProfile/CallSpec → connector ExecutionInvocation
  做 exact join，并核对 profile/call/source/artifact/producer hash。`source_type` 只来自
  `Profile.source_identity.source_type`，不能把 SourceEnvelope 的 source ref 当 source type。
- 公司 facet 只实现一个 versioned resolver：当 source ref 是 `source:sec-edgar`、source type 是
  `official_filing`、operation 是 `list_filings` 时，读取 frozen CallSpec 的 `issuer`，规范化为
  `company:sec-cik:<10位CIK>`。未知 source 或不安全参数不猜公司，也不接受未定义的 caller
  `company_ref` 参数。
- `content_type` 过滤 ArtifactVersion 的 `kind`；`media_type` 单独过滤 MIME。SEC submissions
  JSON 在这里表示 connector response/filing metadata，不能称为 filing 正文全文。

## FTS 与权限

SQLite FTS5 使用 external-content table，title、source metadata 和内置提取文本都可以随投影
删除并重建。逻辑 snapshot hash 只由 canonical record/ref/hash 组成，不依赖 SQLite 文件字节或
`bm25` 浮点结果。查询的 MATCH、facet、access、日期、分页都参数化；查询返回 metadata 与
immutable refs/hashes，不返回正文。

默认可见 access class 是 `public`，调用方必须在创建 projection 时显式扩大范围。非内存索引文件
强制 `0600`。若 projection 配置为可见 internal/restricted，它仍会把相应 disposable 正文物理
写入 FTS 文件；SQLite 文件权限和默认 access filter 不是多租户或 hostile same-UID 安全边界，
runtime 不得取得 index DB path。

tokenizer 固定为 `trigram`。三字符中文（例如“半导体”）可以有限地命中，二字符（例如“存储”）
可能 miss；这不是通用中文分词，也不承诺任意中文子串召回。后续是否增加 embedding 只在 FTS
miss 率有测量后决定，embedding 只能做 recall-only sidecar，不能进入 authority。

## 敌意测试

`tests/test_document_index.py` 覆盖：

- caller 正文/metadata/company 伪造、内部/受限 access 默认过滤和投影主表 access 篡改；
- ArtifactVersion、SourceEnvelope、ConnectorProfile、ConnectorCallSpec 的 hash/ref 绑定，raw
  spool hash/size 复核，以及 unknown source 的空公司 facet；
- FTS `delete-all`、主表正文换绑、主表与 record/facet 不一致时的 `integrity-check` fail closed；
- FTS operator literal 化、NUL/control/query 长度、limit/offset/date 边界、`kind` 与 MIME 分面；
- 空库、Unicode/CJK 三字符与二字符边界、删除后重建逻辑 hash 一致、authority 行数不变，以及
  文件权限 `0600`。

## 验证与后续

验证命令和结果：

- `.venv/bin/python -m unittest -v tests.test_document_index`：15/15 通过；
- `.venv/bin/python -m unittest -v tests.test_document_index tests.test_observability tests.test_connector tests.test_raw_spool`：51/51 通过；
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`：400/400 通过。
- broker 回归：15/15 通过；固定 `SOURCE_DATE_EPOCH=1700000000` 独立构建两份 wheel，
  SHA-256 均为 `ccd4ad817cf1837ed2e99d48b1cdd1b23e543dcadafede8a72921ff70a3cd5c8`，
  文件大小均为 601,297 bytes；干净 Python 3.13 venv 安装、导入、打包后的 FTS schema 和两份
  新 contract 检查均通过。

实现提交：`89d3f08`。GitHub CI `31887581490` 的 Python 3.11、Python 3.13 和 broker 三个 job
全部通过：<https://github.com/everflowinv/dalton-research-agent-os/actions/runs/31887581490>。
CI 只提示 `actions/checkout@v4`、`actions/setup-python@v5` 和 `actions/setup-node@v4` 的 Node 20
弃用警告，没有测试或构建失败。

全量测试仍有仓库既有若干测试打印 `ResourceWarning`，但没有失败；本 slice 新增测试通过
`addCleanup` 关闭 DocumentIndex 连接。上述实现已推送 `main`，但没有部署。下一笔是 ContextPack
materializer：从 DocumentIndex 的 immutable refs/hash 读取、复核并按冻结预算组装模型可见正文，
不能让 FTS disposable body 直接成为 authority。
