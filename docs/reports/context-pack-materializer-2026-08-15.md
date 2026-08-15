# ContextPack authority-bound materializer（2026-08-15）

## 状态

开发候选，未部署、未接 AgendaCoordinator、未改 cron，未写 Evidence/Claim/Ledger。

## 设计

本切片新增 `ContextMaterializer`。它只接受 exact `DaltonStore`、`ObservabilityStore` 和 `RawSpool`，并且只
支持 `claim`、`artifact` 两类 ContextPack input。Mandate、PerceptionSnapshot、SourceEnvelope 没有可靠的
exact 正文 reader，因此直接 fail closed；不接受 caller resolver、caller body、caller metadata、路径或
DocumentIndex FTS 正文。

ClaimVersion 从 Core `claim_versions` exact row 读取，检查 SQL 列、record JSON、canonical record hash、0.1/0.2
validator 和 requested ref/hash；render 同时带入 ContextPack 绑定的 exact ClaimIndex entry，因此 disputed 状态
不会从模型正文消失，ClaimVersion 仍是事实权威。ArtifactVersion 通过 Observability API、跨代 index、对应 record row 读取，再
检查 raw content hash/size；正文从 RawSpool 读取并用内建 `utf8`/`application/json` extractor 重算。storage
locator 只在可信 authority 校验内使用，未进入 manifest；默认 access class 是 `public`，扩大范围必须在实例上显式
配置。

`build_authority_context_pack()` 是旧 ContextPack 0.1 的 authority-bound builder：输入只有 `kind/ref/hash/priority`，
正文由 authority 计算，不接受 `content`。Materializer 对每个 pack input 重新计算正文 hash、token、byte，和
ContextPack original accounting 逐项比较；不一致、ref/hash 换绑、重复 authority 或 tamper 都 fail closed。
旧 coordinator 的 caller-content pack 不因兼容而放宽。

返回值是 `ContextMaterialization(rendered_text, manifest)`。`rendered_text` 只在当前调用短生命周期存在，不进入
manifest。模型输入是确定性的 quoted JSON-lines：header、每个 `_dalton_quoted_input`、footer 均有明确的
ContextPack/renderer/tokenizer hash，原文只出现在 `quoted_data`。ContextPack 的预算只管正文选择；
materialization 另有 envelope-inclusive 总预算，并把 header、每条 wrapper 和 footer 全部计入。manifest 是闭合且 content-hashed 的 wire，
只含 authority/body/render hash 和 token/byte 账，不含正文、database path、storage locator、credential。header
与分隔符开销计入显式 max token/byte budget，超限拒绝而不静默裁剪。

## 文件

- `src/dalton_core/context_materializer.py`
- `src/dalton_core/research_context.py`
- `src/dalton_core/document_index.py`
- `contracts/context-materialization.schema.json`
- `tests/test_context_materializer.py`
- `SPEC.md`
- `docs/PROJECT_STATUS.md`
- `docs/reports/context-pack-materializer-2026-08-15.md`
- `src/dalton_core/__init__.py`

`pyproject.toml` 已有 `contracts/*.schema.json` 的通配 data-file 声明，新 contract 会随既有打包规则进入
`share/dalton-core/contracts`，本切片没有新增不必要的打包路径。

## 本轮验证

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest -v tests.test_context_materializer
23/23 passed
```

专项覆盖 claim/artifact authority 读取、ClaimVersion 0.2 Decimal/structured period、caller body/hash rebinding、
SQL/raw spool tamper、Artifact cross-generation index、duplicate/omitted accounting、正文/总预算、确定性、access class、
unsupported kind/media、JSON canonicalization、quoted prompt-like 正文、冻结 build/selection/tokenizer/truncation、
历史 pack 在 Ledger 增长后的 replay、plan/ClaimIndex binding、manifest 敏感字段/path/locator 隔离和 authority 行数不变。

相关回归：

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest -v \
  tests.test_context_materializer tests.test_research_coordinator \
  tests.test_document_index tests.test_claim_index_authority
57/57 passed

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
423/423 passed

broker 回归：

```text
cd integrations/openclaw-model-broker && npm run check
15/15 passed
```

`compileall`、95 份 JSON schema 解析、16 份 SQL schema 清点和 `git diff --check` 均通过。固定
`SOURCE_DATE_EPOCH=1700000000` 独立构建两份 wheel，SHA-256 都是
`e61d35359d52a169c8abd4df7628836715038064ff5167e917c1c3cd007ebd21`，大小都是 611,413 bytes。
Python 3.13.14 干净 venv 安装后，`dalton_core.ContextMaterializer`、新 contract、共享 document extractor 和
ContextPack tokenizer 均可用。
```

## 未决边界

- 本轮尚未把 AgendaCoordinator 的手工 prompt 迁移到 materializer；Planner/Agenda 仍未接线。
- Mandate、PerceptionSnapshot、SourceEnvelope 尚无可以安全复用的 exact 正文 reader，当前显式不支持。
- renderer 使用项目现有 `dalton-search-token` 的正则 tokenizer；它是确定性 token 账，不是模型 tokenizer。
- 历史 pack 按自己冻结的 ClaimIndex 状态重放，不会在 Ledger 增长后静默刷新；新 attempt 若要当前状态，必须
  重新构建 ClaimIndex 和 ContextPack。
- Artifact extractor 按 authority `media_type` 在 `utf8`/canonical `json` 之间确定；PDF、HTML、OCR、分片文档和
  embedding recall 都未实现。
- quoted envelope 只能把 prompt-like 文本标为引用数据并隔离机器控制字段，不能宣称解决一般 prompt injection。
- 本轮未提交、未推送、未部署。
