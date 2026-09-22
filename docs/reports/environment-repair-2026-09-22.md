# 2026-09-22 环境核实、修复与 Antigravity 复测

状态：已上线。最终发布 `30a9e0d4`（源码 `d76d7878`），9,708 项全量测试通过（4 skipped）、实际发布包 171 项通过。全部 11 个服务和指针一致，三个环境心跳正常；历史队列已重开并完成首条验收，其余继续按原调度处理。第一批 `f3ee7e46` 已恢复抓取与产出；基线源码 `98a1e630`，原线上 `f2758484`。

## 核实结果与对原巡检报告的更正

| 原条目 | 核实结果 | 处理 |
|---|---|---|
| P0-1 | legacy 8,188 个原始对象共 991,744,557 字节；达到预留下一次响应所需空间后的 1 GB 上限。容量失败也使行情、一致预期车道挂起。 | 保留原始字节与哈希的可逆压缩、可配置容量、真实子进程错误传播、容量依赖重试与预警。没有按日期删除证据。 |
| P0-2 | **不是同名模型变体选错。** 所列 refs 是同一 profile 的不可变历史版本。Google 两张 providerControls rateCard 于 09-22 00:00 UTC 到期，00:01 的同步撤下 `provider-controlled-verify`，行为本身正确。 | 核对官方价格后续期证明；代码显示过期原因、能力覆盖与同步中的能力损失。不得把过期历史版本重新选为有效模型。 |
| P0-3 | admission 固化的路由策略与当前配置版本不一致，会使未完成研究失效。 | 执行时重验当前模型权威，保留最初 admission 和实际权威的审计联系；能力、模型家族独立性与预算边界继续生效。尚未排队的阶段刷新权威，成功阶段按精确历史模板与执行证明复用。历史失败回到原有受限恢复入口；第二批以追加式记录迁移精确的未使用恢复授权，预算逐项不扩大、原恢复次数不重置，数据库原子约束阻止旧、新工作同时领取。 |
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
| 本机生产网关 / Gemini 3.8 Flash | 60,280 | low | 单次请求中 3/3 抽样标记可达 |
| 本机生产网关 / Gemini 3.8 Flash | 175,282 | low | 单次请求中 3/3 抽样标记可达 |
| 本机生产网关 / Gemini 3.8 Flash | 175,282 | high | 单次请求中 3/3 抽样标记可达 |
| 直接 stream-json CLI / Gemini 3.8 Flash | 269,100 | low | 5/6 标记，末尾缺失，仍 SUCCESS / 0 |

最后一例进一步核验本地会话的 `gen_metadata`，实际模型请求只保留原始输入前 **191,985 字节**，含 `<truncated 77520 bytes>`，尾部标记不存在。不是仅凭模型漏读来猜测截断。stderr 为空。只提交脱敏摘要，不提交完整系统提示词、账户或会话数据库。

因此保留网关 190,000 字节保护，将 Dalton 已实测的 Flash low/high 输入保护提高到 **170,000 字节**，给宿主封装留余量。部分旧调用方交给路由器的是 token 估算而非字节数；适配器因此在本地 broker 发送前再次计算实际提示词的 UTF-8 字节数。170,001 字节即拒绝，169,998 字节通过本地发送前检查，且只作用于这两个实测档案，使 30k–170k 范围恢复候选资格。档案声明的更小限制仍优先，超出不付费。修链脚本不再仅因旧的 30 KB 观察值删除这些模型。没有替用户重排默认链。

可重新尝试：中等长度的语言检查、资料摘要、抽取、研究草稿与现有梯队回退。本轮只证明三处抽样标记可达、未观察到所测尾部截断，不证明整段逐字节完整，也不认证所有研究任务的 schema、事实保真或输出长度质量；实际 WorkOrder、费用和独立核验约束仍然适用。尤其高推理输出曾触发 `PROVIDER_BUDGET_EXCEEDED`，含 reasoning 的 usage 可能超过 WorkOrder 输出预算；输入边界放宽不能解决它，也不保证 provider 端硬截住输出。Dalton 的 `Work.question` 超过 170,000 UTF-8 字节即须分块或改用其他合适档案；190,000 字节仅是网关对完整封装的保护边界，20 KB 封装余量是保守假设。Antigravity 不因这次探针获得 `provider-controlled-verify`。

另补一组 **130,000 UTF-8 字节** 的合成结构化抽取：输入混合三条项目的旧版及新版记录，要求仅输出各项目最新 revision 的五个字段。low / high 均逐字段完全正确（各一例），耗时 9.350 / 10.224 秒；报告输出 token 为 68 / 1,165。这个例子支持把中等长度的版本消歧、结构化抽取列为可尝试用途，且表明简单抽取先用 low 更省输出预算；并不构成通用质量认证。各 Work 自己的输入 token、输出和费用上限仍须满足。

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

### 最终测试与历史队列核对

