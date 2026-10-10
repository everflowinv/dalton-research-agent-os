# SEC 年报附件的独立来源证明

技术设计与验收摘要，2026-10-10。隔离测试和历史回放用于核对合同与兼容性；不等同于当前公司研究已完成。原始研究材料、运行账单及逐文件收据私有留存。

10-K 主文与 Exhibit 13 使用独立文档身份。附件绑定原 SEC URL、发行人 CIK、accession、原始字节、获取 manifest、artifact 和 connector receipts。禁止把拼接正文哈希写回主文，或用附件替换已冻结的主文窗口。

`verified_exhibit_source` 检查主文类型和原始引用、URL/CIK/accession、获取身份及完整字节。缓存缺失、原始篡改、借用 receipt、跨发行人或自重哈希的来源替换均拒绝。窗口、全文和证据索引共用这一证明。

正常索引与提取在既有使命/公司/预算下登记独立 acquired document 和 review，每轮最多登记两份，复用真实既有获取 ticket，不制造新的成功来源记录。来源、索引、提取和核验专项 147 项通过，含一项既有跳过。

持有 IBM 原件的只读回放保留主文与附件各自身份/哈希。运行已发现独立附件，但发现/登记不等同于附件研究核验已完成；正式结果须从对应 source context 和 review 读取。

设计见 [ADR-0024](../adr/0024-independent-sec-annual-report-attachment-original.md)。
