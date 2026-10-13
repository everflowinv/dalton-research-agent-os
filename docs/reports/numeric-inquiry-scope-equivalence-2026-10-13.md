# 数字研究问题的精确去重

新计划中的 META 回购金额和稀释股数问题，被一个已经回答的递延收入问题挡住。两者来源不同、指标不同，旧匹配器仅因共同日期中的 `12` 和 `31` 判为等价。GOOGL 新期间的收入积压确认比例，也因共同的 `24` 个月被匹配到旧 Cloud RPO 问题。已有答案不包含这些新问题的答案，不能用来完成备忘录。

新 admission 匹配契约为 `mission-document-research:admission-equivalence:v2`。两个 typed numeric 问题只有同公司、同 document ref/hash、同 strategy、同完整指标定义与期间、单位、币种、basis 集合时才等价。完整定义的 hash 保留 consolidated/segment、gross/net 等限定；仅本地 target handle、排序、问题的同义措辞不改变身份。不同目标集合或定义保守视为不同研究要求，不能据此声称经济含义等同。无效目标或伪 definition hash 拒绝，不能退回数字或文本匹配。

typed/untyped 只在同原件、同规范化问题与同 evidence target 时匹配；共同金额、日期或确认期限不够。纯 untyped 问题的新去重屏蔽完整中英文/ISO 日历日期，仍保留真实金额去重。历史 v1 函数不变。

原 zero-answer target revision 和 explicit period replacement 仍使用各自完整来源证明和已消费记录。一条新 revision 只为同一规范化问题派生，不能从另一个问题的共同数字取得权限。已持久化 v1 revision 和 capability upgrade 继续按原冻结证明与 exact-stored legacy 条件回放；旧 admission、result、费用、withdrawal 和次数不改。新门在把 typed 结果视作已答之前复核完整目标与 exact succeeded Work/Formal 绑定，不从一个自报 completed 标志采用事实。

研究任务的输入签名加入固定 v2 契约，使之前因旧匹配器零 admission 结束的计划有一次普通免费 admission 重新判断机会。该签名不含 tick 时间；它不是新的付费许可，实际读取仍需当前使命、来源权限、原预算和正常 Scheduler 准入。

只读实证读取了精确新计划 `bea444e1…` 和三条旧 admission。旧 META `0e154b76…` 已完成的唯一目标是递延收入；其 exact result/formal 和完整目标闭合。候选 gate 对 META 与 GOOGL 不再给出等价阻挡；MSFT 仍返回 `target_revision_already_admitted:88c0e462…`，保留其旧零答案父结果的唯一修订限制。此为程序门的只读验证，没有执行模型、创建生产 Work 或生成新答案。

私有证据：`/tmp/ar-hyp-three-inquiry-scope-private-r02-20261013.json`（SHA256 `0528d4ba3a4e5b58bb48935008d52c3c9f40928e1594bffabb68d6b43425f1b9`）；`/tmp/ar-hyp-inquiry-new-scope-actual-gate-ro-r01-20261013.json`（SHA256 `a1d0753cea56d1792b45f789a8177fd061c4fd1384d49dca7a8a490ac3569836`）。

旧测试调整仅限明确不同 scope 的新准入预期与真实历史 v1 fixture：已答不同指标不再拦当前目标；未执行的相同 typed scope 即使换问题措辞仍阻挡；历史旧 FORM 碰撞的 proof 在旧 matcher 下构造，再由新代码逐字回放。另原基线 `95c96ae5` 的 canonical compact projection 测试已有 25000 字节 fixture 超界 57 字节，本包未修改其预算或 prompt 投影。

部署后应观察原正常 research-task lane 对当前计划的新免费 admission 过程，再核新的正式 numeric result 是否覆盖回购/股数及新期间积压比例。没有这些结果，备忘录的相应研究问题继续未完成。
