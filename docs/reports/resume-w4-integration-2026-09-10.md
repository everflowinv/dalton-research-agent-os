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
