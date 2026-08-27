# Phase 7 收口、P8a live 激活与 S7f coordinator 激活 v0.1

日期：2026-08-27  
状态：已在 live 执行；owner 于同日批准全部保留 gate（DXC lane、P8a live 发布、weekly brief 自动发布激活）  
执行 source：`81cb0ec` / `5aa773a` / host-retry 与 redrive 两个后续修复提交；部署由 `deploy/macos/install.sh` 完成

## 执行顺序与结果

### 1. DXC 第 5 家 SEC issuer（Phase 7 严格门槛达成）

- `sec_company_facts_lane` catalog 加入 DXC Technology Co（CIK 001688568，`company:sec-cik:001688568`）；
  lane 的 agenda binding 按 universe 变更从 v2 bump 到 v3（旧 v2 binding 不可变，保留）。
- live lane run（ticket `sec-lane-run:b3171f6930f68cb31db839f0`，窗口 2025-08-27..2026-08-27，governance
  `connector-governance:sec-company-facts:v1`）50 秒成功：选取 10-Q `0001688568-26-000069`（2026-07-31 提交，
  fiscal Q1 FY2027，concept `Revenues`），Revenues USD 2,999M，同比 **-5.06%**（prior USD 3,159M），
  source / numeric verification pass，`policy-2` 自动提交正式 Claim
  `claim-version:a4d5fb26…80b14`。
- **live Core 现有 6 Claim / 6 Evidence：5 条 policy 自动提交 SEC quantitative + 1 条 transcript qualitative。
  Phase 7「≥5 条 policy 自动提交 SEC Claim」严格退出门槛已达成。**

### 2. lane-only brief v2（五家公司）

- 新 evidence pack v2 `industry-evidence-pack-version:us-it-services:live-sec-lane-v2`（绑原 lane driver pack v1，
  5 条 binding 含 DXC，dispersion debate 增加 DXC「against」立场）；
- 4 家公司 overlay v2（ACN/CTSH/EPAM/IBM，内容不变、改绑 pack v2）+ DXC overlay v1
  （stance=negative，watchpoint 关注降幅是否收窄）；
- live 副本渲染 5 公司 brief：11,049 bytes，DXC -5.06% 全链路出现，连续渲染逐字节一致。
- 仓库 manifest：`deploy/coverage/us-it-services-industry-evidence-v5.json`。

### 3. P8a live 激活

- thesis driver pack v2 `driver-pack-version:us-it-services:v2`（prior=live lane v1；lane driver/metric/template
  + 4 driver / 13 metric / 2 模板的超集，pointer 指向 v2）；
- P8a mandate `mandate-version:us-it-services-constitution-p8a:1`（scope=industry+ACN）；
- **ResearchConstitution v1** `constitution-version:us-it-services:1`（hash `3d60c793…f14d`）：
  绑 mandate / pack v2 / active `policy-2` / weekly brief plan v3 hash；doctrine 绑定为 null
  （doctrine 尚无 writer op，待后续切片补 `publish_doctrine_pack` ops 后由 constitution v2 绑定）；
- 行业 Thesis `thesis:us-it-services:demand-bottoming`（`thesis-version:b35bdc3d…`，human_admission，low）
  与 ACN Thesis `thesis:acn:ai-reinvention-growth`（`thesis-version:00482bf2…`，human_admission，medium）
  经 `propose/decide_thesis_admission` 人工准入，`current_pointers` 各 1 条。

### 4. S7f weekly brief coordinator 激活

- schedule plan v3 `weekly-brief-plan:us-it-services:v3`（hash `75153819…e9c8a1`）：绑 evidence pack v2 +
  5 overlay + ACN company→thesis 映射；v1/v2 plan 文件保留未激活。
- service config：outbox 增加 owner-only 附件目录
  `state/dalton-core/weekly-brief-attachments`（0700）；新增 `weekly_brief` block（300s interval）；
  thesis-impact `company_thesis_refs` 填入 ACN 映射。
