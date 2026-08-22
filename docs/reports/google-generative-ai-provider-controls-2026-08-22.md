# Google Generative AI provider controls — 2026-08-22

## 结论

Dalton broker 0.1.0-spike.4 和 OpenClaw 2026.7.1 本机 patch 已增加原生 Google Generative AI 受控路径。该路径在生成前调用 `countTokens`，校验输入、输出、总 token 和最坏费用，注入 portable JSON Schema，并向 broker 返回与 model、schema、rate card 和预算绑定的 proof。

本轮尚未把 Gemini 标为生产可用。live gateway 仍在等待 safe restart；重启前的真实受控单案正确返回 `REQUIRED_CONTROLS_UNAVAILABLE`，没有进入 provider。重启后还需完成 30 案例 `provider-controlled-v1` 复跑，质量与控制证明同时达标后才可解除 verifier 锁。

## 为什么增加 portable schema

Gemini 3.1 Pro Preview 接受完整 Dalton canonical schema，但不会执行其中所有 JSON Schema 关键字。真实小样本中，canonical schema 要求 `schema_version` 为 `0.2`，模型仍返回了 `1.0`。因此，不能把“API 接受 schema”当成完整约束已经生效。

新的 provider schema 只使用 Google 文档明确支持的关键字，强制对象闭合、必填字段、枚举、数组上限和 finding 结构。完整的 hash 格式、finding code 与 severity 对应关系、pass/reject 条件等业务语义，仍由现有 canonical Python validator 在解析后校验。host 会在任何 provider 请求前拒绝 portable 集合以外的关键字，例如 `const`、`allOf`、`if/then/else` 和 `pattern`。

## Google 控制合同

- mode：`google-generative-ai-count-tokens-v1`
- transport：exact `google/google-generative-ai`
- structured output：`responseMimeType=application/json` + hash-bound `responseJsonSchema`
- 输入准入：同一 model、contents 和 system instruction 调用 provider `countTokens`
- 输出准入：强制 `candidateCount=1` 和 exact `maxOutputTokens`
- 总量准入：`countTokens + 全部 output reserve <= maxTotalTokens`
- 费用准入：用 operator-owned、带有效期的 rate card 做最坏预留
- 禁止项：cached content、tools、tool config、已有 response schema、stop sequences
- 事后复核：broker 要求 provider usage 与 admission proof 一致，并再次核对 token 与费用上限

## 价格口径

Google 官方 2026-08-22 页面显示，Gemini 3.1 Pro Preview Standard Paid Tier 在 prompt 不超过 200k tokens 时为输入 `$2/M`、输出（含 thinking）`$12/M`；超过 200k 时为输入 `$4/M`、输出 `$18/M`。本轮 profile 采用保守的 `$4/M` 输入和 `$18/M` 输出预留，并禁止 cached content，因此不会低估当前标准价。

官方来源：<https://ai.google.dev/gemini-api/docs/pricing#gemini-3.1-pro-preview>

## 验证

- broker Node：21/21 通过，新增 Google exact transport、proof、rate card 和 attribution 覆盖。
- Python targeted：30/30 通过，覆盖 portable schema package、controls hash 和 canonical validator 边界。
- OpenClaw fake transport：OpenAI 和 Google 的准入路径各为 1 次 input count + 1 次 fake model request；输入超限、费用超限、Google 非兼容 schema 和不兼容 transport 均为 0 次 model request；`paidCalls=0`。
- OpenClaw patch runner：9 组 patch 全部通过 `--check`，其中 Google 新 patch 3/3 已应用。
- Gemini 真实 schema 探针：`countTokens=14` 与生成 usage 的 `promptTokenCount=14` 一致；完整 canonical schema 探针证实 `const` 未被执行，由此触发 portable schema 修复。
- live broker 重启前受控单案：fail closed 为 `REQUIRED_CONTROLS_UNAVAILABLE`。

本轮没有运行完整 Python suite，也没有等待 GitHub Actions。已知 matrix campaign 成本仍为 `$5.023876966`；另有三次小额 Gemini schema 探针未进入 broker journal，无法给出完整 provider-reported 费用，未把估算值混入已核账总额。

## 当前开闸条件

1. safe restart 完成，live runtime 能声明 Google mode 与 exact transport；
2. live broker 加载 spike.4 和当前有效 rate card；
3. Gemini 3.1 Pro Preview 在 30 案例 `provider-controlled-v1` 下同时通过质量门和 provider proof；
4. 独立 producer family 约束继续成立。

以上任何一项未满足，production verifier 继续 fail closed。
