# Connector P0-4a Connector Shadow projection

日期：2026-08-14
部署状态：未部署；未连接 OpenClaw live inventory；未访问真实数据源

## 结论

P0-4a 第二笔提交发布 projection schema 0.2，把 metadata sync 和 connector authority 的安全状态投到
可丢弃的 dashboard read model。
Projector 只读 source DB，投影不参与 Catalog、Runner、Scheduler 或 quota admission。P0-4a 至此覆盖可信
snapshot sync authority 与 Connector Shadow；live attach、真实网络和研究 WorkOrder 仍是 No-Go。

## 投影内容

- metadata source：active instance、catalog generation、snapshot head、freshness、最新 ingest 和最新 reject；
- connector operation：profile/version、operation、source identity、auth mode、最新 health/circuit 与未解决的
  blocking incident 数；
- physical attempt：outcome、开始/完成时间、retry、最新 Usage correction、对应 Cost 和 reservation 最新
  Settlement；
- quota：按 stable scope/window 汇总 reservation 与 reserved/consumed/released/indeterminate 计量；
- incident：只投 type、severity、state、相关 authority ref 和时间。

API 新增固定只读 `GET /v1/metadata-sources` 与 `GET /v1/connectors`；静态快照和本地 HTML 的 Connector
Shadow 面板使用同一数据结构。

## 安全边界

- Core 与 CapabilityCatalog 使用 SQLite `mode=ro` 打开；投影前后 source DB hash 保持不变；
- read model 不保存 raw body、authority `record_json`、incident detail、credential/slot、provider request/
  usage ref、artifact locator 或 source DB 路径；
- 投影只能从 append-only/latest-chain authority 重建，不能成为 admission 输入；
- 完全没有 connector/P0-4 metadata-source 表的旧 baseline 返回 warning 和空集合；partial schema fail closed；
- watermark 纳入会影响投影的 connector authority 与 metadata registration/head/ingest event；这些事实变化
  会改变水位。

## 验证

- dashboard/dashboard-projector：20/20；service/static：7/7；
- Python 全量 284/284、broker 15/15、`compileall` 与 `git diff --check` 通过；
- Fable 5 先后复现并关闭同 timestamp event 排序、Connector 16 张 authority partial schema、Catalog anchor
  绕过和 P0-4 metadata 5 张 authority sidecar 漏检，最终裁决为 **Go**；
- 固定 `SOURCE_DATE_EPOCH=1700000000` 的两次 Python 3.13 no-build-isolation wheel SHA-256 均为
  `655f4af42fa0db54524ad5512fc29d6eea4320c64777fe3014918622a7fe7910`；全新 Python 3.13 venv 隔离安装后
  可导入 projection schema 0.2、创建 5 张新 read-model 表、读取 2 个 HTML API endpoint，SQLite
  integrity 为 `ok`；
- 专项用真实 `ConnectorStore` 事实链覆盖 Usage v1 → v2 correction、actual/estimated Cost、consumed/
  indeterminate Settlement、429 retry、quota、open circuit、blocking incident，以及 accepted metadata head +
  rejected gap；同时探测敏感值和禁投字段未进入 JSON 或 SQLite projection。

## 未完成

OpenClaw live inventory attach、public network total deadline/可终止 transport process、真实 A股/SEC source、
authenticated MCP/AlphaEngine、部署、ContextPack/Checkpoint/ClaimIndex builder 和研究 WorkOrder 均未开放。