冻结代码 `81bbc849`：全量 **9,683 项通过，4 项跳过，零失败**（786.246 秒）。发布包 Python 3.14 关键 smoke 56 项通过；hermetic replay 再次通过，零模型调用。随后补充的永久挂起恢复入口通过 159 项文档研究与 launcher 专项测试（76 秒），独立复核 51 项通过；按精确失败 summary、无 start、Core 无模型 Work 判定，每 tick 最多恢复一条，且将已检查的 summary SHA 绑定进原有受控重入审计。

P0-3 的历史规模必须按 **Core 内的 Scheduler 表** 统计，外部 `scheduler.sqlite` 属于规划器，不能拿它证明文档模型未调用。一次中间核对误用了外部 Scheduler，已废弃对应收据并重新核对：31 条当前策略失配 hold 中，9 条未创建模型工作、6 条只有成功的模型阶段、16 条已有失败或未决模型阶段；原报告的 14 条分别为 3 / 1 / 10。正确收据明确标出数据源及被替代收据的哈希。

可恢复队列分两组：4 条已启动但尚无模型工作、6 条可复用成功模型结果，走既有受审计的 reentry 入口，沿用原 admission 的预算，不增加模型恢复授权；另外 5 条尚无 start 和模型工作的 terminal hold，使用严格校验原失败 summary 的单次重入逻辑。已有失败或未决模型工作的 16 条不会因换策略而获得新的未授权调用。其原始失败包括输出契约拒收、adapter 拒绝、provider controls 不可用及缺少执行证明；不能把它们统称为同一项“配置已修好即可重买”的故障。

### 第一批上线验证（13:32–13:36 UTC）

- 源码 `061cdc59fb71f031ec239e8aa4d6b0c21e224208`，发布 `f3ee7e46e675c6f7f2f351b1ac223c700b1a7099f17dffcc7191bbfe9a899d3d`。锁定依赖一致，最终发布包 Python 3.14 的 lane/launcher/production 51 项通过。
- 先停止新工作并确认三个环境无在途子进程，再停止旧 reader；配置备份后将三个服务持久设置为 4 GB / 7 天，切换全部 11 个 LaunchAgent 及全部发布指针。首次短窗口健康检查赶在 legacy 首次重建前，重查已通过：全部指针一致、writer 可读、心跳前进。
- 993 个超过 7 天的 legacy 原始对象完成压缩及 SHA 回读，释放 72,673,795 字节；压缩后物理占用约 920,385,993 字节。没有丢弃原始内容，回滚旧 reader 前必须先 restore。
- 10 条经过 Core 复查的安全重入授权已写入；随后另核实并放行 4 条因 retrieval envelope 碰撞挂起、且从未创建模型 Work 的任务，共 14 条，均未新增模型预算授权。另外 5 条未开始任务由新增单次恢复入口自动处理。
- 13:36 UTC：legacy 从 14,240 增至 14,248 条 claim，Hyperscaler 从 2,475 增至 2,476。行情、一致预期、抽取、earnings 均重新启动。
- legacy 新抽取 `f7cdc99f9afbb301e4e0c796` 成功，两个原本绑定 orphaned 票据的 review 重新读出，8 条候选入账、16 次正式权威写入，summary 不再有 ticket/summary/manifest disagree。

进一步逐条核对上述 16 条含失败或未决 Work 的 admission：共有 4 条精确 RecoveryLink，其恢复 Work 全部已有执行尝试，3 条已成功、1 条输出契约拒收；没有可直接迁移的未执行恢复 Work。因此不能用“曾失败”代替当前有效阶段状态，也不能把 16 条统称为需要重买。3 条成功恢复应复用；另 12 条没有精确 RecoveryLink，必须回到各自原有的失败分类及授权入口。

13:48–13:50 的重入验收还暴露两项确定的历史兼容缺陷：

- `stored Work drifted`：旧 published prompt 与 `ebf7273d` 的提示词更新不同。两个实案仅 `question`、`prompt_hash`、`model_request_binding_hash` 三处不同；按已知旧模板重建可逐字节匹配。retrieval、verifier 和最终 staging 均无差异。不能跳过工作哈希检查，需要明确验证已知历史模板与原执行证明后复用。
- `candidate_source_materials identity conflict`：材料 ID 仅由共享 search proof 定名，但不可变正文包含 admission 及模型证明，两条任务因而以同 ID 写不同材料。需为新材料采用包含完整权威的版本化身份，并保留旧记录的精确重放。

两项已补修并通过专项回归，包括旧候选已落盘而 Scheduler 尚未登记完成的中断场景；只读实案验证精确重建两条原 Work。最终全量及部署验收继续补记。

其他上线观察：行情和一致预期已成功写入新版本；crowd 已重新记录 X / 雪球 / Blind 数据，其活跃 tick 仍可能超过 5 秒目标（一次为 14.41 秒），尚不能宣称 crowd 的网络及记录开销已消除。EPAM 的 Blind employer slug 被连接器拒绝，属于输入配置问题；个别当前日行情 bar 因 open 超出 high/low 被完整性校验拒绝。一次 consensus 后续 scan 仍报告一般只读 WAL 无边车。追踪发现纯规则 broker-note 抽取在读完并校验来源后，不必要地打开模型预算库以追加 model_binding；该抽取器并不使用它。第二批仅为该确定性抽取提供完整来源验证后的上下文，避免打开不相关的模型权威；模型抽取仍保留严格只读预算校验，没有全局放松实时库的一致性保护。

