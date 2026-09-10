# Linde SOG Backlog 项目级 ROIC 模型 v1

> **任务：** linde:sog-backlog-roic:v1（task 4，P0）
> **证据截止：** 2026-08-19
> **数据基础：** Linde 2026Q2 Form 10-Q（filed 2026-07-31）、2025 Form 10-K（filed 2026-02-25）、2026-07-31 业绩发布与电话会材料、Linde 官网项目公告、公开新闻交叉验证；卖方观点沿用 deep-insights-v1 的 AlphaEngine 样本（本轮 AlphaEngine 检索返回额度上限错误，未刷新，见 §8 局限）。
> **前置材料：** `deep-insights-v1.md`（Gate 已通过，投资观点 `UNDER_REVIEW`）；本文件是 Gate 结论中 P0 任务的执行件。

---

## Direct answer

**81 亿美元 SOG backlog 是"在建大型装置的预计资本成本"，不是收入、也不是全部承诺管线；按管理层 20–50% 的 capex→年销售转化口径，全 backlog 满产对应年销售额约 16–40 亿美元、EBITDA 约 6.5–20 亿美元，税后 NOPAT 约 1.8–12 亿美元，折合 EPS 约 0.40–2.65 美元/年（占 2026E 指引 2.2%–14.9%）。** 转化是逐步的：2026 年内约 13 亿美元项目投产，对 2027E EPS 的满年贡献在保守/基准/乐观三档下仅 0.06/0.18/0.43 美元（占卖方 2027E EPS 增量的 3%/9%/20%）。**backlog 是 2028–2029 的增长引擎，不是 2027 年 EPS 的主要来源；项目级 gas-on、爬坡与真实 IRR 公开披露不足，公司"双位数 IRR"口径无法独立审计——模型在基准假设下（30% 转化、45% EBITDA 率）隐含的 20 年无杠杆 IRR 约 8.4%，仅乐观档（50%/50%）达到 18.6%，因此"双位数 IRR"成立与否取决于每个项目落在 20–50% 转化区间的哪一端和合同价格条款。**

对 thesis 的影响：**LIN-T2（电子+backlog 驱动 2027–2029 增长）保持 open/medium，方向性成立但 2027 年贡献有限；LIN-T3（高 capex 高估值）风险确认——backlog 转化对 2027E EPS 的可见增量不足以支撑高起始估值，除非 gas-on 提前或转化率落在区间上沿。** 投资观点维持 `UNDER_REVIEW`。

---

## 1. 口径：backlog 是什么、不是什么

| 项目 | 数值 | 口径 | 来源 |
|---|---:|---|---|
| SOG backlog（2026-06-30） | 约 81 亿美元 | **在建大型装置的预计总资本成本**（sale of gas backlog of large projects under construction） | 2026Q2 10-Q |
| SOG backlog（2025-12-31） | 约 73 亿美元 | 同上 | 2025 10-K |
| SOG backlog（2026-03-31） | 约 71 亿美元 | 同上（deep-insights 记录 71→81） | deep-insights-v1 |
| SOP backlog（2026-06-30） | 约 30 亿美元 | 设备销售类（非 SOG） | 2026Q2 10-Q / 业绩材料 |
| 剩余履约义务（RPO，2026-06-30） | 约 640 亿美元 | 未来最低采购承诺 + 工程设备销售，**不含超最低采购的 on-site 增量销售**；约一半在未来 6 年内实现，合同最长 30 年 | 2026Q2 10-Q |

关键推论：

1. **SOG backlog 是流量管道，不是存量承诺池。** 它是"正在建设的装置"的资本投入，装置投产即离开 backlog、开始产生气体销售收入；RPO（640 亿美元）才是含未来最低采购收入的总合同视野。
2. **backlog ≠ 未来收入。** 81 亿是 capex 金额；收入需按转化率折算。管理层给出的转化口径为 capex 的 **20–50% → 年销售**（Q2 slides，经 investing.com 转述；需在原始 slides 复核，见 §8）。
3. 公司口径：SOG 项目代表**有长期供气协议支持**的资本投资，回报表述为"合同现金流 + 双位数 IRR + 高质量客户 + 网络密度提升"（IRR 为管理层口径，无法独立审计）。

