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

### 签署材料准备

`build_mission_v2_params.py` 新增 `--add-checkpoint`，按封闭词表校验、追加去重、保持原 mission 对象不变；11 项集成测试 / 1.286s 通过，包含真实 authority 发布生成参数。owner runbook 同步删除手改 JSON 步骤。接下来从当前 live 只读生成包含 11 个 may_write 与 3 个 checkpoint 的具体 params，并在副本上验证后交给 owner。

### 失败条目替换语义

新增 `LaneFailureBudget.retire` 和可回放的 `superseded` 事件：输入或权限版本替换时，只撤销对应旧条目，不将共享外部依赖误报为恢复，也不会释放其他公司的等待队列。覆盖重启回放、权限与内容拒绝撤销、同依赖多公司隔离；68 个 focused tests 通过（7.007s）。后续由协调器按实体范围接入。


### 第二批集成与滚动并行

- F14 A/B/C 初版：`0c53299` / `3081606` / `a31433e`；A/B 交叉修复 `309637f`（299 focused）。共享 superseded API `94259c9`。
- HK acquisition/cache：`17a97cf` + `e6b7eda`；118 focused 与 6 cache hardening。新增治理提案 deliberately unseeded，原四 operation 不变。详见 F13 报告。
- ZeroBase / Reflection / research-task 交叉修复：`a2d0178`；124 focused，包括真实 Core/CLI 每公司内容拒绝与重启恢复。内容拒绝不再被成功退出码吞掉，company subset 真正传到 child。
- W5 market-proxy 初版：`250e428`；364 focused。审查发现 mapping identity/同刻最新版本、派生权限、逐 mapping 故障隔离和 argv 部署入口仍需补，已交原作者先修再做成本侧模板。不能把定向通过视为最终完成。
- 另两条 Sol 线持续进行 Crowd source 日期/权限恢复和 HK closed-week 判断。保留 3 个并行槽滚动运行。
- mission v14 本地签署包已从 live v13 只读生成并在临时副本真实发布验证。包含 11 may_write / 3 checkpoints，原 universe、预算、source_plan、bindings 保持；尚未签署，待完整验证部署批次与 pinned 版本确认后交 owner。

Next step：收齐 crowd 与 market-proxy 审查修复后冻结一个可部署版本，主线全量、当前 live 只读副本复演、wheel/安装参数验收，随后 commit/push 并提交具体 owner 部署/签署步骤；后续独立开发继续在 worktree 推进。

### 部署前生命周期修复

安装脚本原先先升级 live venv 再停止服务，且 drain 只读最早五类 ticket。现改为控制器停止并确认 → 当前源码的纯标准库 drain（发现所有两层 lane ticket）→ writer 停止并确认 → 升级运行时；drain 超时中止，不继续打断在飞任务。安装 extras 同时补已有 HK XLS 读取依赖 `hk-filings`。51 项 service/drain 测试通过（1.542s），zsh 语法通过；未运行安装脚本，live 未变。


### 首个持续开发验收点

`e7e06bb` 完整主线 6,114 项 / 495.678s 通过（1 skip）；当前 live 只读副本 12 步复演、67 schemas/35 lanes/38 entries/0 escaped；wheel 428 个 Python/SQL 文件匹配。详见 continuous-wave2-integration 报告。对应代码和文档本次 push，未部署。三个 Sol 槽继续做后续切片交叉修复，不能将本检查点标为全部开发完成。