### 第二批历史兼容修复的约束

- RecoveryLink、旧授权 Work、当前基础 Work、阶段权威刷新、原授权证明与预算都纳入追加式重绑定记录。旧 Work 有任何领取、调用或完成记录均不能迁移。
- 先在同一 Core 数据库原子登记重绑定，再确定性入队；数据库同时禁止领取旧授权 Work，以及领取缺少精确重绑定的新 Work。预留后或入队后中断均可收敛。
- 成功结果跨策略版本复用前，再验证完整恢复链、授权证明、Work、路由、调用和预算。修改审计正文而保留旧哈希列也会拒绝。
- 已用授权与恢复次数持续累计；策略更新本身不创造恢复额度，未知送达状态和已付费契约失败仍使用原有不同恢复入口。

第二批冻结前：文档研究完整模块 76 项通过，独立复核通过；最终不同 epoch 竞争、双连接领取约束及中断恢复专项 3 项通过。确定性上下文与 consensus 专项 62 项通过。源码 diff 检查通过。


第二批候选 `c1a2ba36` 的全量检查发现新库初始化顺序缺陷：9,702 项中 47 errors、1 failure，全部追溯到 Scheduler 表建立前安装跨表 lease trigger。该候选没有上线。修复将这两条 guard 延至共享 Scheduler 存储已初始化的 Executor 构造阶段，保留映射写入及双连接领取约束；fresh bootstrap、Service、schema-only migration 涉及的 16 个模块共 317 项复跑通过，文档研究 77 项通过。随后重新冻结并运行最终全量，结果另记。

逐条只读审计另识别两条旧 `atomic_day_budget_refusal` RecoveryLink：当时完整预算 binding 与不可变拒绝凭证一致，但当前 policy-17 / mandate 12 覆盖旧 policy-11 / mandate 7 的治理外壳后，被错误地判为历史 proof 漂移。修复仅从 canonical、完整哈希及 Work/attempt/route/policy/day 均核对的原始拒绝行验证历史 binding；其完整字段必须精确匹配当前预算，或由不可变原 admission 与 mission 重建的原始预算。当前 mission 身份、pool/lane 仍必须一致；未知中间治理版本继续拒绝。成功恢复结果还须重验完整链及自身已结算预算。新发送继续使用当前预算。这样一条可复用既有成功结果，另一条回到原有付费契约失败入口，不凭空增加额度。

另外两条历史 admission（legacy `e83cce…`、Hyperscaler `256f75…`）只有 `ready → leased → expired → ready`，没有 formal result，也没有 refresh/recovery marker。无法证明未发送，继续暂停是正确行为；不能将其冒充可安全重放的旧成功结果。

最终冻结前的历史预算专项 5/5 通过，独立复核无未决阻塞；16 条实案在保留精确线上执行权威与原始绝对路径的一致性副本中复跑，0 live writes、0 model calls。两条预算误挡已分别恢复成功结果复用与付费契约失败入口；只余上述两条缺 formal result 的任务继续拒绝。收据见 `live16-model-authority-classification.json`。


### 第二批最终冻结测试（15:15 UTC）

源码 `d76d787895c2dc87f627318e834d537524f13b6b` 全量 **9,708 项通过、4 项跳过、零失败**（812.298 秒），记录确认运行期间 src/tests/scripts 没有变化。最终发布 `30a9e0d4af1654d45e560bfe9d82fa699cc7a0d429c4052caefbe2e1a34d930a` 从干净提交构建，全部依赖与 lock 一致；实际安装的 Python 3.14 发布包关键测试 **171/171** 通过（63.831 秒），hermetic replay 再次通过，模型及网络调用为零。

全量通过后，停止三个 controller 及 publication worker，确认三个环境没有在途 child ticket，再停止并确认全部旧进程退出。随后执行 11 个 LaunchAgent 及 manager/workspace/runtime 指针的统一切换。


15:18–15:20 UTC：全部 11 个 LaunchAgent、manager、两个 workspace manifest、runtime/release 指针及 publication gate 均切至最终发布。首次短窗口检查在旧心跳上等待，随后三个环境均显示新的启动时间及持续前进的心跳，`last_error=null`；独立健康重查通过。7 条仅含成功模型结果的任务与 14 条已分类任务获一次精确重入许可，无新增模型恢复预算授权。4 条原安全清单项已经离开旧 hold，按实时状态跳过；两条无 formal result 的未决 Work 未被放行。队列开始消费，尚不能据此宣称所有历史任务均已完成。


首条上线队列验收：legacy `03c4103b…ae719c2f` 于 15:20:57 UTC 完成，summary `complete` / error null，已从旧付费契约失败走完既有受限恢复。7 条 success-only 重入的原模型 Work 和 formal result 哈希均未变，新增 invocation 为 0，但本次观测时仍在排队，不能把未执行的队列项记为完成。当前总 claim 相较修复前为 legacy +11、Hyperscaler +30；后台队列继续按原调度、预算和内容验证规则运行。部署、授权和复用核对收据均已归档。
