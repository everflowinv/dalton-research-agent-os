# 2026-09-22 环境核实、修复与 Antigravity 复测

状态：候选修复正在验证；部署与最终测试结果见本文末尾（未填写前不代表已上线）。基线源码 `98a1e630`，线上 `f2758484`。

## 核实结果与对原巡检报告的更正

| 原条目 | 核实结果 | 处理 |
|---|---|---|
| P0-1 | legacy 8,188 个原始对象共 991,744,557 字节；达到预留下一次响应所需空间后的 1 GB 上限。容量失败也使行情、一致预期车道挂起。 | 保留原始字节与哈希的可逆压缩、可配置容量、真实子进程错误传播、容量依赖重试与预警。没有按日期删除证据。 |
| P0-2 | **不是同名模型变体选错。** 所列 refs 是同一 profile 的不可变历史版本。Google 两张 providerControls rateCard 于 09-22 00:00 UTC 到期，00:01 的同步撤下 `provider-controlled-verify`，行为本身正确。 | 核对官方价格后续期证明；代码显示过期原因、能力覆盖与同步中的能力损失。不得把过期历史版本重新选为有效模型。 |
| P0-3 | admission 固化的路由策略与当前配置版本不一致，会使未完成研究失效。 | 执行时重验当前模型权威，保留最初 admission 和实际权威的审计联系；能力、模型家族独立性与预算边界继续生效。只为尚未排队的阶段刷新权威，兼容的成功阶段直接复用；已有未完成/失败/恢复授权的旧 Work 不换 ID 重买，缺显式重绑定证据时继续拒绝。 |
| P0-4 | **并非容量失败留下半成品。** 两个当前 review 仍绑定 09-07 的 orphaned 票据；09-09 已有完整 succeeded 票据，并且完整原文仍在 transcript-spool。 | 将明确 failed/orphaned 的票据识别为未完成，使用现有成功抓取定位与校验路径。身份或成功清单不一致继续拒绝，不改历史票据。 |
| P1-1 | retrieval 完成 envelope 只以 proof 定名，在不同 WorkOrder 重入时发生碰撞。 | 身份加入工作及执行尝试，保留幂等性。 |
| P1-2 | 无 WAL/SHM 的冷 WAL 策略库无法通过严格只读入口打开；仅增加重试不能解决。 | 仅对不可变策略记录采用受文件稳定性及无边车检查保护的只读冷快照；一般实时数据库读取约束不变。 |
| P1-3 | 双层路径确实存在。 | 新目录使用单层；既有双层目录自动识别，避免部署后丢失证据引用。 |
| P1-4 | reopen 的 held=5 是本周已经检查过的正常抑制，**并未拖住另两条车道**。一致预期与行情的五家公司是各自因 RawSpoolCapacityError 挂起。DXC 的 unsupported_sentence 是实际语义驳回，另有模型路由不可用，不能直接放过。 | 容量恢复、重启及正确分类解除基础设施挂起；保留事实支撑核验。 |
| P1-5 | earnings 每轮重建 guidance 时按多个章节反复扫描同一个 ClaimIndex，即便全部窗口已 held 也支付这笔 CPU 成本。 | guidance 共用单次操作的 ClaimIndex 快照，不跨 tick 缓存、不隐藏新证据。crowd 车道当前也受容量失败影响。 |

新环境没有研究目标属于预期。本次不复制 legacy 研究目标或放宽研究内容核验。

## 受控核验 rate card 续期边界

原始受控核验验收报告已经注明两张 Google rate card 于 2026-09-22 到期并需复核。2026-09-22 再查 Google 官方资料：Gemini 3.8 Flash 促销价到 2026-12-31 为输入/输出 `$0.75/$3.75`，2027-01-01 起标准价 `$1.50/$7.50`；当前配置用后者预留。Gemini 3.1 Pro Preview 在超过 200k 输入档为 `$4/$18`，与当前配置相同。来源：[Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)、[Google Cloud Agent Platform pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)。

续期工具 `scripts/renew_google_provider_controls.py` 只接受当前两条准确的 Google route/mode/rate-card 形状，只改 `verifiedAt` 与 `expiresAt`，且最长续 31 天。默认 dry run；写入时必须提供刚才审阅的完整配置 SHA-256，写前再次核对避免并发覆盖，并在同目录留下备份。任何价格、model、mode、thinking level、受控 profile 集合或字段形状变化都会拒绝，要求重新审计代码而不是顺手接受。12:47 UTC 已应用至 live 配置，窗口为 `2026-09-22T12:35:00Z` 至 `2026-10-22T12:35:00Z`。回读确认仅两张 rateCard 的四个日期叶字段改变，费率及路由不变，原文件有备份；收据见 evidence 中的 `ratecard-renewal.json`。

## AlphaEngine 与性能证据