---

## 2. Backlog bridge（FY2025 → 2026Q2）

```
2025-12-31 backlog   ~7.3B
2026-03-31 backlog   ~7.1B   （投产 > 新签，Q1 净减）
2026-06-30 backlog   ~8.1B   （+1.0B；主要来自美国电子新签）
```

- **Q2 增量驱动：** 电子终端新签——Phoenix ~$1B + 台湾（Linde LienHwa JV）~$0.8B，合计约 1.8B 的同一大型半导体客户订单；扣除同期投产与汇率后净增约 1.0B（公司电话会：backlog 增加 10 亿美元，主要来自美国电子赢单；深洞见与 Citi 交叉核对）。
- **2026 年内预期：** 公司预计 2026 年内（主要在 H2）投产约 20+ 个项目、对应约 13 亿美元投资；**即使扣除这些投产，2026 年末 backlog 仍保持"8 字头"**（约 80 亿美元+），即新签需继续覆盖投产。
- **构成（Q2 slides，经 investing.com 转述）：** 区域——Americas 75%、APAC 20%、EMEA 5%；终端——化学品 56%、电子 22%、制造 14%、金属矿业 7%；**约 57% 与清洁能源应用（氢、CCUS、可再生）相关**。

---

## 3. 项目级追踪表（本次任务三组）

### 3.1 Phoenix（美国亚利桑那州，TSMC Arizona）

| 维度 | 内容 | 来源/置信度 |
|---|---|---|
| 投资额 | 约 **10 亿美元**（本轮新增）；此前 2021 年首期约 6 亿美元（3 座 ASU） | Linde 官网 2026-07-31 / 2021 公告；high |
| 内容 | **2 座新增 SPECTRA® ASU** + 配套基础设施，与现址 3 座 ASU 互补；生产超纯氮/氧/氩 | Linde 官网；high |
| 客户 | 同一"全球最大半导体制造商"——公开报道确认为 **TSMC Arizona**（Phoenix 北区园区）；Air Liquide 亦供氢/氦/CO₂（非独家） | 多源新闻交叉；high（客户为媒体确认，Linde 官方未点名） |
| 支持对象 | TSMC Arizona 两座新 fab（园区内） | aztechcouncil/新闻；medium-high |
| gas-on | **未披露具体日期**；园区内已有 2021 年首期装置（原计划 2022H2 投产），本轮为扩产 | 未知项 |
| 状态 | 已签约、在建（计入 backlog 81 亿） | high |

### 3.2 台湾（Linde LienHwa JV）

| 维度 | 内容 | 来源/置信度 |
|---|---|---|
| 投资额 | 约 **8 亿美元** | Linde 业绩材料/新闻；high |
| 内容 | 多座 ASU + **制氢装置**，覆盖台湾多个厂址 | 新闻交叉；medium-high |
| 客户 | 同一半导体客户（TSMC），供应**新 fab 与先进封装**设施 | 新闻交叉；medium-high |
| JV 结构 | Linde LienHwa 为 Linde 在台合资公司（持股比例未在本次检索确认） | 未知项 |
| gas-on | **未披露** | 未知项 |
| 状态 | 已签约（计入 backlog） | high |

**合并口径：** Phoenix + Taiwan 合计约 18 亿美元，是 2026Q2 backlog 净增（+10 亿）的主要来源，也是"电子 22% 占比"的增量来源。

### 3.3 低碳氢 / CCUS（backlog 约 57% 清洁能源）

