# P9d-3b：mission 预算准入、模型 broker 与人工引用入 staging

日期：2026-09-06
状态：本地实现与自动验收通过；未部署，真实模型 canary 未执行。
分支：`p9d3b-llm-document-extraction`；接续基线 `7d51c53`，保留上一轮未提交改动。
上游：[前一轮阶段报告](p9d3b-document-evidence-and-extraction-scaffold-v0.1-2026-09-05.md)、[ADR-0003](../adr/0003-transcript-candidate-admission.md)。

## 同日隔离副本接续

`f0ab1cb` 之后新增的只读修复与启用预检见 [v0.2 报告](p9d3b-readonly-preflight-v0.2-2026-09-06.md)。本报告以下保留上一轮记录：其中“读取只打开已有 authority”只说明没有主动发布授权，**不能证明构造器不写库**；接续检查发现 context、suggestions、budget status 仍会触发 chmod/schema/WAL 初始化，现已在 workspace 内 clone 修复。最终全仓 1110/1110、专项邻接 174/174、broker 25/25 及 installed canary 证据以 v0.2 为准。没有复制回原仓或启用 live。

## 结果与边界

已接通可配置的真实 `OpenClawModelAdapter`，不再仅允许 Hermetic adapter。生成前，writer 重验 active mission、已获取原文、exact context、单模型 routing policy 和已有共享预算 policy；模型只给出建议。Cockpit 可以校订陈述、指标、期间与口径，缩小原文引用范围，再由已登录的人明确确认 citation 并保存为 qualitative 待审候选。

没有自动 accept Claim/Thesis，也没有伪造 human actor。`value/unit/scale=null`，数字 authority 仍走 SEC。保存候选不会自动关闭文档 review；原有“登记抽取完成”和“接受并写入正式 Ledger”仍是另外两个人工动作。

**代码可连接真实 broker，不等于已验证真实模型。** 本轮真实付费模型调用为 0。没有取得并核验 extraction 的已批准模型接线及共享 live 预算绑定；没有拿 planner 的现有权限充当 extraction 授权，也没有宣称 live 余额足够。真实模型质量、完整 host/provider 链和真实扣费仍待验证。

## 实现

### 模型与预算

- 外层预算补丁后，全仓 1080/1080、专项 133/133 通过；下面的父审小节明确记录 `a3cb1bd` 原缺口及修复，不把此前 1074/1074 当成完整预算证明。
- 复用 `Scheduler` 的 WorkOrder、lease、ResultEnvelope、`ModelRouter` 的 immutable route/profile、原 model accounting 的 invocation/usage/cost；没有新增队列、路由器或第二份费用账本。
- 在既有 `ThesisImpactBudgetStore` 的 admission/settlement 中增加 mission binding。一次 `BEGIN IMMEDIATE` 同时检查 owner cap 与 mission 的日调用数、日费用上限，然后预留完整 WorkOrder 费用。mission 换版仍按同一 mission 累计；尚未绑定 mission 的共享支出也保守计入，不当成免费额度。
- 日界线为 UTC。跨日尚未结算的预留仍占额度；实际费用未知或只有估算时不释放余额。provider 返回实际费用后，先写 Core usage/cost，再结算预算。解析失败照样记账。
- 单个 exact context 的 WorkOrder 上限固定为：输入 16,000 tokens、输出 3,000 tokens、合计 19,000 tokens、0.05 USD、60 秒。输入按 UTF-8 字节数保守预检，超过上限就拒绝，不默默截断、提预算或换模型。因此高字节密度窗口可能被拒绝。
- 断连或不明调用结果保留完整预留；输出错误为终态，不自动再次付费。lease 过期只走 broker replay，不另发 execute。费用超过预留时保留真实 Core 费用、记录既有 owner alert，并停止后续预算准入，等待 owner 对账。
- 建议读回时再核对 WorkOrder、ResultEnvelope、ModelInvocation、route/profile 和 exact mission reservation。broker 的 actor 是 runtime，不冒充人；建议 producer 是 system。

