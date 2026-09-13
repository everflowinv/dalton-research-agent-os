# Dalton 恢复开发：R21 writer 权限追加的部署与回滚

日期：2026-09-13。开发起点 `b1f367ab73de2727ecf341a59ca3242adf1b7e97`。

## 恢复的真实状态

R21 冻结 `8801dadd688705c0f6e21f20111697cb7ef0ad80` 全量已完成：7,854 tests、0 failures、0 errors、1 skip，768.582219s。原始 receipt `/private/tmp/dalton-foundation-r21-full-suite-receipt.json`；SHA-256 `f8c1ab032ee9eb2a1bf1d30a5cb8816c9282e7a142d24add425fc39bca65ae1a`。608 runtime files / 3 JS 与 14-step、42-tick、0-escape 副本演练也通过。

真实安装后的保护状态检查失败：writer token 的 core operations 172→173，唯一新增 `settle_company_model_spec`，token 和其他字段不变。原发布流程把这个 bootstrap 源码变化当作未经审阅的 owner-state 改动，回滚重复拒绝。后续恢复记录核验了 R21 实际程序和 17 份配置并恢复服务；它没有替代 45-sample/660-second 健康与最终发布步骤。正式 current-release 仍指向 R20。

## 本批代码

三个 GPT-5.6 Sol 子代理在新 worktree 并行，分别承担财务审查与配置契约、writer 证明与独立审查、R21 证据恢复与副本演练；主代理完成集成、安装/回滚路径和跨模块验证。

- 由确切、干净的 predecessor/successor 源码提取 core operations，要求严格追加；证明绑定前后文件字节、两份源码 commit 和 operation hash。
- 新 schema 0.5 只在明确提交 writer 追加材料时启用。配置准备与 apply 不提前写 token；bootstrap 是实际变更执行者。旧 schema 0.1–0.4 保留。
- 副本演练对真实 scratch bootstrap 的前后 token 文件逐字比较，再将结果绑定给安装验收；辅助函数通过不代替真实入口接线。
- 安装前要求精确旧文件和 0600 权限；安装后要求精确预测文件。其他保护状态和原始 rollback snapshot 都保持验证。
- 回滚在恢复程序/配置后、重启前恢复原 token 文件；只接受精确 before/after。恢复使用独占文件名发布，竞争写入保留并拒绝覆盖，被移出的文件保留在 rollback evidence 中。原始 initial-state receipt 不被改写。
- 原累计 filing 配对、结构化负值增长率两项修复独立审查通过，没有额外更改财务口径或放宽数字校验。

## 验证

本批完整冻结验收结果待本节追加。已完成的局部检查：财务 284 项、调度/结算/预测联合 314 项、部署契约与 rollback 123 项。它们是不同或重叠集合，不相加为总测试数。首轮 root 测试命令用了两个不存在的模块名，产生 loader errors；修正为实际模块名后 314 项通过，失败调用不计作通过。

## 后续

完成本批同版全量、wheel 对照和新的副本演练后，才能准备下一份具体部署接受材料。当前机器曾恢复到 R21 程序、发布指针仍为 R20，下一份发布材料必须明确核对实际安装与历史发布的差异，不能把旧失败包重标为成功，也不能在已经具有新 operation 的 token 上伪造一次追加。需要完整新的部署/健康/最终发布链。

本轮只开发和离线验证，没有修改 live token、配置、数据库、服务或发布指针，没有发起模型请求。