| 项目 | Linde 角色/投入 | 时点 | 状态与风险 | 来源/置信度 |
|---|---|---|---|---|
| **Blue Point One 低碳氨，路易斯安那州** | Linde 投资**超过 4 亿美元**，建设、持有并运营现场 ASU，长期供应氧气和氮气 | 2026-08-26 项目整体开工；目标 2029 年 gas-on | Linde 2025Q2 已将 `OCI (Blue Point One)`列为 SOG backlog major project；本次开工不是新增 backlog。项目级 capex 曲线、ASU 独立工程进度、2029 年内具体启动月和合同回报未披露 | Linde 2025-06-23公告与2025Q2 slides；CF Industries 2026-08-26开工公告；high（当前81亿美元backlog内的逐项确认仍为medium-high） |
| **OCI（现 Woodside）Beaumont 蓝氨，德州** | Linde 约 **18 亿美元**投资建清洁氢+CCS 供氢装置（2023 公告） | 原计划 2026H2，**Woodside 已提示推迟至 2027 年初** | ATR 第三方供应商工期 + ExxonMobil CCS 设施进度拖累；Woodside 2024 年收购 OCI 装置 | Linde 2023 公告 + enkiai/latitude；high |
| **bp × Linde 德州 CCS** | 大休斯顿地区 CCS，支持 Linde 现有装置低碳氢 | 潜在 2026 启动 | 与 Beaumont 氢装置联动 | bp 官网；medium-high |
| **ExxonMobil CCS（Linde Beaumont 配套）** | Exxon 承运并封存 Linde 清洁氢装置 **至多 220 万吨 CO₂/年** | 预期 2026 启动 | 是 Beaumont 氢项目按期投产的**外部依赖项** | Exxon/latitude；medium-high |
| ExxonMobil Baytown 低碳氢（**非 Linde 项目**） | 无（read-across） | 2025-11 暂停 | 证明低碳氢**客户长期合同意愿不足**——行业需求侧风险 | 新闻；high（仅作背景） |
| IEA 全球视角 | — | — | 2030 已宣布项目库 49→37 Mtpa；达 FID 仅约 4.2 Mtpa（~9%） | IEA GHR 2025；high |

**项目级判断：** 清洁能源在 backlog 中占比最高（~57%），但**最大单项目（Beaumont）已被客户明确推迟至 2027 年初**，且投产依赖 ExxonMobil 的 CCS 设施——这是 backlog 转化时点风险最集中的区域。低碳氢/CCUS 按期权处理（LIN-T4 维持），不按无风险增长储备计入模型基准。

---

## 4. 转化模型（Dalton 假设，显式标注）

### 4.1 假设

| 参数 | 保守 | 基准 | 乐观 | 依据 |
|---|---|---|---|---|
| capex→年销售转化率 | 20% | 30% | 50% | 管理层 20–50% 区间（Q2 slides 转述） |
| 新增销售 EBITDA 率 | 40% | 45% | 50% | on-site 工业气体通常 40–50%（口径见 §6 局限） |
| D&A | 5% of capex/年 | 同左 | 同左 | Dalton 假设（25–30 年装置寿命粗算） |
| 维护 capex | 1% of capex/年 | 同左 | 同左 | Dalton 假设 |
| 税率 | 23.9% | 同左 | 同左 | 2026Q2 调整后 ETR（10-Q） |
| 投产节奏 | 2026 年 13 亿；2027–29 年均约 22–23 亿（合计消化 81 亿） | 同左 | 同左 | 公司 2026 年 13 亿投产口径 + Dalton 平滑假设 |
| 股数 | 4.645 亿（2026Q2 稀释后） | 同左 | 同左 | 10-Q |

### 4.2 满产静态结果（全部 backlog 投产并爬坡完成）