### 人工 citation 与 staging

- 新 human-only writer op：`stage_document_extraction`。HTTP 复用 Cockpit 的 Tailscale identity、session、CSRF 和 ephemeral human governance RPC；客户端不能自报 actor。
- 原文保持只读。人可在建议给出的原文片段内缩小 start/end；server 逐字符核对原文，重新计算 UTF-8 SHA-256。模型不能生成替代原文或可执行指令，页面使用 `textContent`，不把来源里的 HTML 插入 DOM。
- 原 correction authority 新增显式 `verified_raw_span` 范围：仅在 human 给出 bounded span、exact hash、核对理由且没有 source edits 时，可发布零修正的原文确认。它不是取消原 guard：`targeted_flags/full_document` 仍至少需要一条 correction；新 raw review 不能绑定超出已核对范围的 citation。
- 已有 ASR 修正可填写 exact correction-set 版本和 hash。新建空集或使用旧版本都不能绕过当前未决重叠；已有 accepted correction 与引用重叠时也不能假装原文无需处理。ASR 的 evidence-bound 修正编辑仍用原有 correction authority，本页不另造自由文本改原文的入口。
- 人工提交先在 mission workflow journal 冻结 exact 请求和理由，再依次调用原 correction、citation、CandidateStaging authority。相同 actor/request id 的不同内容被拒绝；跨 Core/staging 的中断可按原请求恢复。局部成功可能留下 citation 或待审候选，但不会留下 accept。
- 人工校订后的陈述再次通过 qualitative closed validator。正式 source verification 和 transcript-only policy 拒绝规则保持不变。

### 父审补强：mandate/governance 不能被 mission 预算替代

父按 [ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md) 第 7 条复核后，确认 `a3cb1bd` 有一处实际缺口：它验证了 active mandate 身份和共享 owner 费用 cap，但没有要求 mandate/governance 明确给出研究预算，也没有在外层强制共享日调用数。此前没有真实模型调用或 live 部署，因此没有据此发生实际超支；这一缺口不能用原有绿色测试掩盖。

补丁在**付费文档抽取准入**增加以下检查，继续使用同一预算账本：

- `MandateVersion.constraints.research_budget` 与 `GovernancePolicyVersion.policy.research_budget` 都必须明确存在，且只包含 `max_daily_paid_calls / max_daily_cost_usd / max_alphaengine_calls_24h`。缺失、非有限数、负数或未知字段都拒绝，绝不把缺省解释成无限额度。
- mission 的三项声明上限必须分别不大于这两个外层上限；不能用一个较大的 mission 预算覆盖较小的 mandate/governance 预算。
- mission、constitution、mandate 和当前 governance 的 ref/hash 必须一致且仍有效；governance pointer 换版使旧 context 失效。外层预算及其 exact 版本/hash 进入 context 和同一 admission 的 binding。
- 在同一个原子预算事务中，外层日调用数和费用按共享账本中的**所有 mission、未绑定的旧记录，以及跨日未结算调用**保守累计。mission、mandate 或 governance 换版均不重置余额。此口径可能提早拒绝，但不能放宽限制。
- 新增跨 mission 并发抢额、跨 mission 成本上限、未知调用跨日、缺失外层 cap、mission 大于外层 cap、governance 换版等回归。它不追溯重写历史 mission，也不声称已把其他 legacy lane 自动迁入新准入路径。

## 配置方案：只安装接线，不创造授权

配置入口是 `control.config.research_review.document_extraction_model_config_path`，必须是绝对路径。已有 macOS renderer 只把它传给 writer 的 `--document-extraction-model-config`，不把 broker 凭据交给 Cockpit。未配置时，原文和人工处理功能保留，生成返回明确的 gated 状态。

该 JSON 是 closed object，必须包含：

