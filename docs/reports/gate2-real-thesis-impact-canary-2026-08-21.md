# Gate 2：真实 thesis-impact 隔离 canary

日期：2026-08-21  
状态：控制链完成；独立 verifier 判定 `reject`，assessment 未进入 eligible，也未写入五家公司简报

## 结论

Gate 2 已用 Microsoft 的 Gate 1 正式 Claim 跑完两次真实模型调用、模型返回后进程崩溃、`replayOnly` 恢复、
不同模型家族复核和无 broker 离线重放。控制链通过：两条 invocation、两条 usage、两条 cost 全部一一对应，恢复
没有重复花费，Thesis current pointer 不变，四个 SQLite integrity check 都是 `ok`。

内容质量没有通过。GPT-5.6 Sol 把 18.30% 收入同比判断为 `insufficient`：收入增速符合 driver 的收入端前提，
但 Claim 没有经营利润率、经营利润或费用增速，不能判断 operating leverage。DeepSeek V4 Flash 随后给出
`reject`。它的五条 findings 中有自相矛盾的 ref/hash 描述，并提出 closed taxonomy 之外的影响标签；因此这份
复核本身质量也不合格。系统仍按正式 verdict fail closed，不把 assessment 当成可用研究结论。

脱敏摘要见
[`docs/review-evidence/gate2-real-thesis-impact-summary.json`](../review-evidence/gate2-real-thesis-impact-summary.json)。
仓库外原始结果 SHA-256：`a03a055725d4d6922e00c26e90251821eebe2e0fb48349b52353922ec1cc1e32`。

## 本次正式运行

- 输入：Gate 1 MSFT Claim `claim-version:cb32bdeb2094e2dad62d3e4ee38ce18689dbbe75c7bf6a500905e1f457515e93`，
  季度收入同比 18.30%。
- Thesis：`thesis:msft:revenue-operating-leverage`，driver 为
  `Quarterly revenue growth sustains operating leverage.`。
- assessment：OpenAI GPT-5.6 Sol，881 input / 267 output / 1,148 total tokens，实际成本 USD 0.012415。
- verifier：DeepSeek V4 Flash，1,476 input / 714 output / 2,190 total tokens，实际成本 USD 0.000796。
- family independence：`openai-gpt-5.6` → `deepseek-v4`；verifier route 冻结 producer family constraint。
- 本次两条调用成本：USD 0.013211。
- 已知历史成本加本次成本：USD 0.169986；另为一次无费用遥测的 TIMEOUT 预留 USD 0.25，累计上界
  USD 0.419986，低于 owner 授权的 USD 1.00。

执行 commit：`b980bba7ca3113ef16824e9b4a1055ae5c769864`；离线审计 commit：
`c88746b0a44e1e0d161edaa52eb0a0a17b01566f`。两次运行时仓库均为 clean。

## 崩溃恢复与重放

assessment 的 provider completion 先写入 broker journal 和 Core invocation/usage/cost，随后 worker 在
`Scheduler.complete` 前以 exit code 86 退出。30 秒 lease 过期后，attempt 2 复用同一 route 和 invocation，只向
broker 发送 authenticated `replayOnly`；broker 返回 `duplicate`，没有第二次 provider call，也没有新增
invocation、usage 或 cost。

正式 assessment 与 verifier 结果落盘后，又换成一个在访问 broker key 时立即报错的 deny adapter 运行完整
runtime replay。结果稳定重现 `rejected`，两条 WorkOrder 都直接读取 formal result，模型记账数量不变。

## 真实运行暴露的问题

1. 首次执行遇到 live broker 仍是重启前代码，不认识 `replayOnly`。后续脚本增加零费用协议预检；预检必须收到
   `IDEMPOTENCY_MISS` 且 `provider_called=false` 才允许付费调用。
2. Claude Opus 5 首次返回 7,635 input / 3,221 output tokens，超过旧 verifier WorkOrder 的
   3,500 / 400 上限。旧 adapter 在构造 ModelInvocation 前抛错，导致这条 USD 0.118700 只在 broker journal、
   不在 Core。adapter 现改为拒绝内容但保留 invocation/usage/cost，专项测试 16/16 通过。
3. Claude 第二次 verifier 超过 120 秒，broker 记录 `TIMEOUT`，没有 token 或费用遥测。预算因此为这条不确定费用
   预留完整 USD 0.25，未按零成本处理。
4. DeepSeek 按时返回，但质量不稳定：它一边确认 ref/hash 相同，一边把同一项写成 mismatch；还建议使用
   `none / contradictory`，而 authority 只允许 `supports / weakens / no_change / insufficient`。正式 rejection
   保留，不能为了得到 pass 而改写历史 verdict。

## 验收边界

Gate 2 证明真实调用、预算、不同 family、崩溃恢复、记账和离线 replay 控制有效；它没有证明当前 verifier
组合达到研究生产质量。下一门应针对 verifier false positive 做独立、无信息泄漏的校准集，预先冻结 pass/reject
标准后再比较模型；在此之前不部署 live、不自动修改 ThesisVersion，也不把本次 assessment 写进投资简报。
