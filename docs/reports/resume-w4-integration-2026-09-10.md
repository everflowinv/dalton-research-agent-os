# W4 恢复集成与修复（2026-09-10）

本轮 owner 要求继续开发、并行使用 GPT-5.6 Sol、及时记录与 commit/push。基线 main `694471c`，fetch 后领先 origin/main 11 个提交且工作区干净；保留原有提交与所有旧 worktree。三个新 worktree 承接预测 F1–F3、insider F6–F8、ZeroBaseReview F16–F17；主 agent 审读并集成。

## 已集成

按 hkex → insider → framework → failure-classes → zero-base → rehearsal-2 顺序合并六条交付分支，每次做 import 检查。保留 model-selection、economic-invariants、operations 四格、judgement outcome reader 的共享代码。未修改 live 或治理决定。

## 主 agent 修复

- **F11**：聚焦 486 项测试首次暴露 2 failure / 1 error：`research_event.PAYLOAD_FIELDS` 有两个不同的 `buyback_disclosure` 键，后者静默覆盖前者，港股事件被拒。现统一为单一封闭契约，以 US 字段名 `accession` / `filing_date` / `shares_purchased` / `average_price_paid` 为公共字段；HK 独有价格范围、成交金额、股份占比保留。`cumulative_shares` 与 `cumulative_basis` 成对出现，三种期间封闭；HK 写 `since_mandate`，US 未披露则两项为 null。`cluster_key` 为 ISO 周 + accession / 申报日。增加 JSON schema 与 AST 检查，防止字典重复键再次静默吞掉一条生产路径。
- **F12**：价格权威允许精确四位 `.HK` ticker，其他数字开头格式仍拒绝；模拟 `0700.HK` 一日行情通过 adapter 与真实 authority。判断层把 HK 回购账本行还原为派生上下文，均价以已披露总额 / 股数计算，引用 price version；币种不同不比较。缺价格时如实保留 unavailable。
- **F9**：分类先起草，同次 run 的需求段使用刚起草的分类；调度优先级在 new-ref 计数之前，避免只换 UNITS 却仍按证据数排到后面。持久化档案分节顺序和旧版本不变。
- **F18**：`--set-source-status` 对 inventory 中已知但 mission 缺行的来源追加 source_plan 行，不再要求手改 JSON。闭合行契约不加字段，`role` 写 `created_by=set-source-status` 及 connector 名；五个来源实测下一版可由 CoverageMissionAuthority 发布。原 mission 对象不变，未知来源拒绝，重复执行不重复加行。
- **F19**：ask v0.2 schema 的 enabled 改 boolean/default false；同时修正 max_cost_units/max_rounds 的 const 0（仅改 enabled 仍无法描述合法开启状态），开启必须正预算、正轮数，关闭两项为 0。运行时原有授权规则不变。

## 当前验证

主 agent 聚焦回归（本轮契约、档案、HK 两条生产路径、US 回购、contracts）：

```
Ran 283 tests in 13.782s
OK
```

connector inventory `--check` 通过，`git diff --check` 通过。此前两次聚焦测试命令/新增测试存在测试 harness 名称与字段名错误，已修正并重跑；不计作产品缺陷。全量与最终复演结果待集成全部修复后补记。当前没有宣称已 push 或部署。

## Next step

1. 集成并审查三个子代理修复；重点复核多份 filing 的分部行重复累计、ZeroBase verifier 是否能看到引用正文。
2. F15 待授权失败分类与 F13 港股日缓存在独立后续 worktree 处理；F14 十六条 lane 接账本为后续覆盖扩展，不以旧报告的“已分类”冒充“已接线”。
3. 主线全量绿后 push；对合并 main 的隔离 live 副本重跑 fail-closed 复演，记录实际 schema/lane 数与下一步。
4. D1–D9 继续留为明确待裁决，W5 market-proxy 生产者与成本模板不在本轮扩项。

## 第二次集成审查补充

预测与 ZeroBase 首批修复合入后，聚焦 333 项通过（50.030s）。首轮隔离复演针对 main `1ff80e0`，输入为已有 `/tmp/dalton-rehearsal2-20260910T073000Z/live` 快照，显式 `--source-root` 指向原 Dalton 根，输出在 `/tmp/dalton-resume-rehearsal-20260910`：66/66 schemas、35 lanes、38 tick entries、0 escaped，11 个 may_write 与 3 个 checkpoint 缺授权，5/11 lane switches 在盘；没有部署。该结果只验证快照兼容与接线，不能冒充最终版全量或 live 产物验收。