| 指标 | 保守 | 基准 | 乐观 |
|---|---:|---:|---:|
| 满产年销售（$B） | 1.62 | 2.43 | 4.05 |
| 满产 EBITDA（$B） | 0.65 | 1.09 | 2.02 |
| 税后 NOPAT（$B） | 0.18 | 0.52 | 1.23 |
| 无杠杆 FCF（$B） | 0.51 | 0.85 | 1.56 |
| 稳态税后 ROC（NOPAT/投入） | 2.3% | 6.5% | 15.2% |
| 无杠杆 FCF yield | 6.3% | 10.5% | 19.2% |
| **20 年无杠杆 IRR** | **2.3%** | **8.4%** | **18.6%** |
| 15y / 25y IRR | 0.0% / 3.8% | 6.3% / 9.3% | 17.5% / 19.0% |
| 满产 EPS 增量（$） | 0.40 | 1.13 | 2.65 |
| 占 2026E 指引（17.80） | 2.2% | 6.3% | 14.9% |

### 4.3 2026 年投产（约 13 亿）对 2027E 的贡献

| 指标 | 保守 | 基准 | 乐观 |
|---|---:|---:|---:|
| 2027 满年 EPS 增量（$） | 0.06 | 0.18 | 0.43 |
| 占卖方 2027E EPS 增量比重* | 3.1% | 8.7% | 20.4% |

\* 分母：Bernstein 2027E 19.89 − 2026E 指引中值 17.80 ≈ 2.09 美元（卖方样本 2027E 为 19.45–20.02，见 §7）。

**关键结论：2026 年投产的 13 亿项目即使在乐观档也只解释 2027E EPS 增量的约 1/5；2027 年 EPS 增长主要靠存量业务价格/生产率/回购，不是 backlog。** backlog 的 EPS 贡献集中在 2028–2029（累计 gas-on 达 58–81 亿后）。

---

## 5. backlog-to-EPS/FCF 时间路径（基准档示意）

| 年份 | 当年 gas-on（$B） | 累计 gas-on（$B） | 满产折算销售（$B）* | 增量 EBITDA（$B）* | 增量 EPS（$）* |
|---|---:|---:|---:|---:|---:|
| 2026 | 1.3 | 1.3 | 0.39 | 0.18 | 0.06（部分年） |
| 2027 | 2.25 | 3.55 | 1.07 | 0.48 | 0.18（满年） |
| 2028 | 2.25 | 5.8 | 1.74 | 0.78 | 0.40 |
| 2029 | 2.3 | 8.1 | 2.43 | 1.09 | 1.13（满产稳态） |

\* 转化率 30%、EBITDA 率 45%、税率 23.9%；"增量 EPS"为**当年新增 gas-on 部分的满年贡献**，非累计（累计稳态见 §4.2）。投产→满产爬坡通常另有 1–2 年，实际 EPS 贡献比该表更平缓。

---

## 6. 模型局限（必须声明）

1. **转化率口径不确定：** 管理层"20–50%→年销售"是否含能源 pass-through 未在本次检索确认。若含 pass-through，新增销售 EBITDA 率应低于 40–50%（pass-through 近零毛利），满产 EBITDA 和 IRR 将低于上表；若不含，则上表偏保守。
2. **稳态 ROC 不可与公司 23.5% 直接比较：** 公司税后 ROC（2026Q2 23.5%）是对**全部存量资本**（含大量低账面值旧资产）计算；本表是对**新增 gross capex** 的满产回报。新增项目 ROC 低于公司混合 ROC 是正常结构，不构成公司失信证据。
3. **IRR 是模型输出不是公司数据：** 公司"双位数 IRR"为管理层口径，无逐项目数据可审计。基准档 20 年无杠杆 IRR 8.4% 低于"双位数"——若公司口径为含杠杆/含价格条款的真实项目 IRR，则需转化率或合同价格条款落在区间上沿才能成立。
4. **gas-on、爬坡、合同期限、最低采购比例、逐项目收入均未披露**（deep-insights-v1 §12 未知项 1–2 维持）。
5. **本轮未刷新卖方观点：** AlphaEngine 检索返回额度上限错误（E_UPSTREAM_API: 无查看权限或额度已达上限），卖方 EPS/目标价沿用 deep-insights-v1 的 2026-07-31/08-02 样本。

