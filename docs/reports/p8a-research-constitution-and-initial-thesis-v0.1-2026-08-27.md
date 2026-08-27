# P8a Research Constitution 与初始 Thesis v0.1

日期：2026-08-27  
状态：development candidate；代码、专项/邻接回归、隔离 in-memory canary、sdist / wheel 与 wheel-only 安装验证均通过；未写 live Core，未激活任何 production 配置

## 结果

P8a 按 [Phase 8 裁决](phase8-single-topic-autonomous-cognition-loop-v1.0-2026-08-27.md) 落地了「最小 Research Constitution 与初始 Thesis」：

1. 新增版本化 **ResearchConstitution** authority（`research_constitution.py` + `research_constitution_schema.sql`）：
   human-only、append-only、逐 ref 记录版本链，publish 时以 exact `ref+hash` 绑定当时的
   MandateVersion、Driver Pack 版本、active GovernancePolicyVersion、可选 DoctrinePackVersion 与
   可选 Weekly Brief schedule plan（file-contract 绑定），并只补这些对象无法表达的研究方法。
2. writer 新增 4 个 ops：`publish_research_constitution`（human-governed）、
   `get_research_constitution` / `get_active_research_constitution` / `research_constitution_report`（读）。
   现有 `propose_thesis_admission` / `decide_thesis_admission` 不变，行业 Thesis 复用同一条人工准入路径。
3. 新增 `deploy/phase1/weekly-brief-schedule-us-it-services-v2.json`：唯一变化是
   `company_thesis_refs = {company:sec-cik:0001467373 → thesis:acn:ai-reinvention-growth}`，
   plan hash `da9406d4ca106081047bcb24b1482f2b6c85cae28b748ac24871073a45a4e6dc`；v1 文件原样保留。
4. 新增 `deploy/phase8/p8a-us-it-services-bootstrap-v1.json`（闭合 manifest）与
   `scripts/run_p8a_constitution_bootstrap_canary.py`（隔离 in-memory canary）。

## Constitution 合同（ResearchConstitutionVersion 0.1）

```text
{schema_version 0.1, id, created_at, constitution_ref, version, prior_version_ref,
 industry_ref, title,
 bindings: {
   mandate_version {ref,hash},            # SQL 重读 + hash + scope 覆盖 industry + pointer active + 生效窗口
   driver_pack_version {ref,hash},        # SQL 重读 + hash + industry 一致 + pack pointer active
   governance_policy_version {ref,hash},  # SQL 重读 + hash + governance pointer(=1) 指向它
   doctrine_pack_version {ref,hash}|null, # 可选；SQL 重读 + hash + 必须是该 ref 链最新版本
   weekly_brief_plan {ref,hash}|null      # 可选；file-contract 绑定（WeeklyBriefSchedulePlan.content_hash）
 },
 method: {
   question_admission[], causal_chain[],
   source_standards {hierarchy[], conflict_adjudication[], minimum_independent_sources int>=1},
   falsification {required_falsifier_searches[], alternative_explanations[]},
   materiality[],
   lifecycle {continue_when[], refresh_when[], stop_when[], escalate_when[]},
   output_rubric {criteria[], good_samples[], bad_samples[]}
 },
 actor_ref human:, content_hash}
```

发布校验与既有 authority 同纪律：closed shape、human-only actor、版本链必须接最新、version id 唯一、
幂等 key 重放返回原结果（`duplicate`）；SQL 侧 `research_constitution_versions` / pointer /
idempotency 由 `dalton_research_constitution_authorized()` guard + no-update/no-delete 触发器保护。
读回（`constitution` / `active_constitution`）重解析 canonical record、复算 hash 并逐列比对，
漂移返回 conflict。绑定对象本身都是不可变版本，activeness 只在 publish 时检查；宪法冻结 exact
版本，不冻结 liveness——之后 mandate / policy 换版不会追溯失效，但也不会自动跟随。

`method` 七个区域对应 Phase 8 报告列出的缺口：值得研究的问题与信息增益判断、
US IT Services 需求的 driver / KPI / 因果链、来源等级与冲突裁决（`minimum_independent_sources=1`
与当前 live 强制行为一致：SEC 数值单源自动提交、transcript 语义候选必须人工接受）、
必做的 falsifier 搜索与替代解释、量级→盈利→估值→市场预期的映射要求、
continue / refresh / stop / escalate 人工条件、以及好坏研究产物的冻结样本与 rubric
（good sample 指向首期 live issue `2026-w35`，bad sample 指向 owner 的 `revise` 反馈）。

