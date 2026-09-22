# 两个研究环境巡检与待修清单（2026-09-22 12:20 UTC）

> 后续逐项核实发现 P0-2、P0-4、P1-4 的部分根因推测不成立；请同时阅读 [修复与复测报告](environment-repair-2026-09-22.md)。本文保留巡检时的原始判断供追溯。

## 一句话结论

**两个环境都已经实质停摆。** legacy（IT 服务）自 2026-09-18 22:51 起零产出——连接器原始落盘目录打满 1 GB 硬上限，所有对外抓取一律失败；Hyperscaler 还在动，但产出每天减半（1727 → 380 → 236 → 107 → 25 条 claim），日花费从 $18.7 掉到 $0.24。此外有一条横跨两个环境的路由缺陷：受控核验能力在核验梯队里根本没有可用模型，今天 legacy 已经被拒 110 次。

## 基线

| 项 | 值 |
|---|---|
| 源码 HEAD | `b8c0e818`（runbook）/ `ebf7273d`（代码） |
| 线上 release | `f2758484`，2026-09-18 08:59 本地时间切换，已含 `cfe6c0c9` + `ebf7273d` 两批修复 |
| 进程 | 3 个 writer 已连续运行 3 天 23 小时，无崩溃重启 |
| tick | 三个环境都稳定 12 次/小时，无漏跳 |
| 僵尸进程 | 一轮内回收，无堆积 |

三个环境：legacy（`/Volumes/EveSSD/Dalton/legacy-state/dalton-core`，8793）、Hyperscaler（`ws-7d894366d1132e2930475a60`，8795）、新环境（`ws-e399ececd5aa7a3762b1a0a4`，8794，无研究目标）。

## 产出与花费

| 日期 | legacy claim | legacy 花费 | Hyperscaler claim | Hyperscaler 花费 |
|---|---|---|---|---|
| 09-17 | 7783 | $50.12 | — | $20.43 |
| 09-18 | 52 | $35.31 | 1727 | $18.73 |
| 09-19 | 0 | $17.46 | 380 | $1.84 |
| 09-20 | 0 | $9.54 | 236 | $1.22 |
| 09-21 | 0 | $5.97 | 107 | $0.91 |
| 09-22（半天） | 0 | $1.28 | 25 | $0.24 |

---

# P0：必须先修

## P0-1　legacy 连接器落盘打满 1 GB，所有对外抓取自 09-18 起全部失败

**现象。** `company-wiki`、`sales-notes`、`prior-research` 三条来源车道在过去 24 小时的 430 轮 tick 里，430 轮都是 `unavailable`，原因统一是 `RemoteError: writer service failed to complete the request`；writer 日志里对应 `HostToolRunError: <source> list_documents child failed: exit 1`。子进程自己写下的失败原因是：

```
RawSpoolCapacityError: raw spool high-water mark reached
```

最后一次成功落盘是 2026-09-18 22:51。

**根因。** `src/dalton_core/raw_spool.py` 的 `RawSpool.__init__` 要求调用方传 `max_total_bytes`，全仓 29 个调用点里 28 个硬编码 `1_000_000_000`。`open_sink` 在每次写入前把 `objects/` 目录现有字节数加上本次预留字节数与上限比较，超了就抛 `RawSpoolCapacityError`。而这个类**没有任何删除接口**：唯一的回收方法 `gc_orphans()` 只清 `tmp/*.partial`（未完成的下载），`objects/` 只进不出。

实测 legacy：`connector-spool/connector-spool/objects` 为 967 MB、8188 个文件，最老的落盘日期是 2026-08-26，从未被清理过。按天分布：09-14 有 787 个、09-15 有 755 个、09-16 有 1117 个、09-17 有 4053 个、09-18 有 1220 个——09-17 那天的 4053 个文件直接把余量吃完。

**这不是 legacy 独有的。** Hyperscaler 目前 94 MB，新环境接近 0。按 Hyperscaler 现在的速率，它也会撞上同一堵墙，只是时间问题。**任何环境跑够久都必然死在这里**，这是设计缺陷不是配置问题。

**修复方向。**

1. 给 `RawSpool` 增加保留策略：按内容哈希被引用与否 + 落盘时间做回收（目录扫描回收已被目录/证据链固化的对象），或者把 `objects/` 改成明确的「短期暂存、落库后即删」语义。要点是回收必须证明该对象已经被更持久的地方（catalog / acquisitions / evidence）收下，不能按时间盲删。
2. 上限从硬编码改成可配置（每个环境的 `service.json` 或运行时配置），并在接近上限时通过 needs-human 预警，而不是撞墙后静默地让所有车道 `unavailable` 430 轮。
3. `RawSpoolCapacityError` 现在被包成 `HostToolRunError: child failed: exit 1` 才冒到日志里，子进程的真实原因没有向上传递。要把子进程 stderr/failure_reason 带进 `HostToolRunError` 的消息，否则下次同类问题还是要翻三层目录才找得到。

