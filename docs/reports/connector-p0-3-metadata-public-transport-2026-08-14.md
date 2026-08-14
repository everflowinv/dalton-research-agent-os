# Connector P0-3 metadata importer 与 public transport safety

日期：2026-08-14
部署状态：未部署；未访问真实数据源；未签发 credential grant

## 结论

本切片完成两条离线边界：OpenClaw skill/MCP metadata 可以进入 Dalton 的受控 staging，但不能绕过
Capability Registry/human promotion 直接发布；public connector 有了 credential-free、DNS/IP/redirect
逐跳复核并 pin socket 的 HTTPS transport。两条边界尚未接真实 connector，因此不能称 A股、SEC 或
AlphaEngine shadow 已运行。

## OpenClaw metadata importer

- `OpenClawCapabilitySnapshot` 是闭合契约。skill 只携带 compact metadata、opaque instruction ref/hash；
  MCP tool 携带 compact metadata、input/output schema ref/hash/body。snapshot 不接受 skill instruction、
  prompt、tool output、路径、URL、server config 或 credential；
- schema artifact 采用 `(schema_ref, schema_hash)` immutable 存储；CapabilityCatalog 只保存 ref/hash，
  `search/describe` 不展开 schema body；
- importer 不自动 `publish`。descriptor proposal 仍要经过 trusted approval resolver；permissions、policy、
  visibility 和状态继续由治理层提供；
- 对 imported capability，Catalog `publish` 会逐项核对 kind/name/label/summary/aliases/tags/source/contract
  与 source/schema hash，caller 不能改 metadata 后借原 approval 发布；
- complete inventory scope 内发生 metadata/source/schema 变化或删除时，事务会撤下 current descriptor 并
  推进 catalog epoch。旧 lease 在 use-time gate 因 epoch/revision 变化失败；相同 snapshot 幂等重放不
  推进 epoch；partial scope 不把缺项当删除；
- 首次 snapshot 也会对照已有但未被 metadata authority 观察过的 external descriptor；不匹配就先撤下。

## SSRF-safe public HTTPS transport

- 只允许 `GET/HEAD/POST`、HTTPS、443 和 exact allowlisted hostname；不读取环境 proxy；
- URL userinfo、fragment、非标准端口、control character、credential-shaped query 参数被拒；request header
  只允许 Accept/Accept-Language/Content-Type/User-Agent，Authorization/Cookie/X-API-Key/Host 等被拒；
- POST body 只允许 JSON 或 form，并递归拒绝 credential-shaped field；public transport 的方法签名没有
  credential grant 参数；
- 每一跳重新解析 URL、检查 host allowlist、解析 DNS。A/AAAA answer set 只要含一个 private、loopback、
  link-local、CGNAT、reserved、multicast、unspecified 或 IPv4-mapped private 地址，整次请求即失败；
- socket 直接连接已验证 IP，并校验 peer IP；TLS 仍使用原 hostname 做 SNI 与证书验证，避免 DNS 验证后
  由 HTTP client 再次解析造成 rebinding；
- redirect 不自动跟随。每个 Location 都重新走相同校验；多个 Location、越界 host、超次数、禁用 redirect
  或 POST redirect 均失败；
- body size 同时检查 Content-Length 和 streaming bytes；冲突 Content-Length、非 bytes reader、超限均
  fail closed。敏感 response auth/cookie header 不返回 adapter；raw body 只写 bounded sink，同时给当前
  source-specific normalizer 一份同上限的内存 bytes，不暴露文件路径。

## Credential authority 分界

- `CredentialGrantEnvelope` 只保存 grant ref、authority ref、target ref、profile/lease/adapter exact hash、
  logical slot refs、operation、expiry 和 max calls，不保存 credential value；
- `CredentialAuthorityPort` 未来返回不可序列化 host-owned handle。OAuth、MCP auth 和 secret material 不进
  Core DB、journal、AdapterRequest 或 public transport；
- ConnectorAdapterRequest 0.1 仍固定 `credential_grant_ref=null`。AlphaEngine 不能伪装成 public HTTPS
  connector，也不能把 loopback MCP endpoint 塞入 public `allowed_hosts`；它需要单独的 mcp-managed
  profile/runner wire 与 revoke/permission 演练。

## 验证与边界

- 新增 importer/SSRF/credential 专项 15 项，覆盖 metadata hash tampering、prompt/path/credential-shaped
  schema、complete/partial scope、pre-existing external descriptor、catalog epoch、private/mixed DNS、
  inventory 删除后禁止重新发布、disabled capability 直调 Catalog 绕过 builder、redirect escape、
  URL/header/body credential channel、stream/Content-Length 上限和 closed grant；
- importer/public transport/credential 专项 15/15、Python 全量 269/269、OpenClaw broker 15/15；
  `compileall` 与 `git diff --check` 通过；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 两次 wheel SHA-256 相同：
  `c9af233004f0a6bed406572f97c1802cef06ddefc20b0c17728302bf7138ac86`。隔离安装可导入三个新模块、
  创建 6 张 external metadata 表并找到 2 份新 contract，SQLite integrity 为 `ok`；
- 系统 Python 3.13 的 `pip wheel --no-build-isolation` 因本机没有 `setuptools.build_meta` 失败；使用
  build isolation 的两次构建通过。该环境缺口没有记成代码失败，也没有被隐藏；
- 实现提交 `e1ab94c`；GitHub CI 的 broker、Python 3.11 和 Python 3.13 全部通过：
  <https://github.com/everflowinv/dalton-research-agent-os/actions/runs/31828754012>；
- 本轮没有网络 smoke、OpenClaw live exporter、metadata sync daemon、真实 connector、authenticated runner、
  connector dashboard projection、部署或 Agenda/Ledger 接入；
- 下一安全步骤是补 exporter/sync 与 connector projection，再发布 A股公告、SEC public profile 的 recorded
  fixtures；AlphaEngine 先发布 mcp-managed wire，不复用 public transport。任何真实 call 仍需单独 Go gate。
