# AlphaEngine live connector bridge v1

## 结论

开发候选已经把 AlphaEngine 从 recorded shadow 推进到受限的 live `search_library → get_document` 原始证据链。
每次调用必须绑定 exact CompiledConnectorPlan、operation-scoped profile、credential use receipt、quota reservation、
raw Artifact 和 SourceEnvelope。该切片没有部署，也没有把检索内容变成 Evidence、Claim 或 Thesis。

真实只读 canary 搜索到 10 条 ACN 相关结果，并取得所选文档的首个 30,000 字符分片和 `next_cursor=30000`。
这证明 bridge 和 adapter 能连接现有 AlphaEngine MCP；它不证明完整文档已经获取，也不代表 live Core authority
已经接通。

## 已实现

- `LiveMcpTransportPlan 0.1`：把 frozen AlphaEngine inventory、CompiledConnectorPlan/step、operation、参数、schema、
  transport target 和 bridge ref/hash 冻结为一个 operator-installed plan；
- `LiveMcpAdapterRequest 0.3`：闭合 wire 只保存 opaque ref/hash，不保存 endpoint、token、cookie、Authorization、
  server config 或任意 tool name；
- host-owned loopback bridge：只接受显式端口的 `127.0.0.1` / `::1` `/mcp`，关闭 proxy/redirect，限制 exact tool、
  wall deadline、response bytes、strict UTF-8/JSON/SSE 和 JSON-RPC request id；
- live admission gate：在 quota 前后重检 Catalog、profile、resolver、call、invocation、lease、compiled plan、transport
  plan 和 credential authority；每个 physical call 都形成独立 use receipt；
- AlphaEngine adapter：把 frozen search filter 映射到真实 `search_library` schema，并把 document ref/cursor 映射到
  `get_document(doc_id, offset)`；成功结果保存 exact raw JSON-RPC response；
- provenance：搜索结果写成 `alphaengine-doc:<doc_id>`；文档分片写成
  `alphaengine-doc:<doc_id>:sha256:<content_sha256>`。cursor 存在时只能声明 `partial`；
- recovery：同一 RunnerRequest 重放不会产生第二个上游调用；进程在 `observed` 后崩溃时只补 authority 写入。

## 同步修复的契约债务

- live JSON Schema validator 现在执行 `const`、`enum`、`maxLength`、`maximum`、date/date-time format、array/object
  size 等 frozen inventory 已使用的 keyword；
- credential use idempotency hash 不再包含 authority 当前时钟。同一 use 在时钟前进后仍返回原 receipt；
- JSON scalar、memoryview 和 collection 不能冒充 host-owned opaque credential handle；
- transport executor 的 raw artifact 标题和错误文案改为 connector 通用表述，不再把 live 调用标成 recorded。

## 验证

- credential + recorded MCP shadow + live bridge 专项：33/33 通过；
- connector/ResearchPlan 组合超集：160/160 通过，耗时 862.426 秒；
- `compileall`、`git diff --check` 通过；
- Python 3.13 `build --no-isolation` 成功生成 sdist 和 universal wheel；两份新 contract 和两个新 Python module
  已进入构建产物；
- 真实 AlphaEngine 只读 bridge/adapter canary：search succeeded，10 records；get document succeeded，首个
  30,000 字符分片，`next_cursor=30000`，文档正文没有打印到验收输出。

本轮没有重跑约 42 分钟的全量 658 基线。风险匹配验收覆盖了所有被修改的 connector、credential、transport、
recovery 和 ResearchPlan 模块。

## 尚未开闸

- 没有 production Catalog/profile/credential grant，也没有 live Core write；
- 没有 ACN production mapping，没有模型调用，没有 Evidence/Claim/Thesis mutation；
- 没有实现 bounded multi-page document acquisition，因此当前只能保留分片和 cursor；
- 没有接 Gemini web search 或独立 web fetch；
- 没有验证 AlphaEngine provider 侧的跨进程 exactly-once。Dalton 能保证 journal 命中后的本地 recovery 不重呼，
  但进程若在 provider 已收到请求、Dalton 尚未写入 `observed` 前崩溃，仍需 provider request journal 或查询接口
  才能关闭 ambiguity window。

下一道 gate 是把每个 document page 建模为独立 physical call，验证连续 offset、max pages、byte budget 和终点，
再形成完整文档 authority。完成后接 Gemini web search discovery 和受限 web fetch，供 US IT Services 行业研究
coordinator 组合使用。
