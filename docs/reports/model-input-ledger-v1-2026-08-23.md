# Model Input Ledger v1

日期：2026-08-23

## 结果

本切片建立研究前所需的最小模型台账，不实现通用 Excel 引擎。Core 现在可以保存四类正式输入：

- actual：公司或业务线、指标、期间、calendar、单位、币种、数值，以及 exact Evidence/Claim version；
- scenario：人类 owner、情景说明和可选的 base-scenario version；
- assumption：driver、有效期、value 或 formula、scenario、owner、rationale，以及 source 或 judgment provenance；
- forecast line：预测期间、value 或 formula、scenario、历史 actual 和全部依赖版本。

研究 worker 只能写 candidate。准入必须由 writer service 认证的 `human:*` principal 决定；decision、input version、
current pointer 和 event 在同一个事务内写入。并发 candidate 在 decision 前若已落后于 current pointer，会 fail closed。

## 模型运行与勾稽

Model run 保存冻结的 input refs/hashes、scenario、formula version/hash、输出、错误和执行时间。运行输入必须包含
forecast/assumption 的传递依赖，且所有 scenario binding 必须与 run scenario 一致；因此外部计算器不能只提交结果而
省略输入闭包。

Reconciliation 固定覆盖六项：报表勾稽、单位/币种、期间/calendar、share count、actual override、source revision。
每个适用检查必须携带 exact authority version/hash。若 input 已被新版本替代，或 input 内绑定的 Evidence/Claim 已有
新版本，`source_revision` 必须为 fail；预测期已出现 actual 时，`actual_override` 必须为 fail。

Valuation output 目前保持关闭式准入：price、shares、FX、rates、consensus 五类 authority 必须分别绑定不同的
正式 actual input，缺一项就拒绝 model run。这与当前 US price/shares/FX/rates/consensus connector 缺口一致；本切片
不会用缓存或 agent judgment 冒充这些数据。

## 存储与 RPC 边界

- 所有 authority row append-only；只有 pointer 可在专用 transaction authorization 内更新；
- Model Input Ledger 使用独立 SQLite authorization UDF，普通 `DaltonStore` 写事务不能越权写模型表；
- bootstrap 和 writer owner process 会初始化新 schema；schema 已进入 wheel package data；
- writer RPC 新增 candidate、human decision、Core run/reconciliation、只读查询和 integrity report；
- 本切片没有迁移 live database、没有部署、没有写 actual/assumption、没有调用 connector 或模型。

## 后续顺序

下一切片回到 US IT Services 行业 evidence pack：先用 SEC/AlphaEngine/Gemini+fetch 建行业 driver 和 KPI evidence，
再以 ACN 作为 company overlay 写入 candidate。正式 valuation 继续等待 price/shares/FX/rates/consensus authority。

## 验证

- Python 全量回归：704/704；
- 新增 Ledger 专项：8/8；writer RPC 专项随全量回归通过；
- `compileall`、`git diff --check` 通过；
- Python 3.13 sdist/wheel 构建通过，wheel 内含 `model_input.py` 和 `model_input_schema.sql`。
