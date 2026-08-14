# Dalton Research Agent OS

Dalton 是面向投研团队的独立研究控制内核。它把任务调度、研究账本、验证、模型路由、成本与产物权威从具体 agent host 中拆出来，让模型和连接器可以替换，研究记录仍可追溯。

项目目前是原型，尚未达到生产部署标准。OpenClaw 只是可选适配层，不是 Dalton 的运行时、数据库或事实来源。

## 仓库边界

- `src/dalton_core/`：Core 契约、Research Ledger、Scheduler、模型路由、Capability Registry、writer service 和只读 dashboard。
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

旧工作流的初步取舍见 [docs/legacy-workflow-disposition.md](docs/legacy-workflow-disposition.md)。完整契约见 [SPEC.md](SPEC.md)。

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

## 开发状态

当前代码已经实现主要契约和本地原型，但生产部署仍缺少独立 OS/container identity、正式 capability sandbox、外部持久化方案、原生事件连接器和完整运维控制面。任何旧工作流切换都要逐项验证，不能因文件已导入就视为完成迁移。
