# M1 财务建模引擎与 AE Probe 双线 v0.1

日期：2026-09-01  
状态：均已部署 live 并完成首次真实运行；owner 同日裁决「M1 与 AlphaEngine probe 并行，AE 24h/30 次上限」

## M1：ForecastLine authority + 首个 ACN 财务模型

按 vision review 的缺口清单补上「建模能力」的第一层：

1. **`model_forecast.py`（+ packaged schema）**：版本化 ForecastLine authority，value_kind 采用
   SPEC 冻结词表（observed / assumption / derived_deterministic / estimate / simulation）。
   `derived_deterministic` 行只能绑定冻结公式合同 `formula:quarterly-growth-extend:1`（语义：
   base actual × (1+g)^k 逐季复利，季节性由假设承担）与 exact 的 scenario / base actual /
   growth assumption 输入版本 hash；非 derived 行 human-only 且必须带 rationale。SQL 侧
   authorized-guard + 不可变触发器；行级 content_hash 闭合校验、版本链接最新、幂等重放。
2. **`extend_growth`**：确定性延展函数——从已准入的 actual（基数）与 assumption（增速，percent
   自动换算）派生 N 个季度预测行，同时经现有 `record_model_run` 写入 model run（input bindings
   绑 exact 版本、公式 ref/hash、outputs 闭合 metric 形状）。全链幂等（seed + result 双记录），
   跨天重放不重复、不重复计费。
3. **writer ops**：human-governed `publish_forecast_line` / `get_forecast_line`，以及
   `extend_growth_forecast`（governance + core）。
4. **首个 live 模型（2026-09-01，`human:lumos` 准入输入）**：ACN base scenario、Q3 FY2026 收入
   actual **USD 18,718.144M**（绑定 live SEC lane claim 的 exact evidence version/hash）、
   增速 assumption 1.15%/季（从已准入 ACN Thesis 的 implied_expectation「中个位数本币年增长」
   ~4.7% 复利换算，rationale 记录推导）。延展产出 4 条 `derived_deterministic` 预测线
   （Q4 FY2026 18,933.40M → Q3 FY2027 19,594.15M）+ model run v1。隔离 canary
   （`scripts/run_m1_acn_model_canary.py`）先在 live 副本验证全链（fresh→duplicate、公式/场景
   绑定、不可变）。
5. 顺带修复一个 live 卡环：driver 收到 LLM coordinator 的 `core_action`（硬控制态已提交 terminate
   提案）时原来会再次确定性提案 → 永远 `duplicate`，v5 循环因此卡了 40+ 个悬空提案；现在直接
   使用 `core_action` 的结果。

## AE：AlphaEngine Tier 1 probe（24h/30 次上限）

1. **`bounded_alphaengine_probe.py`**：owner 上限落在代码里——每次探测先数 Core 中
   AlphaEngine connector invocations 的 trailing 24h 计数，超 30 直接
   `ALPHAENGINE_PROBE_BUDGET_EXCEEDED`（零调用花费）；文档已在 authority 时直接命中
   （`calls_spent=0`，不发网络）；缺失时经 launcher 获取并回验 authority。
2. **launcher 新增 `start_bounded_probe`**：与人工路径同一治理子进程，但 ticket 记录
   `automation:` principal——自动化请求永不伪装成 human；人工 `start` 的 human-only 校验不变。
3. **writer core-only op `bounded_alphaengine_probe`**（探测在 writer 进程内执行，因为获取子进程
   写 Core connector authority）；driver 按 WorkOrder 的 `operation==alphaengine_get_document`
   分流。
4. **live（2026-09-01）**：AE ProbeTemplate `probe-template-version:c966a9a0…`
   （permission_scope `alphaengine_read`，side effect `read:alphaengine-mcp`）与循环 v6
   （五家 SEC + `coverage:transcript:acn`）准入。v6 全自主跑完 6 轮：5 轮 SEC（4 observed +
   DXC source_unavailable，SEC 侧仍未恢复该 key）+ **round 6 AE transcript observed**
   （ACN 8/26 已入库文档直接命中，0 调用花费），终态 `evidence_observed_for_review`。
   当日 AE 调用计数 0/30。

## 验收

新测试：model_forecast 3（确定性/复利/重放、human 与 closed-shape 规则、输入匹配）、
alphaengine probe 5（authority 命中、预算拒绝、窗口过期、automation principal、非 Tier 1 拒绝）；
全仓 **969/969**（三个部署迭代各跑一轮全仓）；canary 通过；CI 绿色。

## 边界与后续

- 季节性：growth-extend 是逐季复利，ACN Q4 偏强 / Q3 偏弱的季节结构由假设吸收；下一版公式
  （yoy-extend：需要 4 个 year-ago actual）待 actuals 积累后冻结。
- 估值仍 fail closed：price/shares/FX/rates/consensus authorities 依旧缺位（M2 需要 owner 解冻
  市场数据 connector）。
- AE probe 目前绑定已知 document ref；search 驱动的新文档发现（search_library probe）留待
  AE-2，预算门已就位。
- thesis implied_expectation → 数值假设的映射目前是人工撰写 rationale 的 judgment 输入；
  「driver→模型行」的自动映射等 M2 与 driver pack 扩展。
