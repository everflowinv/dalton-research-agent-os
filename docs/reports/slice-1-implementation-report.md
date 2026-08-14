# Dalton Core Slice 1 实施与验收报告

> 日期：2026-08-13  
> 状态：在下述信任边界内验收通过；未接入 Dalton live 系统

## 1. 本轮交付

独立实现位于 `scripts/dalton-core/`，不依赖 `workspace-chem`。本轮冻结并行使了：

- 九份 versioned JSON Schema：WorkOrder、ResultEnvelope、RuntimeProfile、
  ModelInvocation、DomainEvent、CapabilityProposal、ThesisVersion、
  VerificationRecord、GovernancePolicyVersion；
- 严格 Python contracts：拒绝未知字段，角色和 capability 保持配置化，冻结 verdict、
  value kind 和 independence predicate 词表；
- SQLite hybrid-temporal store：T-stage、T-verify、T-commit 分段事务，append-only
  version/event/invocation/verification/policy，current pointer 只在 T-commit 更新；
- 版本化 governance policy：生效区间、版本链、actor、内容 hash；默认只允许
  `pass`，producer/verifier 必须来自不同 `model_family`；
- 完整 `ThesisVersion`、`VerificationRecord`、`GovernancePolicyVersion` wire JSON
  落库，不能以临时 payload 冒充冻结实体；
- fresh / duplicate / conflict 幂等三态，request hash 绑定 change、verification、
  staged content 和 request；
- 本地 deterministic executor：检查 capability、schema version、预算、profile limits、
  声明/实际副作用；
- 不调用真实模型的 producer/verifier stub，以及一条离线
  stage → verify → commit walking skeleton；
- wheel 打包 `schema.sql` 和九份 JSON Schema。

## 2. 已验证失败路径

- 缺 verification、非 `pass` verdict、同一 invocation 自证、同 model family 验证；
- policy 过期/尚未生效、verification 后切换 policy；
- invocation ID 碰撞、未知 invocation 字段、缺 provenance；
- staging 直接改写、staged 内容与 hash 脱钩、event provenance 与 thesis identity 脱钩；
- 同幂等 key 不同 request、同幂等 key 跨 target、调用方伪造 request hash；
- 裸 UPDATE/DELETE 权威历史、绕开 service 更新 current pointer；
- handler 超预算、越权副作用、不支持的 envelope version；
- T-commit 在 version/event/pointer 中途失败，以及子进程 `os._exit` 后重开数据库。

## 3. 验收结果

- `PYTHONPATH=src python3 -m unittest discover -s tests -v`：35 项通过；
- `compileall` 通过；
- wheel 构建、隔离目录安装、`python -m dalton_core.demo` 通过；
- wheel 内含九份 `.schema.json` 和一份 `schema.sql`；
- source isolation scan 未发现 live Coverage OS 路径或数据库引用；
- Dalton live coverage DB 的 SHA-256、mtime 和大小与开工前一致：
  `80eb316470ac5a712206749aadee75893eb3df1473aa9b97c705128464715bc6`、
  `1786638929`、`204800`。

独立敌对审计第一轮不予放行，并复现 staging 篡改、request hash 旁路、profile limits
未执行等问题；修复并加入回归测试后，第二轮没有发现新的 P0/P1，允许在明确限制下
验收。

## 4. 信任边界与未完成项

SQLite trigger + commit service 是完整性防线，不是抵御持有 DB 文件的同用户恶意进程
的 sandbox。数据库文件设为 `0600`，但任何外部 runtime、自生成工具或 OpenClaw
adapter 在独立 writer service 上线前都不得获得 DB 路径。

本轮明确未做：

- 不迁移 Coverage OS，不读写 live DB，不改 cron；
- 不接 OpenClaw、Pi、DeepSeek Harness 或真实 LLM；
- 不实现 Claim/Evidence、Model IR、Excel exporter、agenda/scheduler；
- CapabilityProposal 只有 contract，尚无 sandbox/eval/promotion；
- 不把 SQLite 定为生产长期存储。

Runtime spike 排在 Core contract 和 commit skeleton 之后。下一步先补独立 writer
service 边界、Claim/Evidence 与 Capability Registry 的最小 slice，再让 Pi、
DeepSeek Harness 和 Dalton-native loop 竞争同一组 RuntimeProfile / WorkOrder /
ResultEnvelope contracts。