---

## 7. 与卖方/共识的桥

| 机构（日期） | 2026E EPS | 2027E EPS | 目标价 | 方法 |
|---|---:|---:|---:|---|
| J.P. Morgan（2026-08-02） | 17.90 | 19.45 | 530 | 19.1x 2026E EV/EBITDA |
| Bernstein（2026-07-31） | 18.02 | 19.89 | 559 | 31x prospective P/E |
| Citi（2026-07-31） | 18.05 | 20.02 | 600 | — |

- 公司 2026 指引：调整后 EPS **17.70–17.90**（上调下限，中值 17.80）；2027 年公司指引口径为"8–12% 增长，不依赖宏观改善"（电话会）。
- 卖方 2027E 隐含 EPS 增量约 1.65–2.22 美元；本模型基准档中 2026 投产项目仅贡献约 0.18 美元（~8.7%），**缺口需由存量业务价格/生产率/回购与 2027 年新投产共同填补**——这反过来要求 2027 年 gas-on 比 2026 年更快，或转化率更高。

---

## 8. Falsifier 与下一项验证

**本模型/backlog 叙事的 falsifier：**

1. 2026 年末 backlog 跌破 80 亿且新签不能覆盖投产 → "8 字头"预期落空，增长叙事后移；
2. 2027 年 gas-on 明显慢于 2026 年 13 亿节奏，或 Beaumont 等大项目再度推迟超 6 个月 → 转化时点风险兑现（LIN-T3 加强）；
3. 项目投产后 4–6 个季度 ROC 不改善、ROPO 口径年销售增长持续低于转化模型下沿 → backlog 实际转化率低于 20%；
4. 电子终端基础销售转负或 TSMC Arizona/台湾 fab 扩建延期超 12 个月 → LIN-T2 削弱。

**下一项验证（按信息增益排序）：**

1. **复核 Q2 slides 原始文件**：确认 20–50% 转化率、57% 清洁能源、75/20/5 区域和 56/22/14/7 终端的原文与口径（本轮为新闻转述）；
2. **TSMC Arizona Fab 2/3 与台湾厂扩建官方进度**：从 TSMC IR/新闻核对 fab 建设-设备安装-gas-on 时间线，映射到 Linde ASU 投产；
3. **Beaumont/Woodside 季度更新**：跟踪 ATR 工期、ExxonMobil CCS 投运与 2027 年初供氢目标；
4. **每季 10-Q**：登记 backlog 净变动 = 新签 − 投产 − 取消/汇率，区分"投产消耗"与"取消/延期"；
5. 若 AlphaEngine 额度恢复：刷新 JPM/Bernstein/Citi 对 backlog 转化与 2027–28 EPS 的项目假设。

---

## 9. 决定与 thesis 更新建议

- **LIN-T2（bull，open，medium）：** 方向性支持（Phoenix/Taiwan 合计 18 亿、电子 +18%、backlog 净增 10 亿），但**2027E 可见增量有限**，维持 open，不升 confidence。
- **LIN-T3（bear，open，high）：** 本模型强化其风险逻辑——backlog 对 2027E EPS 的贡献在基准档仅 ~0.18 美元，高起始估值下"8–12% 增长"若主要靠存量业务而非项目，倍数消化风险仍在。
- **LIN-T4（neutral，open，medium）：** Beaumont 推迟至 2027 年初 + ExxonMobil Baytown 暂停，确认按期权处理，不纳入基准。
- **投资观点：维持 `UNDER_REVIEW`。** 至少需看到：2026 年末 backlog 保持 80 亿+、2027 年 gas-on 加速、税后 ROC 守 20%，才考虑升级。

**决定类型：`NO_CHANGE`（观点与 thesis 均无方向性变化；本任务为验证性建模，产出证据而非新观点）。**

---

## Sources

