# Dalton Research Agent OS

Dalton 是面向投研团队的独立研究控制内核。它把任务调度、研究账本、验证、模型路由、成本与产物权威从具体 agent host 中拆出来，让模型和连接器可以替换，研究记录仍可追溯。

项目已有本机常驻控制服务，但仍是原型，尚未达到生产部署标准。OpenClaw 只是可选适配层，不是 Dalton 的运行时、数据库或事实来源。

## 仓库边界

- `src/dalton_core/`：Core 契约、Research Ledger、Scheduler、模型路由、Capability Registry、writer service 和只读 dashboard。
- `deploy/macos/`：owner-only runtime bootstrap、LaunchAgent 安装、卸载和健康检查。
- `contracts/`：跨进程 JSON Schema。
- `integrations/openclaw-model-broker/`：复用 OpenClaw 已管理模型认证的受限 broker。
- `spikes/`：候选 runtime 的隔离实验，不属于生产执行面。
- `docs/reports/`：架构与实现记录。报告描述当时状态，不自动转化为当前约束。
- `tests/`：契约、隔离、幂等、账本、调度和适配层测试。

运行数据库、模型输出、旧研究文件、密钥、OAuth 状态和导入后的 artifact store 不进入 Git。部署时要把它们放在独立的 owner-only 数据目录。

## 设计边界

- Core 是 headless、event-driven 的权威层。
- agent 负责规划和执行 WorkOrder，不能自行提交研究结论或改写治理规则。
- 模型 fallback 必须由 Core 路由并留下 decision，provider 或 host 不能静默切换。
- OpenClaw 可以提供模型、消息、审批和投递连接器；Core 不读取 OpenClaw 配置或凭据。
- 旧 OpenClaw agent 的约束、研究结果和 cron 只作为 legacy input 归档。归档不代表采用、兼容或继续运行。

旧工作流的初步取舍见 [docs/legacy-workflow-disposition.md](docs/legacy-workflow-disposition.md)。完整契约见 [SPEC.md](SPEC.md)，当前完成度与未完成项见 [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)，Connector 边界见 [docs/CONNECTOR_PROTOCOL.md](docs/CONNECTOR_PROTOCOL.md)，Context/Memory/Log 裁决见 [docs/reports/context-memory-log-subsystem-2026-08-14.md](docs/reports/context-memory-log-subsystem-2026-08-14.md)。

## 本地验证

需要 Python 3.11+；OpenClaw broker 需要 Node.js 24+。

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
python3 -m pip install build
python3 -m build

cd integrations/openclaw-model-broker
npm run check
```

这些测试不需要真实模型凭据。真实模型 smoke test 属于部署验收，不能放进默认 CI。

## macOS 常驻服务

安装脚本会把 wheel 和 COS 可选依赖装进 Dalton 自己的 venv，在 `~/Library/Application Support/Dalton/` 创建 owner-only 配置和状态，再加载 writer、controller，以及启用时的 Agenda control LaunchAgent：

```bash
./deploy/macos/install.sh
./deploy/macos/health.sh
```

controller 常驻，LLM worker 不常驻。空闲时 controller 只做 lease 回收、authority 变更检测、dashboard projection、插件重试和健康心跳。静态看板插件只读 projection DB，发布到 <https://eve.lumos.space/dalton/>；它不会修改 COS bucket 的站点首页配置。

卸载脚本只停止 LaunchAgent，并把 plist 移到废纸篓；runtime 和 authority data 保留：

```bash
./deploy/macos/uninstall.sh
```

## 开发状态

Phase 1 已进入单公司 Agenda Shadow：controller 每日从规范化 `PerceptionSnapshot` 生成一次
AgendaCycle，真实经过 Scheduler、Model Router 和 OpenClaw broker，再由确定性权重与稳定
tie-break 选择 ResearchQuestion。结果进入 append-only AgendaDecision，再由 OpenClaw/Discord
bridge 投递通知。bridge 使用 claim lease、确定性 marker、发送后 reconciliation 和 receipt 回写；
重启时会先查找已发 marker，避免重复外发。人工 agree/disagree 改由 Tailscale Serve 后的 owner-only
HTML 控制面提交；浏览器反馈与 24 小时超时默认接受使用两个独立 feedback-only principal。超时默认
接受单独统计，不计入人工标签或认可率。当前不会执行研究，也不会写 Evidence、Claim 或 Thesis。

Connector P0-1 authority foundation 和 P0-2a Runner 控制面底座已完成；当前仍未执行 adapter 或访问
真实数据源，下一段是 recorded journal/spool/authority-port thin slice。生产部署仍缺少独立
OS/container identity、正式 capability sandbox、Model IR、原生事件连接器、
研究 worker/verifier coordinator、更多原生投递渠道和完整运维控制面。任何旧工作流
切换都要逐项验证，不能因文件已导入就视为完成迁移。当前项目状态见
[docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)，最近一次 Agenda 控制面实施记录见
[docs/reports/phase-1-agenda-control-2026-08-14.md](docs/reports/phase-1-agenda-control-2026-08-14.md)。