- governance `policy-3` 激活（`effective_from` = 激活时刻，非未来——吸取 S7a 指针事故；回填由 plan 自身
  `effective_from=2026-09-03T11:00Z` 阻止）：完整保留 policy-2 的 auto-start / auto-commit 规则，
  增加 `weekly_brief_auto_publish`（exact plan v3 hash，`max_issues_per_week=1`）。
- 投递前故障演练：live 副本 canary `ok=true`（admission/issue/outbox fresh→duplicate、无外部投递、
  副本 integrity 2 issues / 1 delivery / 1 feedback / 1 admission）。
- 心跳 `weekly_brief: waiting`（reason: no schedule is due after plan effective_from）；
  **首个自动窗口 2026-09-03 07:00 America/New_York（第二期 issue，将绑定 exact prior issue 2026-w35 做 delta）。**

### 5. thesis-impact 首条真实链与两处修复

mapping 激活后 runner 找到 ACN target（plan `research-plan:fc579bbd…` × `thesis:acn:ai-reinvention-growth`）：

- assessment（`profile:gpt-5-6-sol`，真实付费调用）成功，正式记录
  `thesis-impact:f53bd7da…e9a394`，裁决 **insufficient**——单一 SEC 收入增速 Claim 与 thesis 机制的中个位数
  增长预期方向一致，但无法证明 AI-led 程序产生了咨询与后续托管服务需求；这是正确的认识论行为，
  不把单指标过度解释成投资结论。
- 两次 live 事故暴露并修复（均已部署 + 测试覆盖）：
  1. **配置类控制面失败永久卡死**：thesis-impact `broker_auth_key` 指向不存在的
     `~/.openclaw/keys/dalton-model-broker-core.key`（正确为 `~/.openclaw/dalton-model-broker.sock.key`），
     adapter 在任何 provider 调用前 fail closed，但确定性 WorkOrder 身份使修复后无法重跑。修复：
     控制面终态失败可**有界 re-drive**（`MAX_CONTROL_PLANE_REDRIVE=3`）；day-budget 与已付费的模型输出
     违约通过 `control_plane_redriveable=false` 显式排除。
  2. **host 完成失败一次终态**：verifier 首次 Gemini 调用在 OpenClaw host 内
     `HOST_COMPLETION_FAILED`（零 usage、零费用），旧代码 attempt 1 即终态。修复：host 完成失败进入既有
     有界重试（attempt cap 3），耗尽后同样可 re-drive。
- verifier（`profile:gemini-3-7-flash`）当前仍在 host 侧持续失败（OpenClaw 运行时内 Gemini 路径问题，
  非 Dalton 控制面）；runner 按有界重试自愈，失败调用零费用，状态在心跳与日志可见。

## live 现状（2026-08-27 13:1x UTC）

- `deploy/macos/health.sh`：`ok=true / running`；agenda delivered、outbox ready、backup ready、
  weekly_brief waiting；writer/controller/control/thesis-impact 四个 LaunchAgent 均为新代码。
- 6 Claim / 6 Evidence / 2 ThesisVersion（各带 pointer）/ 1 Constitution / 1 thesis-impact assessment /
  evidence pack v2 + 5 overlay / policy-3 active。
- 已知未决：OpenClaw host 的 Gemini 路径故障（影响 thesis-impact verifier 与未来 gemini profile 调用），
  需在 host 侧排查；Dalton 侧已有界重试。

## 下一步

1. host 侧排查 Gemini 路径；verifier 通过后 ACN 链路完成「获取→验证→提交→thesis-impact→brief」闭环。
2. 9/3 首个自动 weekly brief 窗口：验证 delta（对照 w35）、附件投递与 DeliveryReceipt。
3. P8b CompanyResearchView 与结构化知识查询；doctrine writer ops（供 constitution v2 绑定）。