**临时缓解（owner 可执行，见文末命令）。** 把 `objects/` 里 09-16 之前的对象移走到备份盘，可立即释放约 20% 余量，让 legacy 恢复抓取。但在 1 之前这只是拖延。

## P0-2　受控核验 rate card 已过期，当前目录无可用受控模型

**现象。** `model_route_decisions` 里 `capability = provider-controlled-verify` 的决策全部 `rejected`：legacy 今天（09-22）已有 110 条，历史上还有 09-15 的 320 条、09-16 的 70 条；Hyperscaler 今天 1 条。受影响的工单前缀：`research_language_check`（290）、`plan`（82）、`research_localization_verifier`（63）、`event_judgement_verifier`（49）、`debate_map_verifier`（9）。

Hyperscaler 的 debate map 车道因此长期 held：

```
the verifying call did not run: the model call did not succeed (MODEL_ROUTE_REJECTED)
subject_ref: company:ticker:amzn
```

**复核后的根因（更正原判断）。** 候选快照里每一个候选的 `rejection_reasons` 都含 `capability_not_supported`，但 `broker-gemini-...:3/:4/:5` 不是同名模型的多个可选变体，而是**同一个 `profile_id` 的追加式不可变历史版本**。路由器只使用最新版本；旧版本只供审计，不能因旧版本曾有能力就拿来承接今天的调用。

legacy 当前有 38 个最新 profile，携带 `provider-controlled-verify` 的是 **0 个**。两个 Google profile 的历史版本曾携带该能力，最新版本在 2026-09-22 00:01 UTC 目录同步时正确移除，原因是 OpenClaw 的两份公开 `providerControls.rateCard` 都明确写着 `expiresAt = 2026-09-22T00:00:00Z`。这也解释了为什么 09-21 22:43 UTC 仍有成功选择，而 09-22 起全部拒绝。把能力从历史版本“继承”到最新版本会谎报已经失效的受控合同，不能这样修。

原始 rate card 文档已写明“到期需更新”。2026-09-22 重新核对 Google 官方价格后，当前保守预留价仍覆盖公开价格：Gemini 3.8 Flash 当前促销价为输入/输出 `$0.75/$3.75`，现配置保守使用 `$1.50/$7.50`；Gemini 3.1 Pro Preview 在大于 200k 输入档为 `$4/$18`，与现配置一致。依据：[Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)、[Google Cloud Agent Platform pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)。

**修复。**

1. 配置侧需要在保留 model、mode、thinking level 和四个价格字段不变的前提下，按本次官方复核更新两份 rate card 的 `verifiedAt`，并给出不超过 31 天的新 `expiresAt`。仓库新增的 `scripts/renew_google_provider_controls.py` 默认只演练；真正写入必须带演练输出的完整配置 SHA-256，且会先在原目录保留备份。不能只手改到期日，也不能从历史 profile 恢复能力。
2. 目录同步现在明确报告 valid / expiring / expired / invalid 的 provider controls，并记录本轮从哪个 profile 移除了 `provider-controlled-verify`，不再只留下泛化的 `capability_not_supported` 供事后反推。
3. verifier 梯队保存会用**最新** profile 当场验证能力覆盖；没有任何仍有效的受控 link 时原子拒绝，不改策略文件。模型配置页同时把该梯队标红，直接说明所有 `provider-controlled-verify` 工单会在调用前被拒。

## P0-3　文档研究 admission 被模型路由策略版本滚动整批作废（待办里 32 条）

**现象。** 待办审批：legacy 27 条（25 条 `controlled_recovery` + 2 条 gate 自动退回），Hyperscaler 7 条。hold 账本 legacy 28 条、Hyperscaler 9 条。部署后两条车道确实重新派发过（legacy 18 次子进程、Hyperscaler 4 次），但新的失败原因变成：

```
MissionDocumentResearchError: mission document admission is no longer executable   （legacy 12 次 / Hyperscaler 2 次）
```

