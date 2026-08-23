# US IT Services / ACN 初始覆盖准入 v1

## 结论

开发候选已经把首个覆盖行业从化工切换为 US IT Services，并以 Accenture（ACN）作为第一家公司。Core 没有写入
公司专属逻辑；行业差异放在 versioned Driver Pack，准入仍走通用 mandate、candidate、human decision 和
ThesisVersion authority。

本切片没有改 live Core。production `company_thesis_refs` 仍为空，thesis-impact runner 继续零调用 idle。

## 已实现

- `ThesisVersion` v0.2：`low / medium / high` ordinal confidence；v0.1 float 只读兼容。
- `IndustryDriverPackVersion`：不可变版本、active pointer、closed metrics/drivers/templates 合同。
- `ThesisAdmissionCandidate`：绑定 exact company、industry、mandate/hash、driver pack/hash、template、drivers、
  falsifiers 和 thesis content/hash。
- `ThesisAdmissionDecision`：只接受 `admit / reject`，记录 reviewer、rationale、candidate/hash 和 resulting version。
- writer human gate：三项写操作只允许认证 `human:` principal；worker、verifier、普通 automation 和 core automation
  不能代写 actor。
- coverage thesis reservation：candidate 建立后，旧 model-verification commit 对同一 `thesis_ref` fail closed。
- 迁移：旧 thesis 表可升级到 authority union；旧版本 JSON 和引用保持不变。

## ACN Driver Pack v1

Driver Pack 冻结四类 driver：

- bookings mix and conversion；
- AI and reinvention demand；
- delivery economics；
- cash conversion and capital return。

首条 candidate 使用前三类 driver。判断是：大型 AI/reinvention 项目和 managed services 能抵消 discretionary
consulting 偏弱，使 Accenture 在不牺牲 operating margin 的情况下维持 mid-single-digit local-currency growth。
初始 confidence 为 `medium`，不是校准概率。

主要 falsifier：AI bookings 不转成收入、consulting 弱势超过 managed services 的缓冲、增长必须依靠明显 margin
牺牲。初始版本没有自动导入历史 Claim；`claim_refs=[]`，后续正式 SEC/earnings evidence 只进入 assessment，不会
自动改 thesis。

## 隔离验收

`scripts/run_isolated_acn_admission_canary.py` 在 in-memory Core 完成：

- exact mapping：`company:sec-cik:0001467373 → thesis:acn:ai-reinvention-growth`；
- `mapping_count=1`；独立 target-discovery 测试覆盖 mapping 启用后 1 个 closed ACN plan、移除后 0 个 target；
- 生成 1 条 `human_admission` ThesisVersion，confidence=`medium`；
- 重放 decision 返回 `duplicate`，ThesisVersion 总数仍为 1；
- SQLite `integrity_check=ok`；
- paid model calls=0。

专项测试还覆盖：非 human principal 拒绝、unknown driver/template/falsifier 拒绝、mandate/pack hash 和 active pointer
重检、第二个 decision 拒绝、初始 admission 不可冒充 revision、coverage candidate 建立后自动 commit 拒绝、旧表
结构迁移，以及 writer RPC 权限矩阵。

完整 Python 回归为 658/658 通过，耗时 2543.537 秒。Python 3.13 隔离构建成功生成 sdist 和 universal wheel；
新增四份 JSON Schema 均进入 wheel 的 `share/dalton-core/contracts`，`coverage_admission.py` 进入 package。

## 尚未开闸

- 没有把 ACN mapping 写入 production config；
- 没有向 live Core 注册 mandate、Driver Pack、candidate 或 decision；
- 没有运行真实 GPT-5.6 Sol / Gemini 3.7 Flash assessment；
- 没有启用 coverage thesis revision。

下一道 gate 是人工复核 manifest 中的 driver 定义、candidate statement 和 falsifiers。复核通过后，才能通过
ephemeral governance principal 把 exact 内容写进 live Core；随后先跑一条 ACN SEC Claim 的有限真实 assessment，
再决定是否保留 mapping。
