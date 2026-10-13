# 外国发行人年度原件与正式三表输入：1b annual

本包增加通用 20-F/40-F 年度原件链，不按公司 ticker 特判。强 SEC submissions profile 必须闭合公司、issuer、discovery、query、CallSpec、invocation、envelope 与原始字节；只选择原始 annual 且 isXBRL=1 的完整 descriptor。自动调度先读原年度，不把 amendment 当独立年份；6-K/6-K/A 仍是披露材料，不能视为季度财报。

新 v6 批准明确增加 foreign annual 输出范围。原 v2–5 输出、批准与原件保持；旧本地 amended reparse 保留 v5 批准和请求，不换成 HTTP。owner 仅对已知精确 DDL 扩展 dispatch/filing 枚举，保留 rowid、全部行、hash、触发器和外键；未知 DDL/index/trigger 拒绝。新 native request/evidence 两表 append-only。

child 必须读 owner 已登记的 exact request，重新验证当前 mission/source 权限后才取原件。保存完整 attachment index，以及 primary/XML/XSD/HTML/text/PDF 源成员的精确字节、身份和 hash；同一 raw parse 还保留原 submissions bytes、原 v6 批准与批准时点。不得跨 issuer/accession/URL，缺附件、原件、实际年度收入/资产/现金流角色或证据漂移均拒绝。来源级别仍是已有获批 edgartools public-HTTP parser，并非独立 Dalton transport lease 验证；本包不夸大该原有信任边界。

正式读取再从原 parse/document-set/profile 重建 lines 与证据；context 和普通 model input 保留原 IFRS concept、dimensions、单位与 duration。年度总量不改名为 10-K，也不凭 annual 填造季度。已完成原件不因 profile/日历变化重复取得。foundation 仅在强 profile 下改该公司的 annual 表单目录；只解析三表时 acquired/read 仍为 0，实际 acquired/read 原件才满足相应资料项。

## 兼容性证据

旧 sec_financials_normalise.py、sec_filing_units.py、statement_companyfacts_context.py、statement_context.py 文件未修改。固定 baseline 4a2668cb 的 v2–5 schemas/identities 和国内 normalized wire 逐字相等。normalizer_hash 仍为 d9195918cc645eff3f8a0452fc31df46e78c000afccb32100300150d5d54343a；黄金测试同时保护其 source-wide 身份。A 的独立 unit-source alias 包未覆盖。

## 现存农业环境：正常部署时的批准与 selector

下面为正常治理入口，不在运行中热改。先 preview，保留 JSON binding，quiet 部署批次中 apply；该工具不 reload/bootout/restart 服务、不改旧 v5 文件或数据库记录。

```sh
python -m dalton_core.sec_native_statement_governance \
  --manager /Users/everflow/.dalton/manager.json \
  --environment ws-624b3f2ba8f1f81727ae7388 \
  --approved-by human:shared-46217623efa279c5b31604e5dd65485e
```

批准 apply 使用同一参数加 `--apply --expected-binding-json '<preview.binding 的完整 JSON>'`。当前农业 preview 已强核 workspace/catalog/prior-v5/source 四方绑定，结果 eligible，production writes/model/HTTP/service actions 均 0。

v6 文件新增于 `state/dalton-core/connector-governance/sec-financial-statements-v6.json`；原 v5 保留。批准落盘后，同一入口加 `--sync-writer` 默认 preview；正常停靠后加 `--sync-writer --apply --expected-plist-sha256 <preview.before_sha256>`，只 CAS 替换 installed writer 的一个 `--statement-lane-governance` 参数，保存原 plist/receipt，不 reload。必须在官方部署中启用新 selector 后再启动 writer；release-only 更换解释器不会替换既有显式 v5 参数。owner schema 扩展由正式 writer 的 CoverageMissionAuthority 迁移，不另进程写 Core。新环境初始完整 source 批准默认 v6。

## 真实只读判据与剩余边界

2026-10-13 05:34:46Z，NTR 强原根 discovery a3682965…、envelope be9cdd0e…、raw a3eda661… 闭合，8 个原 40-F XBRL annual 可选。最新 0001193125-26-081326 / filed 2026-02-27 / report_date 2025-12-31 / d56746d40f.htm。0.051 秒/query_only=1/changes=0，未做 HTTP 或获得真实附件，不能声称 NTR 财务已解。

部署后的验收应通过正常 lane 取得真实原件，检查 durable reader/context/三表 input，再记录真实剩余缺项。IFRS→模型 role 映射、非 XBRL 6-K 的季度资料、短年度 transition context 尚未支持，不猜季度/零值。若 parser 返回 opaque unit id，旧 sec_filing_units 尚不消费新 native document-set，旧非 10-K unit gap 也仍写 sec_10q；该原件/单位接线为紧接本包的独立通用修复，不能将当前年度读取能力宣传为完整预测可用。