**根因。** 09-18 那批修复解决了 mission 版本滚动的问题，但同样的病还有第二层。`resolve_for_execution`（`mission_document_research.py:695`）只允许 `GOVERNANCE_ENVELOPE_FIELDS = ("mandate_binding", "outer_budget")` 这两个字段随版本变化，其余字段必须逐字节一致。而 `_derive` 里的 `model_execution` / `model_authority` 来自 `self.model_execution_resolver()`，读的是**当前**的模型配置文件，其中含 `routing_policy_ref`。

实测 legacy 最近 40 条 admission 的 `model_execution.draft.routing_policy_ref` 分布：

| 策略版本 | admission 数 | 当前线上 |
|---|---|---|
| `...company-dossier:53` | 4 | ← 当前 |
| `:52` | 2 | |
| `:51` | 2 | |
| `:50` | 11 | |
| `:49` | 6 | |
| `:48` | 10 | |
| `:12` | 5 | |

Hyperscaler 同理：当前 `:54`，只有 2 条 admission 对得上，其余 25 条停在 `:35`、`:51`、`:53`。

**每改一次模型配置，路由策略版本加一，所有在途 admission 立刻全部作废。** 而 owner 明确要求各环境模型配置同步（一处改动同步到所有环境），这让策略版本滚动变得更频繁——修一次模型配置，就杀一批在途研究。

**修复方向。** 与 09-18 那批同构：模型权威也应当是「执行时重新解析并完整校验」的外壳，而不是钉死在 admission 身份里。具体是——admission 身份保留它被 admit 时用的模型权威（供审计与复现），执行时按当前权威重新解析，并要求新权威仍然满足该工作流的能力/家族独立性/预算上限要求；把「实际运行在哪一版权威下」记进结果。只有当新权威**不满足**工作流要求时才拒绝。注意这一条要和 P0-2 一起做：如果权威重解析后发现梯队缺能力，那就是 P0-2 的问题，应当报出来而不是静默作废。

## P0-4　legacy 抽取车道空转：12 次里 12 次零入库

**现象。** legacy `extractions` 最近 12 次运行，`admitted` 全为 0，24 个 discovery 窗口全部：

```
AcquisitionLaunchRejected: ticket, summary and manifest disagree
```

同时 `reviews_skipped_exhausted` 已达 discovery 68 / numeric 46。

**根因判断。** 高度怀疑是 P0-1 的下游后果：抓取子进程在写完 ticket、还没写完 summary/manifest 时因落盘容量失败退出，留下三者不一致的残局；之后每一轮抽取都重新撞上同一批坏票据。需要确认的是——这些不一致的票据是否会自愈，还是需要一次性清理。

**修复方向。** 先修 P0-1，再验证是否自愈；不自愈的话需要一个「三者不一致即作废该票据并允许重抓」的收敛路径，而不是无限重放。

---

# P1：影响明确，但不致命

## P1-1　`SchedulerConflict: ResultEnvelope id already identifies another accepted completion`

部署后 legacy 4 次、Hyperscaler 1 次（09-18 16:27/16:32、09-22 00:31/00:32/00:37/00:41）。受控重入复用了与此前已接受完成相同的 ResultEnvelope id。重入应当生成新的 envelope id，或者在发现已有已接受完成时把该 admission 直接判为已完成并结清，而不是报冲突后升级给人。

## P1-2　预算策略只读读取仍然失败（09-18 的重试不够）

```
MissionDocumentModelAuthorityError: installed mission document draft budget policy is unavailable:
OperationalError: read_only WAL requires existing WAL/SHM; no sidecars were created
```

部署后仍出现 3 次（legacy 09-18 15:52、16:02；Hyperscaler 09-22 00:41）。`ebf7273d` 加的是 3 次 × 0.5 秒重试，对这个窗口不够。这个报错的本质是：以只读方式打开 WAL 库时，若没有现成的 `-wal`/`-shm` 边车文件，SQLite 无法自己创建。正确做法不是延长重试，而是**避免只读打开处于 WAL 模式且无边车的库**——例如改用 `immutable=0` 的普通只读连接并容忍边车缺失，或让写入方保证边车常驻。

## P1-3　落盘目录路径多套了一层

`RawSpool.__init__` 内部会在传入路径后再拼一级 `connector-spool`，而多数调用方传的已经是 `<state>/connector-spool`，于是实际路径变成 `<state>/connector-spool/connector-spool/objects`。功能无碍，但排查时非常误导（`du -sh <state>/connector-spool/objects` 会显示 0）。建议统一。

## P1-4　legacy 有 5 家公司全部 held，拖住三条车道

