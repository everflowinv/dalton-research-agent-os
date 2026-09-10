# 持续开发：失败恢复、HK 共享采集与后续切片

日期：2026-09-10；基线 `4c28816`，工作区干净，main/origin 同步。

## 执行授权与完成口径

Owner 最新要求持续开发直到完成，最大化 GPT-5.6 Sol 并行。运行能力为四个并发 agent（含主代理）；因此保持三个代码子代理，完成一片继续下一片。沿用“聚焦通过后 commit、主线全量通过后 push”；签署/部署须给 owner 已准备好的具体材料。

## 当前批次

F14 的真实范围是 13 个协调器，按 `resume-failure-ledger-next-2026-09-10.md` 三组分给独立新 worktree。验收重点：quiet/input unchanged 不冒充失败；依赖失败不耗重试预算并持久化；业务输入没变、外部依赖恢复时也能继续；权限单列，配置/mission/policy 变化可恢复；内容拒绝仅终态当前输入。不能用“更新业务 hash 后可跑”替代真正依赖恢复。

主代理审查公共失败账本/恢复接口、准备 HK 全市场日 acquisition 与公司视图合同，负责冲突集成、跨分支回归与签署材料。其他 agent 不改共享 helper，需求先统一。

## 持续队列

1. F14 三组接线与集成验收。
2. F13 一次全市场日采集 / invocation / artifact + 各公司视图；并发、缓存损坏、governance 变化与跨公司隔离均须测试。
3. HK 周级判断的调度与增量归组，保留逐日原始事件。
4. W5 market-proxy claim 存储与生产者、分类成本模板、10-Q Item 5 trading arrangements。
5. 具体运行激活与签署材料，owner 执行后验收五家公司 dossier / DebateMap / judgement 产物。
6. 其余蓝图项目按现有优先级补完；周报投递与 Excel 导出仍在最后，外部发送必须单独有明确授权。

## 进展

三个 Sol 子代理已启动。签署/部署未执行，live 未修改。后续交付、发现、测试和 next step 随实际结果追加。

### 公共账本恢复修复

主代理发现 `LaneFailureBudget.replay()` 在读取 `dependency_ok` 时调用会写账本的 `dependency_answered()`；每次 writer 重启会重写恢复历史。现拆出只更新内存的恢复函数，replay 不再产生新事件，并支持单项 `resumed` 历史。另修同时间戳按 event hash 排序造成“先恢复后失败”：读账本改为 timestamp + SQLite append rowid，保持同刻的真实写入顺序。

回归覆盖三个重启后账本逐行不变、八组同刻 park/recovery、现有 permission / cockpit / extraction；76 项 / 7.327s，OK。公开调用 API 不变，三条子代理线继续并行。
