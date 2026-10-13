# 深度研究分组暂停与费用连续性

本设计基于 49975aba。当前生产与 v25 冻结包不变，不调用模型。

实际 MSFT 0c53ac…运行在 11:40:36 启动，只重写 market/thesis 两组。两个原 Work 的 max_seconds 都是 180，分别在 11:43:38、11:46:39 形成 TIMEOUT/dispatch_state unknown 的正式失败。原预留 803,784 与 859,600 微美元均保守结算保留；summary cost_micros=0、cost_status=unproved 不能解释为免费。源码一般最多四组，但本次并未发送四组。精确只读收据：/tmp/ar-v01125-msft-deepgate-exact-stage-ro-r01-20261012.json（a40c0df528292dbe4d9286bb115021f3213f2a7a937acbf504b7dbf87a419cf0）。

当前 deep_insight_gate_cli 在组 unavailable 后仍继续 wanted 中下一组（1590–1633），完成全部 wanted 才在 1634–1648 汇总 execution_incomplete。此 CLI 没有合作暂停入口。draft_group 形状修正、returned 数字修正、rubric 修正及独立 verifier 各有调用入口，不能只检查最外层组循环。成功解析和失败成本不是同一件事，现 spent 只汇总返回 call cost，无法单独证明同一冻结输入全轮历史费用完整。

## 最小交付与强边界

1. 建立 purpose-local 合作暂停请求，沿用现有 RunQuiesce 的 ticket/PID/start/命令/两个配置路径与字节指纹强绑定，附公司与 source fingerprint。原请求 fsync、单一独立 successor 目录；不得重写旧 ticket、Work、账本或 summary。
2. 每个 producer、允许的修正、verifier 调用前检查暂停；已开始调用正常完成。返回后须有完整正式 Work/result/route/admission 证明，才可称 settled boundary。未知费用可停止新派发，但必须明列 unknown/保守占用，不伪称成功 settled quiesce。证明不足时停派并明确 unproved，不能靠该状态取得自动新购授权。
3. 成功组不保存弱解析缓存作为金融权威。恢复沿原 prompt/request/Work/Formal 强缓存读取并完整重验；冻结输入不变才复用，实际输入变化进入正常新研究。旧成功/失败 authority 保持，unknown 原预留保留。
4. 在第一条强系统性 provider/transport unknown 正式失败后停止本轮后续组及修正/核验，不修改用户链、不自行追加 fallback，不把故障命名为证据缺口。有正常授权的已有技术恢复仍走原机制；本包不创造新的 retry slot。
5. 在购买任何后续组或修正前，对同一完整冻结研究输入的 producer/shape/numbers/rubric/verifier 全部费用做精确归属。scope 包含 mission、公司、原完整分组 prompts、review/standard 与材料 identities，不含运行时钟或预算 epoch。call budget/policy 变化不得重置 scope。已结算计账本额，未知计原保守预留。正常 replay 新增费仍为零，历史账目分开报告。

## 范围与不能简化的地方

可复用 investment_memo_quiesce.settled_call（已支持 purpose 参数）的成功 Work/结果/路由/费用强读取；不直接复用硬编码 memo ticket schema。未结算/失败证明须独立分支，不能放松该成功 helper。Scheduler 由既有 owner 迁移新增 purpose-local append-only scope/request 索引；第二进程不得直接建 Core 写口。

预计触及 deep_insight_gate_cli.py、deep_insight_gate_draft.py、deep_insight_gate_launcher.py、mission_deep_insight_lane.py、新 purpose-local quiesce/accounting 模块与专项测试。冻结输入、request-index 与旧 repair/verifier 完整归属是必要接线，不能只加 summary quiesced。旧记录如不能证明全轮归属，仍允许强成功答案免费读取，拒绝额外付费；有完整证据的旧 ticket 可有界导入。分页 pending 必须正常自然继续，不转永久内容 held。

## 必要回归

- 两家公司、多组中间暂停，0 新后续调用；success 原 Work 重放且当前新增费 0。
- shape/numbers/rubric/verifier 四入口都检查暂停；当前 in-flight 不被打断。
- PID/start/source/config/request 错绑拒；既有未证明 pause 不自动续购。
- 第一组强 TIMEOUT/POST_SEND_RESULT_UNKNOWN 后不购买下一组，原 unknown 金额不归零。
- 先前成功、失败及未结算费用全部纳入同 scope；改预算/换运行目录不能重置总额。
- 旧修正 unknown/paid 继续消耗原修正门；缺历史或索引不足不能当零。
- 其他公司与真正新材料 scope 不受无关旧文章费用污染。

费用归属比单纯暂停接线更大，交付应独立于已就绪 v25/A/C；不以未验证的部分接线声称可安全免费续跑。

## 已实现的候选与真实验收边界

候选固定 scope 为公司与四组原始 request/prompt 身份；当前 mission、原件及问题通过单独的强 consumer 记录重验，不声称旧 Work 曾拥有不存在的原始 source hash。旧 schema、Work、prompt、成功回答及账本不改。仅可证明生产组阶段提前失败、未到达匿名修正/核验的旧记录支持 normal-only 完整导入；其他旧记录仍可免费强重验成功答案，额外购买保持 audit hold。

正常每个新 admission 前复读同 scope 全部已付/未知费用及当前 active Work 的已完成 attempts。已结算失败费用和原未知保守上界保留。当前运行新增费用与历史费用分列；未发送不凭空补结算。下一正常组、形状修正以及核验前保留其余尚未免费成功复用的组和 verifier；所有入口停派仅在完整 CockpitModel.complete 前后，不中断 Work 内部既有 fallback/transport。

人工共享预算确实增加 timeout/run 额度且旧步骤为强 pure TIMEOUT 时，可登记唯一 scope/group/normal request 的 continuation grant。后来再次提高限额或无关 policy revision 不重置该槽。AMZN/GOOGL 实录只读 counterfactual 证明当前 rev22 可授权各一个旧 thesis 步骤，历史保守占用分别 3,209,064 与 2,070,908 微美元；这是资格与余量证明，不是新正式产品或实际恢复成功。

完整界限：每 scope 最多 18 个注册角色（固定四组各 normal/shape/numbers/rubric、固定 all verifier 两项）；每前缀最多 64 个强 Work；旧 verifier census 最多 128 项；人工预算链最多 64 个 revisions。超界明确拒，不截最近 N。合作暂停正式入口为 `python -m dalton_core.deep_insight_gate_quiesce --run-dir <exact-run> --expected-ticket-hash <hash>`，部署调用方须按精确运行 ticket 发送请求；本包不改 release switch。