| 字段 | 必须指向的既有对象 |
|---|---|
| `routing_policy_ref` | 已批准且只 pin 一个模型的 immutable policy |
| `credential_slot_refs` | 该 policy/profile 已批准的 credential slots |
| `model_router_db` | 已存在的 router authority 文件 |
| `broker_socket`、`broker_client_id`、`expected_agent_id` | 已获准的 broker endpoint、client 与 dedicated agent |
| `broker_auth_key` | 本机既有受保护 key 文件路径；配置中不放 key 内容 |
| `budget_db`、`budget_policy_ref` | 与已有付费 lane 共用的预算账本及有效 policy；不能新建空库假装有余额 |

启用前必须核对模型和来源送模许可、exact policy/profile、credential slot、active mission，以及共享账本剩余额度；还必须有上述 mandate/governance 显式研究预算。当前读取的仓库 P8a mandate 模板没有 `research_budget`，不能据此推断已获付费抽取授权，live 当前版本仍未核验。若现有版本缺字段，须由 owner 通过既有版本化发布流程明确已有上限，并更新 constitution/mission 的 exact bindings；不能在配置文件里伪造外层 cap。当前代码不会自动注册 cap、提高预算、扩 provider/source 允许范围或回退到另一模型。

如补齐接线后做真实 canary，**总量最多 1 次、最多 0.05 USD**，只发送已获准的公开/合成文本或该模型已获准处理的来源；预留必须写入真实共享预算账本，费用记录要可回查。一旦调用、解析、来源或费用验证失败立即停止，不换模型重试。本轮未执行这一步，也未读取真实凭据。

## 验收证据

| 验证 | 结果 | 证据（均在 `temp/p9d3b-continuation/`） |
|---|---|---|
| 全仓 unittest | **1080/1080**，146.270 秒，EXIT=0 | `outer-full-final.log` |
| 专项与邻接 unittest | **133/133**，25.374 秒；与全仓重叠，不相加 | `outer-targeted-final.log` |
| OpenClaw model broker | **25/25**，0 fail | `broker.log` |
| 独立真实本地 adapter/socket 检查 | **1/1**；合成 provider，不是真实模型 | `broker-socket-canary.log` |
| 预算协议 canary | 1 次合成 socket 请求；重复生成不再发请求；50000 微美元预留、1000 微美元合成费用结算 | `outer-budget-canary/result.json` |
| HTTP/writer RPC 与人工 staging | 通过；1 次明确人工 staging，重放幂等 | `http-canary/result.json` |
| 1280/390 浏览器交互 | 两个宽度分别编辑并人工确认；无横向溢出、XSS、页面错误或外部请求 | `browser-final/result.json`、`desktop.png`、`mobile.png` |
| 既有 hermetic research replay | 1/1，0 provider calls | `hermetic-replay.log` |
| wheel/sdist | 外层预算补丁后重新构建成功 | `outer-build.log`、`outer-dist/` |
| 干净 Python 3.13 wheel 安装 | `--no-index --no-deps` 成功；installed HTTP/staging canary 通过，import 来自新环境 site-packages | `outer-clean-install.log`、`outer-installed-http/result.json`、`outer-installed-budget/result.json` |
| 静态检查与安装产物核对 | `compileall`、`git diff --check` 通过；wheel 内 216 个 runtime 文件与最终源码逐字节一致 | `outer-checkpoint-final.json` |

broker 与浏览器组件未受外层预算补丁影响，保留此前通过的 25/25 与 1280/390 交互证据，没有为凑次数重跑；补丁后的真正本地 socket 与 installed canary 已重跑。浏览器 canary 前后正式 Claim/Evidence 均为 0，连接器 invocation 保持 4；仅人工操作新增 2 个 correction set、2 个 citation 和待审候选，Core/staging integrity 均为 ok。以上都是隔离 fixture，不是 live 数据。

补丁后预算 canary 的 Core cost entry 为 `cost-entry:9af441d433f038cf17677a3e27358c8d`，绑定 `usage-entry:912e6d69d0b8982a800b3c1878e7839d`；完整 admission、route、mission binding、settlement 的 ref/hash 保存在 JSON。**1000 微美元来自合成 provider response，不能当真实账单；真实模型调用/花费仍为 0 / 0 USD。**

以下场景已建立自动测试与可重放脚本：