## 初始 Thesis 与 company→thesis mapping

- **行业 Thesis**：`thesis:us-it-services:demand-bottoming` 以 `company_ref == industry_ref`
  （主体即行业自身）走既有 `propose_thesis_admission → decide_thesis_admission` 人工准入，
  写成 ThesisVersion 0.2 / `authority_kind=human_admission`。驱动包为此追加 v2
  （`driver-pack-version:us-it-services:2`，prior=v1，新增 `template:industry-demand-bottoming`
  行业模板，v1 模板原样保留）。weekly brief 的所有权 join 按 candidate 的 `company_ref` 匹配，
  行业 Thesis 的主体是 industry ref，不会与公司 binding 冲突。
- **ACN Thesis**：`thesis:acn:ai-reinvention-growth` 沿用 S7 bootstrap 内容，绑定 pack v2 准入；
  其 candidate `company_ref` 即 weekly brief `_thesis_bindings` 要求的公司所有权证明。
- **company→thesis mapping** 机制不变（config，不是新 authority）：weekly brief schedule plan v2、
  `publish_weekly_brief` 参数与 thesis-impact production config 共用 `company_thesis_refs`。
  本切片把它从 `{}` 变为 ACN 单条候选配置；激活属于 owner gate。

## 隔离 canary

`scripts/run_p8a_constitution_bootstrap_canary.py` 在一个 in-memory Core 上按 manifest 顺序执行：
mandate → driver pack v1 → v2 → doctrine pack → constitution（绑 mandate / pack v2 / policy /
doctrine / v2 plan hash）→ 行业 Thesis propose/decide → ACN Thesis propose/decide → 映射验证。

2026-08-27 结果（`status=passed`，`paid_model_calls=0`）：

- constitution 发布 `fresh`、同参数重放 `duplicate`，pointer/report 各 1；
- driver pack 链 v1→v2，行业与 ACN Thesis 各 1 条 `human_admission` ThesisVersion，
  重放均 `duplicate`；`thesis_versions=2`、`current_pointers=2`；
- weekly brief production 读路径 `_thesis_bindings`：映射 ACN → `current`
  （thesis_version_ref 与准入结果一致），未映射公司 → `insufficient`；
- `PRAGMA integrity_check=ok`。

## 验收

- 专项 / 邻接回归（constitution authority + 部署工件 10、writer ops 3、weekly brief 6、coordinator、
  coverage admission、industry research、writer service、governance CLI、packaging）：62 / 62；
- 全仓 `unittest discover`：940 / 940（本切片新增 14 个测试）；
- `compileall`、`git diff --check`：通过；
- sdist / wheel 构建通过，wheel 含 `research_constitution.py` 与 `research_constitution_schema.sql`；
  干净 venv wheel-only 安装后 schema 资源可加载，v2 plan 从安装包解析出相同 hash
  `da9406d4…a4e6dc`。

## 尚未完成（owner gates）

- **未写 live Core**。live 激活需要 owner 依次：创建 P8a mandate、注册 driver pack v2
  （live 现为 v1）、（可选）发布 doctrine pack、`dalton-gov` 发布 constitution、
  propose/decide 行业与 ACN Thesis，并裁决 schedule plan v2 是否替换 v1（替换即更换
  coordinator 绑定 plan hash，与 S7f activation 是同一个 gate）。
- 既有 live writer token 文件中的 `coverage-governance` principal 不会自动获得新 op；
  `dalton-gov` 的 ephemeral principal 每次按运行时集合生成，可立即调用。
- thesis-impact production config 的 `company_thesis_refs` 仍为 `{}`；ACN Thesis 入 live 后才有
  非空映射可配。低影响 assessment 自动运行、重大 Thesis 变化人工批准的边界未变。
- Phase 7 剩余门槛（DXC 第 5 家 SEC issuer、Weekly Brief coordinator live activation）仍各自保留
  exact owner gate，本切片未触碰。
- Constitution 目前没有消费者强制读取（P8b 的 CompanyResearchView 与 P8c 的 Planner 接线才会读它）；
  本切片先建立 authority 与准入链。

## 下一步

1. P8b：可重建 CompanyResearchView 与结构化知识查询（当前 Thesis、有效/contested/superseded
   Claim、open questions、falsifier、freshness、最近 issue 与研究停点）。
2. live 侧在 owner 批准后按上节顺序激活 P8a 产物；DXC lane 与 coordinator activation 不变。