- `alphaengine-doc:320000610008414`：旧票据 `dec4938bdc99dcd6cfd8f05b` orphaned，成功票据 `63645586dce129a0d80a2f50`；原文 56,679 字节，SHA-256 校验通过。
- `alphaengine-doc:320000610165860`：旧票据 `1758cc29b9cd31a152671351` orphaned，成功票据 `041022c0353d0b2eec828739`；原文 29,619 字节，SHA-256 校验通过。
- 线上数据库只读对比、DXC guidance：重复快照 19.472 秒，共用快照 2.157 秒；40 条 guidance、38 条 actuals 的结果逐项完全相同。单次测量，不宣称所有 tick 都达到同一耗时。

## Antigravity：可以扩大有限输入用途，截断尚未彻底修复

历史问题有两组记录：

1. Dalton `live-ops-and-research-audit-2026-09-16.md`：debate-map 提示词 p50 29,434 字节，35/112 不少于 30,000；Flash high 在当时记录中 2/207 成功，因此在 `model_profile_bounds.py` 设置了 30,000 的保护，并被修链脚本排出长输入梯队。这是当时的保守运行限制，不能据此推断 CLI 的准确截断位置。
2. 网关仓库 `docs/agy-long-context-2026-09-10.md`：agy 1.2.0 的六次探针已证实模型实际接收的用户消息在约 192,000 UTF-8 字节截断，仍返回 SUCCESS/0。网关因此按完整拼装后的 **字节数** 设置 190,000 上限。

本机现为 agy **1.2.8**。随二进制提供的 changelog 描述的是压缩会话时如何分配用户请求与摘要预算，并未承诺移除这条单消息边界。本次使用随机首、中、尾标记的合成输入，不包含研究数据、不调用工具：

| 路径 / 模型 | 完整输入字节 | effort | 结果 |
|---|---:|---|---|
| 本机生产网关 / Gemini 3.8 Flash | 60,280 | low | 3/3 标记完整 |
| 本机生产网关 / Gemini 3.8 Flash | 175,282 | low | 3/3 标记完整 |
| 本机生产网关 / Gemini 3.8 Flash | 175,282 | high | 3/3 标记完整 |
| 直接 stream-json CLI / Gemini 3.8 Flash | 269,100 | low | 5/6 标记，末尾缺失，仍 SUCCESS / 0 |

最后一例进一步核验本地会话的 `gen_metadata`，实际模型请求只保留原始输入前 **191,985 字节**，含 `<truncated 77520 bytes>`，尾部标记不存在。不是仅凭模型漏读来猜测截断。stderr 为空。只提交脱敏摘要，不提交完整系统提示词、账户或会话数据库。

因此保留网关 190,000 字节保护，将 Dalton 已实测的 Flash low/high 输入保护提高到 **170,000 字节**，给宿主封装留余量。部分旧调用方交给路由器的是 token 估算而非字节数；适配器因此在本地 broker 发送前再次计算实际提示词的 UTF-8 字节数。170,001 字节即拒绝，169,998 字节仍可发送，且只作用于这两个实测档案。档案声明的更小限制仍优先，超出不付费。修链脚本不再仅因旧的 30 KB 观察值删除这些模型。没有替用户重排默认链。

可重新尝试：中等长度的语言检查、资料摘要、抽取、研究草稿与现有梯队回退。此处只认证传输完整性，不认证所有研究任务的 schema、事实保真或输出长度质量；实际 WorkOrder、费用和独立核验约束仍然适用。尤其高推理输出曾超过 WorkOrder 输出预算，输入截断修复不能解决它。超 190 KB 的整份资料、整段长历史仍须分块；Antigravity 不因这次探针获得 `provider-controlled-verify`。

实测摘要：[evidence/environment-repair-2026-09-22](evidence/environment-repair-2026-09-22/)。

## 验证与部署

- 共用 guidance 快照相关 46 项、AlphaEngine 票据恢复 21 项、服务配置及重新安装 78 项专项测试通过。
- Hermetic research replay canary 通过，全程零模型、零网络调用。
- 从线上对象复制出的 80 个样本完成 archive → 读出并校验 SHA → restore，逐字节完全一致；归档后 4,095,420 → 949,770 字节，原线上对象未被该演练修改。
- 首轮全量 9,666 项跑完；执行期间仍有代码变更，出现 4 项失败（HKEX 三项、Cockpit 断言一项）。HKEX 最新代码专项复跑已通过，最终冻结后的结果另行补记，不将本轮计为通过。
- 服务配置新增 `raw_spool.max_total_bytes`、`raw_spool.archive_after_seconds`，重新生成 LaunchAgent 时仍保留设置。压缩默认关闭；必须等全部旧 reader 停止后再启用，回滚旧版前应先执行 `raw_spool_maintenance restore`。

- 实际 UTF-8 适配器输入保护与模型选择相关 184 项通过；170,001 字节且路由估算仅 10 的多字节输入在发送前被拒，169,998 字节通过。后续专项复跑 11 项通过。
- 13:05 UTC 前后，三个环境的最新 Google profile 均已恢复 `provider-controlled-verify`，续期通过既有 writer 同步生效。
- 容量预留仍有既有的多进程限制：同时打开响应流的预留量按进程记账，因此 4 GB 是运行高水位，不能当成文件系统严格配额。无证据删除，磁盘仍有充足空间。

部署与最终冻结验证待补记。
