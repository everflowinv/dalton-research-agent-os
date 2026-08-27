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
- OpenClaw 可以提供模型、消息、审批和投递连接器；Core 常驻运行时不读取 OpenClaw 配置或凭据。显式校准命令只投影 provider/model、上下文、价格和 broker profile 等公开路由元数据，忽略密钥与 headers。
- 旧 OpenClaw agent 的约束、研究结果和 cron 只作为 legacy input 归档。归档不代表采用、兼容或继续运行。

旧工作流的初步取舍见 [docs/legacy-workflow-disposition.md](docs/legacy-workflow-disposition.md)。完整契约见 [SPEC.md](SPEC.md)，当前完成度与未完成项见 [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)，当前执行顺序见 [Phase 8 单主题自主认知闭环](docs/reports/phase8-single-topic-autonomous-cognition-loop-v1.0-2026-08-27.md)，Connector 边界见 [docs/CONNECTOR_PROTOCOL.md](docs/CONNECTOR_PROTOCOL.md)。

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

# S7f：只读复制一个现有 Core，在临时副本验证 weekly coordinator；不投递外部消息
python3 scripts/run_weekly_brief_coordinator_canary.py \
  --source-core /ABSOLUTE/PATH/TO/core.sqlite \
  --plan deploy/phase1/weekly-brief-schedule-us-it-services-v1.json \
  --policy deploy/phase1/governance-policy-v3-weekly-brief.candidate.params.json

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

# 对照当前 OpenClaw 模型清单；报告不包含密钥，新/改 profile 进入 smoke_required
python3 scripts/reconcile_openclaw_model_catalog.py \
  --openclaw-config /ABSOLUTE/PATH/TO/openclaw.json

# 显式带入当前模型清单跑一个付费 smoke；不会自动调用或自动上线新模型
dalton-thesis-impact-calibrate-matrix \
  --openclaw-config /ABSOLUTE/PATH/TO/openclaw.json \
  --profile-id profile:NEW_MODEL --case-ref calibration:thesis-impact:001 \
  --output-dir temp/model-smoke --socket-path /ABSOLUTE/PATH/TO/broker.sock \
  --auth-key-path /ABSOLUTE/PATH/TO/broker.key

# Owner 授权后的 3×30 provider-controlled verifier 生产 canary；三重硬顶，产出验收裁决
dalton-thesis-impact-verifier-canary \
  --output-dir temp/verifier-canary-3x30 \
  --profile-id profile:gemini-3-7-flash \
  --thinking-level low --rounds 3 \
  --per-case-cap-usd 0.05 --per-round-cap-usd 1.60 --campaign-cap-usd 5.00 \
  --socket-path /ABSOLUTE/PATH/TO/broker.sock \
  --auth-key-path /ABSOLUTE/PATH/TO/broker.key

cd integrations/openclaw-model-broker
npm run check
```

默认测试不需要真实模型凭据，也不访问网络。SEC canary 是显式运行的开发验收，不属于默认 CI；
它只访问 `data.sec.gov`，所有 SQLite 和 raw spool 都在隔离临时目录中创建。

## macOS 常驻服务

安装脚本会把 wheel 和 COS 可选依赖装进 Dalton 自己的 venv，在 `~/Library/Application Support/Dalton/` 创建 owner-only 配置和状态，再加载 writer、controller，以及启用时的 Agenda control 和 thesis-impact 短任务 LaunchAgent：

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

截至 2026-08-27，Phase 7 已收口，Phase 8 的首批 authority 已在 live：

- live Core 有 6 条正式 Claim / 6 条 Evidence：5 条 SEC policy 自动提交（ACN、CTSH、EPAM、IBM、DXC），1 条 transcript 经 owner 人工确认；
  Phase 7「≥5 条 policy 自动提交 SEC Claim」严格退出门槛已达成；
- 五家公司 live evidence pack v2 与 overlay 已注册，lane-only brief v2 可重放；
- ResearchConstitution v1、行业 Thesis 与 ACN Thesis 已经 owner 人工准入进入 live，ACN 的 company→thesis 映射已激活；
- policy-controlled Weekly Brief coordinator 已部署并激活：schedule plan v3 绑定五家公司 pack 与 ACN 映射，
  首个自动窗口 2026-09-03 07:00 America/New_York，同窗口重放不重复投递；
- thesis-impact 已产生首条真实 assessment（裁决 insufficient，不把单一收入指标过度解释成投资结论），
  独立 verifier pass；当日定位并修复了 Gemini host 路径故障（broker 把 thinkingLevel 合并进
  providerControls 与 proof 形状两处脱节），ACN 链路全链闭环；
- P8b CompanyResearchView 与结构化知识查询已完成 development candidate：纯投影 + writer 只读 ops +
  ContextMaterializer 接手，live 副本 canary 5 家公司全通过；
- P8c-1 已把 Tier 1 Bounded Planner 准入面接进 live：常驻研究问题「美国 IT 服务需求是否见底」、
  SEC revenue-growth ProbeTemplate 与五家公司循环 v1 已人工准入；
- P8c-2 controller 驱动已上线并完成首次全自主循环：daltond 每 300 秒唤醒停泊循环，确定性 planner
  提案 → Core 准入 → 真实 SEC probe → 源级 outcome → 终态，五轮探测无人干预；
- Agenda Shadow 最新 live cycle 正常交付，controller、writer、projection 和 dashboard health 均为 running；
- Doctrine（writer ops）、Bounded Planner、LLM Planner、Answer Router 和 DocumentIndex 仍是 development candidate，
  尚未接入同一条 live 认知循环。

当前阶段是 **Phase 8「单主题自主认知闭环」**。首个主题固定为「美国 IT 服务需求是否见底」，近期顺序是：

1. 关闭 Phase 7 剩余门槛：第五家 SEC issuer；审核并单独批准 Weekly Brief coordinator 的 live activation；（两者已于 2026-08-27 完成）
2. 建立最小 Research Constitution、行业 Thesis 和 ACN Thesis（已于 2026-08-27 进入 live）；
3. 增加可重建的 CompanyResearchView 和结构化知识查询；
4. 把 Tier 1 Bounded Planner 接进 live，只能选择已批准的 probe；
5. 用 Weekly Brief、Agenda 和 Claim review 的真实反馈建立冻结评测集；
6. 连续四周通过后，才开放 Tier 2 Planner 和语义检索。

当前状态和历史实现记录见 [PROJECT_STATUS](docs/PROJECT_STATUS.md)。Phase 8 的裁决、切片和退出门槛见
[单主题自主认知闭环 v1.0](docs/reports/phase8-single-topic-autonomous-cognition-loop-v1.0-2026-08-27.md)。
