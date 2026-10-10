# 分部桥、XBRL 身份与旧上下文补全

技术设计与验收摘要，2026-10-10。隔离测试和历史回放用于核对合同与兼容性；不等同于当前公司研究已完成。原始研究材料、运行账单及逐文件收据私有留存。

分部桥不闭合即 FAIL，记录左右差额、未调节项和缺失成员。分部按披露概念/完整维度映射，匿名金额不得按大小/排序赋予身份；跨轴合计碰巧相等不能证明相同覆盖范围。公司明确声明的分部 scope 冻结 axis、固定维度、成员及实际消费期间，避免不同披露划分混加；漏声明成员仍拒绝。

SEC v4 保留完整 dimensions。旧 nullable dimensions 迁移不改变旧 wire/hash。context authority 追加来源版本与明确拒绝，重验持有 raw 哈希、CIK、accession/header、normalizer 合同及映射哈希。

旧 v2/v3 可按 ordinal 逐字段字节相等时附完整身份；旧去重导致无法一一证明时保存独立完整 snapshot 和独立 line refs。原财务行不更新。新模型 proof 钉定具体版本，旧 proof 使用原 shape，不跟随最新 context。

历史 header-only filing hash 只证明 metadata，不能替原 numeric rows 背书。即使库里有强 snapshot，旧 numeric proof 也不会自动认证；实际消费必须明确绑定该 snapshot 的确切行。重新验证来源后追加完整 snapshot 是合法修复入口。

历史 63 份申报隔离回放得到 10 份 ordinal overlay、53 份 snapshot，恢复匿名分部身份；仍不闭合/缺成本/粒度重叠的桥如实 FAIL。合成测试覆盖相同金额不同身份、不同轴、舍入容差、缺成员、篡改、旧 proof、幂等与默认 dry-run。

`statement_context_cli` 默认只读，明确 `--apply` 才追加 context。新来源改变 inputs_hash，后续规格/模型须正常更新。v4 normalizer 仍按治理批准和精确哈希选用，不能用 proposed seed 冒充批准。
