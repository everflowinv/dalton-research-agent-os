# 财报电话会原文 Evidence Gate v1

## 结论

电话会 connector 已完成 proposal-only 合同和离线 ACN canary，但还没有准入 live execution，也没有向 US IT
Services Evidence Pack 写入 transcript Evidence 或 Claim。现阶段仍是开发验收环境；SEC earnings exhibit 继续承担财务
actual，电话会只补管理层解释和分析师问答。

## 已实现

- 新 connector identity：`connector:earnings-call-transcript`；source identity：
  `source:company-earnings-call-transcript` / `public_web`。
- 保持 2026-08-14 冻结的 10 connector inventory 不变。新增 connector 位于独立 proposal package，状态固定为
  `proposal_only`，`lease_eligible=false`，`live_execution_allowed=false`。
- 只允许 `fetch_get`。Gemini 搜索摘要、citation snippet、HEAD、caller 提供的解析文本和付费墙页面都不能形成
  transcript authority。
- 每次调用必须绑定 Gemini/public-web URL authority、issuer ref、ticker、公司名、fiscal year、fiscal quarter 和
  source role。source role 只接受 `issuer_primary` 或 `third_party_transcript`，最终 host 还必须命中部署时的显式
  allowlist。
- parser 从实际保存的 UTF-8 HTML bytes 重建可见文本，不接收 caller 提供的 transcript JSON。正文至少 5,000 个
  可见字符、20 个段落，并同时出现 earnings-call、公司、ticker、财年、季度、Operator 和 Q&A 标记。
- source record ref 同时绑定 canonical URL SHA-256、raw body SHA-256、parser/projection SHA-256。失败时保留 raw
  bytes 的审计可能性，但不生成成功 SourceEnvelope。

## 来源实测

2026-08-23 对 `roic-transcript list --ticker ACN --json` 做了匿名只读检查，roic.ai 返回 HTTP 403。因此 roic.ai
当前不能作为 Dalton 的生产依赖，skill 只能视为可替换的 transport/parser 参考。

随后用 Gemini 做了 4 次定向发现检索，分别检查 ACN、CTSH、EPAM 和 IBM 的 2026 年最新电话会。公司 IR 结果只找到
earnings release 或视频，没有找到四家公司 IR 发布的完整 Q&A transcript。搜索同时发现 Investing.com、Fool、
GuruFocus 等第三方页面；这些 URL 仍是 discovery，未独立 `fetch_get`，未写入 Evidence，也未采用搜索合成内容中的
任何说法。

## 离线 canary

`scripts/run_isolated_earnings_call_transcript_canary.py` 用 recorded public transport 跑一份合成 ACN Q3 FY2026
transcript，验证：

- 原始 bytes 完整进入 raw sink；
- issuer、ticker、财年、季度、host role 和 Q&A 结构全部通过后才生成 transcript source record ref；
- network calls、paid model calls、formal Evidence writes 均为 0；
- connector 仍不可租用、不可 live execution。

错误样本覆盖 wrong company、wrong year、wrong quarter、缺 Q&A、付费墙、HEAD、未批准 host 和 source-role
重标，全部 fail closed。

## 对 US IT Services pack 的影响

v2 pack 的 `source:company-earnings-call-transcript` required gap 保持未完成，没有为了让 gate 看起来通过而写入合成
Evidence。下一次真实 canary 必须先选定一份可合法访问的原文，保存 exact raw artifact，再由人工复核它是否只补充
management wording/Q&A，不能覆盖 SEC actual。若使用 AlphaEngine 或其他许可库，source type 和 independence group
必须保留该库的真实 provenance，不能重标为公司 IR。

## 验证命令

```bash
.venv/bin/python scripts/build_earnings_call_transcript_proposal.py
.venv/bin/python scripts/run_isolated_earnings_call_transcript_canary.py
.venv/bin/python -m unittest -v \
  tests.test_earnings_call_transcript_connector \
  tests.test_public_web_connector \
  tests.test_connector_inventory
```
