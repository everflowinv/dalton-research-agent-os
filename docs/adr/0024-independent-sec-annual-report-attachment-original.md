# ADR 0024 — SEC 年报附件的独立原始文档

状态：已实现并部署；实际产物完成情况与长期观察单独验收。
日期：2026-10-10。

10-K 主文和被其引用的 Exhibit 13 分别拥有文档身份。后者沿用已有 `sec:exhibit:<accession>:<filename>` 身份，绑定原 SEC URL、发行人 CIK、accession、原始字节哈希、已记录的 fetch manifest、artifact 和 connector receipts。不得将拼接正文的哈希放回原 10-K，也不得用附件替代已经冻结的主文读取窗口。

现有 evidence corpus 枚举缓存的附件；正常提取车道在授权 mission/company 范围内将其登记为独立 acquired document 和独立 review。登记复用已经完成的获取 ticket，不伪造新的获取成功事件或来源引用，每轮最多登记两份新附件，已登记项不占后续新附件名额。原始读取仍逐次验证来源。

共同 `verified_exhibit_source` 复核主文确为 10-K、主文原始链接确实指向该附件、SEC URL 与 CIK/accession 一致、成功缓存与原始获取记录相符，并重新哈希 receipt、原始 artifact 和全文。窗口和全文提取共用该入口，证据索引共用同一证明。缓存缺失、原始字节篡改、借用 receipt、不同发行人或自重哈希后的来源替换均拒绝，不能降级为主文。

IBM 已持有原件的只读回放保留原主文与附件各自身份/哈希；未下载、未调用模型、未登记生产 review。正常运行已发现独立附件；索引、review 和核验终态分别追踪，发现或离线回放不冒充已发布研究结论。
