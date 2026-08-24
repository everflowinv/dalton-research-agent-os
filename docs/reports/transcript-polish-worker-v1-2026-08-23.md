# TranscriptPolishWorker v1

日期：2026-08-23
状态：development candidate；未接 live 模型或真实 AlphaEngine 文档

## 完成范围

本切片实现一条最小、可重放的 transcript 派生链：

`complete AlphaEngine document manifest → original UTF-8 bytes → model-shaped candidate → Core conservation verifier → derived polished artifact → bounded-loop observed Outcome`

模型只允许提交 `TranscriptPolishCandidateV0.1`：一组有序的 `source_start / source_end / source_sha256 /
polished_text`。候选不能声明来源、权限、验证状态、引用 authority 或 artifact identity；这些字段全部由 Core 生成。
候选若带 Markdown fence、重复 JSON key、未知字段或不合法 span，会在写入前拒绝。

v1 先用 deterministic candidate fixture 验证正式合同，没有调用付费模型。下一步接真实 model worker 时不改候选合同。

## 原稿与 span mapping

Core 不接受调用者自报的原稿。`TranscriptPolishAuthority` 重新读取并验证 exact
`AlphaEngineDocumentAcquisitionManifest`，只接受 `status=complete / termination_reason=terminal`，再从 content-addressed
spool 读取 assembled object，复核 manifest ref/hash、declared/prefix/object SHA-256、bytes 和字符数。

隔离测试中的 `manifest_resolver` 是内存 fixture。生产接线必须让 resolver 从 Core authority store 按 exact version ref
读取不可修改的 manifest，并再次核对调用方冻结的 ref/hash；不能直接返回 WorkOrder 或模型提交的 manifest JSON。

候选 spans 必须：

- 从字符 0 开始，按顺序无缝、无重叠覆盖到原稿结尾；
- 每段 source slice SHA-256 与候选声明一致；
- 每段最多 2,000 个字符，整份最多 256 段、200,000 个字符；
- Core 生成对应的 polished start/end 与 polished slice SHA-256。

因此 polished 文本的任意位置都能回到一个有界的原稿 span。这个 mapping 只证明文本变换绑定，不把 polished
文本升级为独立事实来源。

## 数字与专名守恒

这里沿用现有 transcript-polish 工作流的保真规则，并把可机械执行的部分收敛成 Core verifier：

- 逐段及全局提取金额、币种、比例、年份、数量和英文量级词；比较有序序列，不只比较集合或总数。因此两个年份
  或两个金额互换也会失败。
- 自动保护 acronym、CamelCase、含数字产品名和多词首字母大写专名；WorkOrder 还可增加原稿中确实存在的 exact
  protected terms。逐段及全局同时比较出现次数和顺序。
- polished/original 字符比必须在 0.65 至 1.20 之间，防止把逐字稿压成摘要或大幅补写。
- v1 不允许模型自行修正受保护专名。未来若要改 ASR 专名，必须增加独立、可引用的 verified correction authority，
  不能靠模型自证。

这些检查不能证明所有语义都完全等价，所以产物固定 `citation_authority=original_only`。Evidence/Claim 必须引用原稿
authority 和原始位置；polished artifact 只供模型上下文与人工阅读，不能算第二个独立来源。

## 受限能力与 Planner Loop

- capability：`capability:dalton:local:transcript-polish`
- operation：`verify_and_materialize_transcript_polish`
- runtime：`runtime-profile:dalton-core-transcript-polish:0.1`
- permission：`read_exact_alphaengine_document_artifact`
- output：`schema:transcript-polish-probe-output:0.1`
- verifier：`verifier:transcript-polish-conservation:0.1`

隔离测试用 human-admitted ProbeTemplate 把该能力放入现有 Bounded Planner Loop。Core 继续签发原有 WorkOrder，
Scheduler 仍是唯一队列；candidate 通过验证后生成 append-only `TranscriptPolishArtifactVersion`，ResultEnvelope 再由原有
CoverageManifest / ResearchOutcome 路径记为 `observed`。没有新建 queue 或 DAG，也没有写 Evidence、Claim、Thesis、
Model Input 或 generic ArtifactVersion。

## 验证与未完成项

专项 4/4、相关回归 31/31 通过，覆盖：

- exact complete AlphaEngine manifest 与 assembled bytes 重验；
- candidate closed contract、重复 key/fence 拒绝、完整 span partition 和 slice hash；
- 数字改变、数字换序、专名改变、专名换序、新增专名、缺 span、错误 hash 和 source hash drift 全部 fail closed；
- derived bytes 的 content-addressed replay、artifact 版本重复与 SQL immutability；
- ProbeTemplate → Core admission → 原 Scheduler lease → TranscriptPolishWorker → ResultEnvelope → observed Outcome。

尚未完成：真实模型 routing/accounting、transcript 专用 frozen corpus 与模型横评、真实 AlphaEngine canary、超过 200,000
字符的 chunk/assembly、verified correction authority、generic ArtifactVersion 注册、中文/多币种全部表达、语义级遗漏检测和
live deployment。当前自动专名保护是保守词法规则，不是完整 NER；任何 verifier 无法证明安全的候选都会拒绝并回退原稿。

下一笔应先接 routed model worker，并用专门的 transcript corpus 比较高上下文模型；Planner 的 DeepSeek V4 Flash 选择
不能直接外推成 transcript worker 的模型选择。
