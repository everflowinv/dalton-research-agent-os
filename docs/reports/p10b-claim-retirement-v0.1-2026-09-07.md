# P10b：错的结论被挑战和退役，账本一个字都不改

*2026-09-07*  ·  基线：[愿景 v1.1](vision-and-next-phase-v1.1-2026-09-07.md)

## 这一片回答的问题

ADR-0005 让任务按 policy 自动准入定性 Claim，live 也确实做到了：229 条 Claim，
其中 224 条定性。但把它们读一遍会看到两类错误，而且**每一条的出处都无可挑剔**：

- **公司张冠李戴。** 50 条关于 LED 照明、电动车充电、道路产品、数据中心的陈述，
  被记在 EPAM 名下。AlphaEngine 的搜索返回了另一家公司的文档；每句引文都精确、
  每个哈希都对得上，主体仍然是错的。
- **免责声明。** "过往业绩不代表未来表现"——有归属、有引文、什么也没断言。

P10c 要从 Claim 起草 Initial Screen，这些必须先清掉。

## 做了什么

**账本不改，另立两条记录。** Research Ledger 是 append-only、ClaimVersion 契约是
冻结的，所以这里既不编辑也不删除任何 Claim。两条 append-only 记录承载更正：
**挑战**（谁说它错、哪个确定性检测器命中、针对哪份精确原文）与**决定**
（退役或保留）。所有回答问题、算清单、起草交付物的读取路径跳过已退役的版本；
账本本身原封不动，历史哈希全部仍然可验证，系统曾经相信过什么也仍然可读。

**两个检测器，都由权威在写入时重跑，而不是听调用方说。**

- `subject_absent_from_source`：这家公司自己的名字（取自发现计划里已有的搜索词，
  剔除"technology""systems"这类行业词）在它引用的那份原文里**一次都没出现**。
  这不是对陈述的判断，是关于字节的事实——原文里提一次就放过。
- `boilerplate_disclaimer`：起草路径早已在用的免责声明过滤器，回溯应用到它存在
  之前准入的 Claim。

**写入受任务授权约束。** 检测是免费的、永远上报；写挑战或决定是写入，按 ADR-0004
只做任务授予的事。没有 `claim_challenge` 授权时，这一跳如实报告发现并且**不改任何
东西**——这正是 owner 做决定所需要的。人可以挑战和退役任何 Claim；自动化只能依据
确定性检测器，而且只能退役、不能替你说"保留"。

**驾驶舱**："待你审批"里出现每条被标记的结论（陈述原文、发现的问题、依据），可以
选退役或保留；退役与保留都会进研究日志。

**链脚本新增 `--add-write-scope`**：只发一版任务，不做 policy 级联（写入范围是任务
级授权，不是 policy 规则）。

## 验证

- 11 项新测试：挑战绑定精确的 claim 版本与哈希、重复即幂等、自动化不能提"人的判断"
  也不能替人说"保留"、自动化决定时权威重跑检测器并在不再命中或读不到原文时拒绝、
  退役后账本行与哈希完全未变、人可以保留且不再复现、无授权时只报告不写、有授权时
  精确退役该退的、读不到原文的一律不动、文档读取预算限流并顺延、定量 Claim 不被这
  两个检测器碰。
- live 副本演练（真实 spool + 演练版任务 v8）：53 条被挑战并退役（50 条 EPAM 错误
  归属来自 5 份文档 + 3 条免责声明），第二轮写入为零，41 条读不到原文的原样保留。
- live 部署后（尚未授权）：`scanned 246 / detected 53 / documents_read 32 /
  unreadable 41 / status held`，Core 零写入。
- 全套 1226 项测试，只余两项已知的 macOS 路径失败与一项单跑即过的 node broker 用例。

## 部署时发现的两件事

**一张同名的旧表。** Core 里早就有 `claim_challenges`——账本自己的"两条 Claim 数值
冲突"记录，语义完全不同。我的 `CREATE TABLE IF NOT EXISTS` 撞名后被静默跳过，随后
建索引时才炸。新表改名为 `claim_retirement_challenges` / `claim_retirement_decisions`，
旧机制一行未动。

**新 schema 文件没进包。** `pyproject.toml` 的 package-data 是逐个文件列出的，
新增的 `.sql` 不在其中，于是装到 venv 里的包**有模块没有它的建表脚本**，writer 只
回一句通用错误。补进列表后正常。这也说明：这类"逐项列举"的清单每加一个文件都得记得
改，是个容易再犯的坑。

## 需要你跑的一条命令

授权后，被标记的 53 条会在下一跳自动退役（权威仍会逐条重跑检测器）：

```
cd /Users/everflow/Projects/dalton-research-agent-os
.venv/bin/python scripts/publish_extraction_authority_chain.py --live \
  --add-write-scope claim_challenge
```

已在 live 副本上演练通过：任务 v8 的 `may_write` 增加 `claim_challenge`，policy、
mandate、constitution 一律不动。不跑也可以——你可以在驾驶舱"待你审批"里逐条决定。

## 这一片没做

35–41 条引用公开网页的 Claim 读不到原文（网页的"原文"是确定性渲染，spool 里只存
原始字节），因此永远不会被自动退役；要覆盖它们需要在检测时重新渲染。SEC 10-K 正文
获取通道、Initial Screen 交付物（P10c）不在本片。