额外发现并修复：ZeroBase 的兄弟候选表虽接入 ThesisRevisionAuthority，但 cockpit 仍只读原表。现两个来源统一展示，同一裁决入口过滤终态，四问 narrative 可见；周反思的 judgement_outcomes 也接 HTML。首次模型发布被拒、公司还没有旧模型时，卡片同样显示拒绝理由。ask 的 route-decision 输出契约同步允许实际返回的 boolean availability。

复演 harness 原本将 escaped lane 仅计入 findings、仍返回通过；现在任何 escaped lane 都使 tick 步骤失败，并加回归测试。这是发现即变成机器检查，不依赖操作者阅读告警。

验证：cockpit/回归 115 项（36.215s）通过；回归/复演 108 项（1.520s）通过；内嵌 JavaScript `node --check` 通过。

## 最终代码集成（`48cc315`）

三条 GPT-5.6 Sol 修复分支及交叉审查已集成：F1–F3、F6–F9、F11–F12、F15–F19 完成；F5 / F5b / F5c 为共享文件集成完成，F4 无代码修复。F10 仍属于 W5，F13 / F14 未实现，不将可行性审查写成已交付功能。

- F2 补修同一比较期来自多份 filing 时的重复分部累计，按同一份最新 filing 选完整的合并值与分部组。
- F6 保留 v1 原始字节，新增 v2；F7 月份行保留独立事件、共享一次 producer/verifier 判断，两个 prompt 都包含所有月份；别名费用为 0 并指回 primary，事后评分排除这些别名，避免同一决定重复计分。F8 薄覆盖的未匹配出售计划为 unknown。
- F15 document extraction 的治理拒绝进入单独待授权账本与 cockpit 区域，重启后仍不重复调用；mission pointer、governance policy pointer 或相关配置文件改变时自动解除 hold 并重新检查。没有将其他十六条 lane 冒充已接失败账本。
- F16 以最近复盘后 30 天为周期，未复盘的新财报可提前触发并重置周期；F17 独立家族 verifier 看到在档 thesis/debate/claim 正文，拒绝错误引用、超大上下文或验证失败。producer/verifier 必须成对配置。
- 主线补回归 277 项（10.077s）与最后的评分/权限恢复 57 项（0.346s）通过。完整主线测试已在 `48cc315` 冻结代码后启动；最终结果在下节记录。

## 最终隔离复演

`48cc315` 使用同一个已有 `20260910T073000Z` live 快照重跑，输出 `/tmp/dalton-resume-final-rehearsal-20260910`，命令与首轮相同但换了独立 temp-root/report。全部 12 步通过：23 个文件 / 818 MB 副本、66/66 schemas、27 seeded（15 already present / 12 gated）、35 registered lanes、38 tick entries、0 escaped。tick 3.9s，仅为离线单轮样本，不代表线上吞吐验收。

快照仍缺 11 个 may_write、3 个 checkpoint，只有 5/11 lane switches；ZeroBase、judgement、dossier 等仍 unconfigured。旧 verifier pin 指向本次 catalog sync 标为 retired 的 `profile:gemini-3-7-flash`，部署前需按现有模型路由机制修正。完整 findings 保存在上述临时报告。本次只用复制的 SQLite / 配置、模型 stub 与拒绝外网 transport，没有启动/替换 live LaunchAgent，也没有实际发布五家公司研究产物。

## 下一里程碑

1. **运行激活与产品验收优先**：按更新后的 owner runbook，用当前 live 新快照复演，核对 verifier pin、mission grants/checkpoints、producer/verifier switches 和 tracking policy v2；部署后验收五家公司首版 dossier、DebateMap 与一轮判断。历史快照通过不能代替当前 live 核对。
2. **F14**：逐条迁移仍未接公共失败账本的 16 条 lane，区分输入未变的正常等待与真正依赖失败；依赖恢复回归随每条 lane 交付。
3. **F13**：先定义全市场日 acquisition + 各公司 derived view 的受治理身份，再实现一次 invocation/artifact 缓存；详见 `resume-hk-cache-feasibility-2026-09-10.md`。当前 ticker 是治理请求身份的一部分，不能仅从 hash 中删除来假装去重。
4. **HK 周分组新增待办**：独立审查指出 payload 的 ISO周+申报日标识不等于周级调度，HK 日披露仍逐事件判断。F11 按原清单保留该标识；下一轮需明确已判断日的新增记录如何增量归组，再实现稳定周级推理单元。简单按周合并未判断行也不能保证一周只调用一次，不以本轮 US 月份分组冒充已解决。
5. **W5 / D1–D9**：market-proxy producer 与成本侧模板另开切片；HK universe、数字权威边界、60日慢背离等既有待裁决保持显式。周报投递与 Excel 导出仍后排。
