# 外国发行人季度原件与正式财务输入

当前调查基线为 `3d7a00af`，生产取证只读，未调用模型或 HTTP。

## 实际状态与已确认缺口

NTR 已有正式 40-F 年度来源：`0001193125-26-081326`，报告期末
`2025-12-31`。原 statement filing 账本保留 558 行；后续强单位处理后的
正式 statement context 为 560 行。二者是不同层次的原件/投影，不能改写为
同一个原始行数。现有正式 spec 是
`company-model-spec:b5dc21074c913723724b6fe1732e2456`。
实际完整预测回放的收入、利润公式均没有季度历史，`ready_for_forecast=false`。
这不是已完成季度模型。

强 submissions 缓存 `279a30c3…28a14` 含 221 个 6-K、2 个 6-K/A，
全部 `isXBRL=0`。因此仅给 XBRL parser 增加表单枚举没有实际数字入口。
首次有界 material inventory 达到 65 条上限，不能据此宣称所有持有材料都没有
季度财报。这个样本只证明现有正式 statement ledger 没有 6-K 季度来源。

`mission_statement_lane` 的外国分支只排年度；年度已持有时仍跳过国内分支，
没有后继 interim 原件入口。`sec_financials_cli.parse_live` 在 `xbrl() is None`
时直接跳过 filing。年度 v6 的授权、文档集合同和 SQL 表单范围均明确不包含
6-K 的正式财务数字，不能冒充旧审批已授权。

另一个闭环缺口是 `forecast_structure_binding` 在强重建 replay 后拒绝
not-ready，但其 `FinancialStatementStructureError` 不是 CLI 当前材料缺口
异常分支；lane 已有 generic 材料 fallback，但没有结构化季度缺项，不能把
该 fallback 的存在说成季度原件获取已经闭合。
正常材料路由 `SEC_SPECS` 也没有 `sec_6k`，而 data 类型请求固定转 8-K。

## 最小分拆与交付边界

第一步是**可正常调度的原件入口**，不是财务建模完成：

1. 在显式新版 owner 合同下选择强 reporting profile 原 submissions 内的
   6-K/6-K/A；form、filed、SEC reportDate、accession 原样保留，
   `reportDate` 绝不被用作季度期末。profile/current mission/source 权限独立
   重验；旧 v6 年度请求与费用不改。
2. 每公司每 tick 至多一个原件文档集，沿既有 single-slot、预算和队列公平门。
   原完整 attachment index 与同 accession 下的 HTML/XML/TXT/PDF 原件一起
   保存；使用已批准 edgartools 公共 SEC reader，精确核 URL、issuer、accession、
   filename、字节/大小上限和 SHA。不遍历其它目录或猜附件文件名。
3. owner append-only 文档集/读完分类记录与稳定 source hash 绑定。
   无财务内容、缺附件、不可读取的材料分别有显式结果；同原件不每 tick 重买。
   公共 filing descriptor 和标题只是获取候选，不是季度财务证明。
4. 在完整 strong replay 已确认缺季度时向普通 planner 发有界、稳定的
   document 请求，选 `sec_6k`/当前 foreign plan；不把未知日期伪成具体季度。
   这条资料入口保留原 model failure/paid Work，不发新 spec 作为“资料修复”。

第二步才是**原 cell 到正式三表输入**：

1. 复用 A 的 `operating_source_columns.table_measurements(raw, original_text)`
   强 cell/row/colspan/rowspan/column period 证明，不另写第二套表格 parser。
   其 0.1 是完整 XHTML 边界；普通不完整 SEC HTML/PDF 必须使用独立版本的
   正常读取/独立核验路径，不能悄悄放松旧规则。
2. 内容明确发行人、实际报表标题、期间起止、币种/缩放和三表身份后才登记。
   原表行使用 document/table/row 身份；未标记行不靠 label 冒称 IFRS QName。
   模型可以在正常语义阶段显式选择这些原行和角色，随后完整历史/DAG/三表
   tie-out 再决定可执行性。
3. 同一附件的三个月利润表与时点资产负债表可形成该季度；cash YTD 原值保持
   YTD，仅既有合法同口径差分可以产生现金季度。半年报仍是半年报。
   diluted EPS 与 weighted-average shares 必须原直接季度栏，不做累计差分、
   年度四分或均值补齐。没有完整披露的期间输出精确普通资料缺口。
4. 新 v7 output/owner 迁移保 form 6-K，SEC reportDate 与财务 period 分离。
   旧 domestic/v6 source、wire、hash、normalizer 语义原样；新增代码独立 helper，
   避免整个旧 normalizer 文件 hash 改变造成所有国内公司无关重算。

在取得真实 NTR 附件并通过正常 acquisition → strong 原件 → 独立 cell 核验
→ owner ledger → statement context → 模型历史执行前，不宣称 NTR 季度已解决。

## 变更面与测试

独立 interim request/document-set helper 与 owner schema；v7 governance/schema；
statement launcher/CLI/source lane 的显式原件分支；normal material routing；
forecast not-ready typed gap。正式非 XBRL line/role 接口在 A cell 合同冻结后单独
接入，不与 C 的 company model spec/repair 文件混编。

测试必须覆盖两发行人、6-K 非财务/半年/季度内容区别、6-K/A 不猜 amendmentOf、
错 issuer/accession/profile/attachment/hash/period/unit，缺任一三表、混币种/缩放、
缺原季度 shares/EPS、YTD 不冒充季度、坏/缺行不当零、cursor 重启/公平/重复获取，
旧 v2–6 golden 与 normalizer hash，完整保存重读强核验，not-ready 普通请求幂等。
真实离线样本优先由正常受控 source lane 取得，不手动触发 provider 或 HTTP。

## 取证路径

- `/tmp/root-v01143-ntr-forecast-replay-ro-r01-20261013.json`，SHA
  `539f3538bd55fb8ce673a810faca74d21101ccfa8554ef694cc1325eabe9e8c8`。
- `/tmp/ar-ntr-native-quarter-source-inventory-ro-r01-20261013.json`，SHA
  `a8ce0756891bcd5f2464adcf82858eab229626170f5ba39ce7ce9ecb38babd25`。
- `/tmp/root-ntr-native-source-inventory-ro-r01-20261013.json` 提供已持有
  submissions 原缓存路径与原 SHA，未请求外网。