- **[S1]** [Linde 2026Q2 Form 10-Q](https://www.sec.gov/Archives/edgar/data/1707925/000162828026051289/lin-20260630.htm)，filed 2026-07-31；backlog、RPO、capex、ETR、股数。
- **[S2]** [Linde 2025 Form 10-K](https://www.sec.gov/Archives/edgar/data/1707925/000162828026011430/lin-20251231.htm)，filed 2026-02-25；FY2025 backlog 73 亿。
- **[S3]** [Linde 2026Q2 业绩发布](https://www.linde.com/news-and-media/2026/linde-reports-second-quarter-2026-results)与 [Q2 teleconference slides](https://assets.linde.com/-/media/global/corporate/corporate/documents/investors/quarterly-earnings/linde2q26teleconferenceslides.pdf)，2026-07-31；转化率/构成经 [investing.com 转述](https://www.investing.com/news/company-news/linde-q2-2026-slides-record-backlog-amid-margin-pressure-93CH-4828713)。
- **[S4]** [Linde Phoenix $1B 公告](https://www.linde.com/news-and-media/2026/linde-to-invest-$1-billion-to-support-major-u,-d-,s,-d-,-semiconductor-facility-expansion)，2026-07-31。
- **[S5]** [Linde 2021 TSMC Arizona 首期 $600M 公告](https://www.linde.com/news-and-media/2021/linde-signs-long-term-agreement-to-supply-new-world-class-semiconductor-manufacturing-complex-in-the-u-s)。
- **[S6]** [Q2 2026 电话会文字稿（fool.com）](https://www.fool.com/earnings/call-transcripts/2026/08/03/linde-lin-q2-2026-earnings-call-transcript/)，2026-08-03；8 字头、13 亿投产、2027 年 8–12%、电子 +18%。
- **[S7]** [Seeking Alpha：FY2026 EPS 17.70–17.90、backlog 8 字头](https://seekingalpha.com/news/4622894-linde-projects-17_70-17_90-full-year-eps-while-backlog-holds-an-8-handle-despite-1_3b-of-2026)。
- **[S8]** [Linde 2023 OCI Beaumont $1.8B 清洁氢公告](https://www.linde.com/news-and-media/2023/linde-to-invest-1-8-billion-to-supply-clean-hydrogen-to-oci-s-world-scale-blue-ammonia-project-in-the-u-s-gulf-coas)；Woodside 推迟与 Exxon CCS 依赖见 [enkiai](https://enkiai.com/fuel-cells/lindes-hydrogen-dominance-2025-a-deep-dive-analysis/)、[latitude media](https://www.latitudemedia.com/news/just-a-few-projects-are-driving-a-small-bump-in-hydrogen-investment/)。
- **[S9]** [bp × Linde 德州 CCS（bp 官网）](https://www.bp.com/en/global/bp-supply-trading-and-shipping/news/press-releases/bp-and-linde-plan-major-ccs-project-to-advance-decarbonization-efforts-across-texas-gulf-coast.html)；ExxonMobil Baytown 暂停见公开新闻（2025-11）。
- **[S10]** [TSMC Arizona / 气体供应背景（aztechcouncil）](https://www.aztechcouncil.org/600-million-gas-plant-planned-to-support-phoenix-semiconductor-manufacturing-facility/)、[Wikipedia TSMC Arizona](https://en.wikipedia.org/wiki/TSMC_Arizona)。
- **[S11]** IEA Global Hydrogen Review 2025（经 deep-insights-v1 [S8] 引用）。
- **[A1]–[A3]** J.P. Morgan / Bernstein / Citi，2026-07-31/08-02（AlphaEngine，经 deep-insights-v1 引用；本轮未刷新）。
- **[G]** Guidepoint 专家观点（经 deep-insights-v1 引用，本轮未新增访谈）。

---

*模型脚本：`temp/linde_sog_model.py`（可复算）；本文件由 task 4 执行生成。*