`dispatch_mission_reopen` 连续 429 轮 `held`（companies 5 / held 5），`dispatch_mission_consensus` 与 `dispatch_mission_market_prices` 都因 `covered-company refresh is blocked (held=5)` 而跳过。另有 `company:sec-cik:0001467373` 的 dossier 连续 383 轮验证不通过：

```
dossier verification did not pass: [{"unit": "our_view", "code": "unsupported_sentence", ...}]
```

需要确认这 5 家是不是同一批卡住的，以及 dossier 的 `our_view` 段落为什么持续产不出有支撑的句子——这可能与 P0-2（核验能力不可用）相关。

## P1-5　`dispatch_earnings_season` 每轮稳定超预算 12–15 秒

legacy 近 24 小时内，该车道 430 轮全部 `busy`，`over_budget_seconds` 集中在 11–15 秒（预算 5 秒），原因是「all due earnings windows are held」。一个全部 held、无事可做的车道不应该每轮烧 16 秒——这是 legacy tick 平均 41–47 秒的主要来源之一。`dispatch_mission_crowd_sources` 同样超 7–8 秒。

---

# P2：观察项，暂不处理

- **新环境 `ws-e399ececd5aa7a3762b1a0a4` 至今没有研究目标。** 全部车道 `lane_unconfigured_hold`，属预期。但它的 `dispatch_mission_source_discovery` 每轮报 `discovery plan is unusable: [Errno 2] No such file or directory: .../discovery-plans/us-it-services-alphaengine-...`——这是车道对齐时从 legacy 复制过来的计划引用，指向一个本环境不存在的 us-it-services 计划文件。等 owner 给这个环境派目标时会一起解决，但对齐脚本不该把别的环境的 mission slug 带过来。
- **僵尸进程**：观察期间恒定 2–5 个，均在一轮 tick 内被回收，无堆积。
- **tick 时长**：legacy 从 09-17 的 68 秒降到 41–47 秒（09-18 那批修复有效）；Hyperscaler 从 1.4 秒升到 20.6 秒，趋势需要盯，但绝对值仍健康。
- **模型冷却/回退**：两个环境近 3 天均无 `model_fallback_notices`、无 `model_profile_cooldowns`。

---

# 建议的修复顺序

| 序 | 项 | 谁做 | 说明 |
|---|---|---|---|
| 1 | P0-1 落盘回收 | 派 opus 子代理 | 先加回收与配置化上限，再由 owner 做一次临时清理让 legacy 复活 |
| 2 | P0-2 核验能力 | 派 opus 子代理 | 与 3 合并做，因为 3 的重解析依赖 2 的结论 |
| 3 | P0-3 模型权威外壳化 | 同上 | 32 条待办部署后应自行消化 |
| 4 | P0-4 抽取票据一致性 | 修完 1 后复验 | 可能自愈 |
| 5 | P1-1 / P1-2 | 同一个子代理 | 都在文档研究车道内 |
| 6 | P1-4 / P1-5 | 单独一轮 | 需要先排除 P0-2 的干扰 |

## 原巡检临时缓解建议（已被可逆压缩修复替代，不再执行）

> 13:32 UTC 后已经部署支持透明解压的 reader 并完成有哈希验证的可逆压缩。下列移动命令仅保留作历史记录；直接移走对象会使证据引用失效，当前无需执行。

在修复落地前，把 09-16 之前落盘的原始对象移到备份盘，可释放约 200 MB：

```zsh
S=/Volumes/EveSSD/Dalton/legacy-state/dalton-core/connector-spool/connector-spool/objects
B=/Volumes/EveSSD/Dalton/backups/raw-spool-archive-2026-09-22
mkdir -p $B
# 先看会移走多少（只读，安全）
find $S -type f -newermt "2026-08-01" ! -newermt "2026-09-16" | wc -l
du -sh $S
# 确认后再执行移动
find $S -type f ! -newermt "2026-09-16" -exec mv {} $B/ \;
du -sh $S    # 期望降到 800 MB 以下
```

移完不需要重启 writer：容量是每次写入前现算的，下一轮 tick 就会恢复抓取。**注意**：这些对象是连接器原始响应的留存证据，移走后若有引用链要回溯到原始字节会读不到，所以是移到备份盘而不是删除。是否可以安全移动，取决于 P0-1 里「哪些对象已被更持久的地方收下」这个问题的答案——在子代理查清之前，建议只移 09-11 之前那一小批（约 20 个文件），或者等修复。

---

*巡检范围：tick ledger（三环境 24 小时全量车道状态）、needs-human、writer stderr、模型路由决策/回退/冷却、预算结算、落盘目录、抽取与文档研究子进程产物、进程表。*