- 专项：source/hash/span、human auth、CSRF、同 key 内容冲突、stale context、budget 并发竞争、零预算、跨 mission 版本与跨日预留、共享未绑定支出、错误输出计费、断连预留、超预留停止、未决 correction 不可绕过。
- `run_p9d3b_budget_canary.py`：真正的本地 Unix socket 与 `OpenClawModelAdapter` 协议交换，但 responder 是合成 provider。报告输出 admission、mission binding、settlement、Core cost refs/hash；不能当作真实模型扣费证据。
- `run_p9d3b_document_extraction_canary.py`：真实本地 HTTP/writer RPC；默认做 human citation/staging、重放与敌对输入检查。浏览器模式在 1280px 与 390px 实际编辑并点击确认，检查 XSS、分页、重载、过期清空、无页面异常及无外部请求。
- 证据目录：`temp/p9d3b-continuation/`。首个 checkpoint 在读取契约后落盘，随后 `checkpoint-02.txt`、`checkpoint-03.txt` 记录实现与失败修复；初次失败日志保留，不拿中间绿色结果代替最终验收。

## 未完成、未做与权限边界

1. 真实模型 canary 未执行：除了 approved extraction 接线与共享余额尚未核验，mandate/governance 的显式研究预算也未在 live 核验。真实模型抽取质量和抗提示注入表现未验证。当前证明的是 deterministic guards 和本地 broker 协议，不是模型总能理解原文。
2. 截图已产出：`temp/p9d3b-continuation/browser-final/desktop.png` 与 `mobile.png`；人工视觉审阅仍未完成。本轮未重试被拒的 `view_image`，未复制图片、绕路读取或修改系统权限。父负责处理权限及独立审阅。
3. 没有 live 数据副本验收；原文、mission、模型输出均使用明确标注的隔离 fixture。没有 live mission/config 写入、部署、服务重启、push、merge 或远端 CI。
4. page 生成是同步、受限 RPC，不是新增后台自动提取器；人工改写的语义是否准确，仍需正式 human review 再决定 accept。中英混合或高字节密度窗口可能因保守输入上限拒绝，不支持默认提限。
5. 仅本地 commit；外部状态由父汇总后处理。

## 复跑命令

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_document_extraction tests.test_document_extraction_admission \
  tests.test_transcript_polish tests.test_transcript_qualitative_candidate \
  tests.test_transcript_candidate_writer_ops tests.test_thesis_impact_budget \
  tests.test_agenda_control tests.test_writer_service tests.test_contracts tests.test_service -v
(cd integrations/openclaw-model-broker && npm run check)
.venv/bin/python scripts/run_hermetic_research_replay_canary.py
.venv/bin/python scripts/run_p9d3b_budget_canary.py --output temp/p9d3b-continuation/outer-budget-canary
.venv/bin/python scripts/run_p9d3b_document_extraction_canary.py --output temp/p9d3b-continuation/http-final
PYTHONPATH=src python3 scripts/run_p9d3b_document_extraction_canary.py \
  --output temp/p9d3b-continuation/browser-final \
  --browser-executable '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
.venv/bin/python -m build --no-isolation --outdir temp/p9d3b-continuation/outer-dist
.venv/bin/python -m venv temp/p9d3b-continuation/outer-clean-install
temp/p9d3b-continuation/outer-clean-install/bin/python -m pip install --no-index --no-deps \
  temp/p9d3b-continuation/outer-dist/dalton_core-0.1.0.dev0-py3-none-any.whl
temp/p9d3b-continuation/outer-clean-install/bin/python scripts/run_p9d3b_document_extraction_canary.py \
  --output temp/p9d3b-continuation/outer-installed-http
```

本机 `.venv` 不含 Playwright；浏览器运行使用已安装 Playwright 的系统 Python，未下载浏览器或改系统权限。干净 Python 3.13 环境使用本地 wheel、`pip --no-index --no-deps` 安装，另外验证 installed package 路径和 HTTP/staging canary，不以 editable import 冒充安装验收。
