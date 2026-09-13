# Vision 与 owner 指令覆盖复盘 — 2026-09-13

本表替代旧报告中的“当前缺失”判断，历史报告保留其日期含义。核对范围包括 v0.1、v0.5/v0.6、v0.9、v1.1 vision，09-09 analyst onboarding 蓝图、并行计划及 owner 裁决，09-10 owner expectations/steps，09-11 foundation priorities 与两份 prompt-contract review，以及本次 owner 关于持续开发、Sol 并行、Guidepoint、OpenClaw Wiki、桌面历史资料和及时清理备份的指令。没有发现比这些更近的独立 Dalton 指令文件；这不代表能访问全部历史对话。

最新 owner 优先级已收敛为先交付正式可运行 demo。当前已发布基础版本 R23 `3e4e8682`，页面、模型表及五公司导出通过；实际 Ask 路由缺陷仍在修复，尚未计正式交互 demo 完成。最新执行状态见 [PROJECT_STATUS](../PROJECT_STATUS.md)。后续集成包含模板导入、合成双工作区工具和 Wiki 路径修复，但这些扩展不在本次 demo wheel。下列高级范围暂停推进，未从剩余清单中删除。

## 仍有明确开发工作

| 项目 | 当前边界与剩余工作 | 原始要求 / 实现证据 |
| --- | --- | --- |
| Conviction v2 | 当前 authority 仍为 0.1。需版本化 policy/schema、producer/verifier、proposal/lane 全链；允许市场同向但回报有吸引力，分开表示证据置信度、情景概率与回报；后续人工发布策略。不能把 prompt identity 0.2 当 v2 已实现。 | `owner-research-prompt-contract-audit-2026-09-11.md` 最后一节；`conviction_call.py`、`conviction_call_draft.py` |
| 持有期回报及市场预期 | 缺冻结的持有期回报/归因 authority 与买方预期来源 authority。现有 valuation 和调用者提供的 XLSX scenario 不等于这些对象；来源分类不能靠模型猜。 | `foundation-research-prompt-contract-review-2026-09-11.md` Memo 行；`fund_xlsx_export.py` |
| 结构化情景与观察记录 | Dossier 当前情景/触发条件主要仍为有引用的文字。需独立结构化、可持久跟踪的条件/观察；通用 tracking 或模型 scenario 不能替代该对象。 | 上述 review 的 Dossier 行；owner prompt audit 的 Conviction v2 要求 |
| 参考基金模板闭环 | 安全离线样式候选 importer 已合入并通过 9 项相关测试；仅接受可明确映射的受支持样式，并非任意 workbook 自动转换。registry/select 在独立候选 `8e66ee01` 完成并通过 31 项检查，尚待合入、部署和产品验收。 | `fund_xlsx_template_import.py`、`fund_xlsx_template.py` |
| 最终交付与真实多工作区验收 | 页面和 workspace 核心已有实现。仍需确定最终交付/设备视觉验收范围，以及真实两行业 workspace 的配置、安装升级、回滚隔离与共享容量验收。新本地 harness 已补 synthetic acceptance，不能记作真实 host 产品验收。 | `run_two_workspace_acceptance.py`、`workspace.py`、`workspace_process.py`；owner 要求保留现有 Cockpit 风格 |

高级投资能力仍排在基础运行及真实研究产物闭环之后。已有范围内继续开发，不把缺少策略签名误写成代码已经完成。

## 已实现但尚未完成产品验收

建模/财务桥、Initial Screen、Dossier、DebateMap、Deep Insight、行业框架、Memo、事件判断、Thesis 修订候选、Reflection、Ask v2、周报、质量评分和 Analyst Journal 均已有 authority、执行路径及测试。尚未完成的主要是：

- 修复版五家公司真实模型/Excel/HTML 与来源、公式回放验收，IBM 新规格和 CTSH 研究覆盖的具体结果。
- 五份 Initial Screen、至少三份通过出口门，以及实际 Deep Insight → Memo 草稿与人工决策；不能用对象存在代替通过。
- 行情/估值/异常波动闭环、五公司事件覆盖、原始资料定向研究及真实后续修订结果。
- Ask 十个真实问题至少八个有用答案、一次受治理的 refresh；三期周报的质量评分与反馈进入后续起草。
- 人工 gate、Conviction/Memo 决策和研究反馈。自动化不能替人作判断。

## 本次来源事实纠正

**Guidepoint 已有 connector，且 Dalton 与 OpenClaw 共用 `http://127.0.0.1:8943/mcp` 的 `search_library`。** 运行中本地代理、writer endpoint/tool/plan 已绑定。当前 mission v14 把 `source:guidepoint` 写成 `not_connected`，导致 grant 被拒；需要追加正确的 mission 版本。零 Core invocation 不证明缺 MCP。上游没有 `get_transcript`，不得声称可取完整访谈；其 narrowing proposal 单列。

**公司 Wiki 原文、skill 和向量索引均已存在。** `~/.openclaw/workspace/skills/company-wiki/SKILL.md` 对应 `workspace/wiki/vectors.db`，只读清点为 1,022 documents、8,720 chunks。原安装器误找 `workspace/wiki-index.sqlite`。修复 `3715fe1f` 使用实际索引并兼容旧路径，corpus root 保持 workspace，232 项相关测试通过；待后续版本部署。两项 Wiki governance 当前 proposed，mission 尚缺 source entry，启用状态仍需如实登记。

**历史资料目录已建好：** `/Users/everflow/Documents/Dalton 历史研究资料`，含「待整理」「公司/STRL」「行业/美国电商」。桌面 `STRL_Initial Screen_20260612.docx` 与 `US eCommerce_Model_20260801.xlsx` 仅只读查看结构，未移动、修改或自动入库。ecommerce 是跨公司行业模型样例，不是新电商 API 需求。既有 prior-research 读取器按 company manifest 明确列举材料；待资料放入并确认日期/归属后登记，行业多公司文件不能全部归到一家企业。

Sales notes、IR、yfinance 和 Ask 的现有治理/策略状态分别核验；不把普通部署授权写成新来源审批。具体待审项目见 `owner-review-remaining-2026-09-13.md`。

## 发布与清理检查点

R23 已完成 7,945 项完整测试（0 failures/errors，1 skip）、608 文件 wheel/source 核验、14 步复制状态演练、45/45 持续健康及正式发布。真实 HTTPS 首屏 3.587 秒，五公司模型/产物页面与 HTML/XLSX 导出通过；Ask 一次实际调用被路由拒绝，正以 R24 必需修复处理。页面可用不等于高级研究质量和人工验收已经完成。

失败历史保留：R22b 的部署工具漏接返回值导致安装失败并健康回滚；R23 首次演练在复制前被磁盘余量检查拒绝；旧 QA proxy 引起线上请求积压，移除后页面恢复。所有失败日志与修复后验收单独保存。

本轮持续清理已完成演练和开发的临时 SQLite 副本，并关闭确认过期的 QA proxy/session；保留发布证据、当前回滚及 R18b 外部依赖、R21 恢复链所需文件。备份保留状态与清理 receipt 随发布 packet 登记。

## 明确冻结或排除

Skill 自主生成/sandbox、embedding-first、新 Temporal/Postgres、多 runtime 重构及 Interrupt/park/resume 在原 vision 中冻结或移除。现有多分析师 workspace 隔离不等于解冻这些项目。旧“没有 Reflection/market/dossier/Ask v2”的历史表述不再作为新建任务。
