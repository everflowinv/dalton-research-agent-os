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

旧工作流的初步取舍见 [docs/legacy-workflow-disposition.md](docs/legacy-workflow-disposition.md)。完整契约见 [SPEC.md](SPEC.md)，当前完成度与未完成项见 [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)，当前执行顺序见 [docs/reports/direction-review-and-execution-plan-v0.7-2026-08-21.md](docs/reports/direction-review-and-execution-plan-v0.7-2026-08-21.md)，Connector 边界见 [docs/CONNECTOR_PROTOCOL.md](docs/CONNECTOR_PROTOCOL.md)。

## 本地验证

需要 Python 3.11+；OpenClaw broker 需要 Node.js 24+。

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
python3 -m pip install build
python3 -m build

# 无网络、无付费模型的显式 closure → thesis-impact replay gate
python3 scripts/run_hermetic_research_replay_canary.py

# 生成完整 review evidence；输出路径必须尚不存在
python3 scripts/collect_review_evidence.py \
  --manifest docs/review-evidence/gate0-review.manifest.json \
  --output /tmp/dalton-review-evidence.md

# 可选：真实公共 SEC 只读 canary；不读取凭据，不接 live 数据库
python3 scripts/run_public_sec_authority_demo.py

# 可选：人工批准的完整 ResearchPlan 四步 canary；output-dir 必须尚不存在
python3 scripts/run_sec_research_plan_canary.py \
  --output-dir temp/sec-plan-canary-example \
  --date-from 2026-01-01 --date-to 2026-08-17 \
  --approved-by human:operator

# 在 exact candidate 已经人工接受并完成 Ledger promotion 后，关闭同一条计划
python3 scripts/close_sec_research_plan_canary.py \
  --output-dir temp/sec-plan-canary-example \
  --decision-ref human-review:EXACT_DECISION_REF

# 五家公司同口径 revenue-growth batch；要求仓库 clean，output-dir 不存在
python3 scripts/run_sec_revenue_growth_batch.py \
  --output-dir temp/sec-revenue-growth-batch \
  --filed-from 2025-08-21 --filed-to 2026-08-21 \
  --policy-owner human:operator

# 对 batch 内单家公司做无网络 closure replay
python3 scripts/replay_sec_research_plan_canary.py \
  --output-dir temp/sec-revenue-growth-batch/samples/MSFT

cd integrations/openclaw-model-broker
npm run check
```

默认测试不需要真实模型凭据，也不访问网络。SEC canary 是显式运行的开发验收，不属于默认 CI；
它只访问 `data.sec.gov`，所有 SQLite 和 raw spool 都在隔离临时目录中创建。

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

live 仍停在 Agenda Shadow 和 recorded connector 路径。当前未部署的开发候选已经在隔离 authority 中完成
policy-authorized SEC public ResearchPlan：读取 Company Facts、解析同一 10-Q 的季度收入、独立复算同比、提交
正式 Evidence/Claim 并关闭 Backlog question。固定五家公司 batch 已在同一代码 commit 上完成 Microsoft、Apple、
NVIDIA、Walmart、Amazon 的 5/5 正式 closure、进程重启后无网络 replay，以及 Walmart stale concept fail-closed
控制样本；脱敏摘要和简报已提交。该结果仍是隔离开发验收，不代表已部署到 live。

开发候选还包含 Claim → thesis impact assessment、不同 model family verifier、ModelRouter/OpenClaw broker 接线，
以及模型返回后崩溃时的 durable replay。Gate 2 已在 USD 1.00 hard cap 下使用真实 ThesisVersion 和真实模型完成
隔离 canary：GPT-5.6 Sol assessment 经 crash 后由 `replayOnly` 无重复恢复，DeepSeek V4 Flash 独立复核并给出
`reject`。控制链、记账、family independence、离线 replay 和数据库完整性通过；verifier findings 有明显矛盾，
所以 assessment 未进入 eligible，也未写入简报。

Gate 0、Gate 1 和 Gate 2 控制面验收已完成；模型质量门仍未通过。第一版 verifier 校准基础已冻结 12 个
no-leakage 样本、严重度和评分器，新 WorkOrder 使用严格 `0.2` finding 合同；Gate 2 的已观测结果在正确样本上
形成 1 个误报。下一步只做完整 12/12 候选模型校准，不部署 live、不自动修改 ThesisVersion，也不切旧 cron。
详细状态见
[docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)。
