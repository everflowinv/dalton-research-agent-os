# Dalton Cockpit 独立审查：按钮与交互入口源码台账

日期：2026-10-09。配套 [主报告](cockpit-independent-analyst-ux-review-2026-10-09.md) 和 [证据附件](cockpit-independent-ux-evidence-2026-10-09.md)。

此表按源码构造点覆盖按钮、链接、折叠、表单字段和图表命中区。动态对象（公司、审批actions、情景、反馈、模型链、待办entry）会复用一个构造点；行数不是页面可见按钮数量。表内保留开发定位片段，供实现者准确查找，**不是建议把这些代码展示给分析师**。通用工厂/条件分支也列入，未出现的分支标为源码覆盖，写操作未实际提交。

通用检查规则：只读入口需对象和返回路径；写操作需业务结果、输入校验、处理中状态、失败恢复和完成态；危险/收费动作需准确后果与额度；导航/展开不得丢草稿。具体问题与修复见主报告各页及第12节。

## cockpit_v2/admin/admin.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I001 | 按钮构造 | [admin.js:47](../../src/dalton_core/cockpit_v2/admin/admin.js#L47) | btn(label, onclick, cls = "") { return h("button", { type: "button", class: ˋadm-btn ${cls}ˋ.trim(), onclick }, label); } function pill(kind, label) { return h("span", { class: ˋadm-pill ${k |
| I002 | 动态控件构造 | [admin.js:48](../../src/dalton_core/cockpit_v2/admin/admin.js#L48) | h("button", { type: "button", class: ˋadm-btn ${cls}ˋ.trim(), onclick }, label); } function pill(kind, label) { return h("span", { class: ˋadm-pill ${kind}ˋ }, label); } function tech(label, |
| I003 | 管理员动作表单 | [admin.js:183](../../src/dalton_core/cockpit_v2/admin/admin.js#L183) | writeForm({ action, label, payload, danger = false, confirmText = null, after = null, defaultReason = "", placeholder = "写一句理由（会和操作一起存档）", primary = true, extra = null, validate = null }) { |
| I004 | 动态控件构造 | [admin.js:186](../../src/dalton_core/cockpit_v2/admin/admin.js#L186) | h("input", { type: "text", class: "adm-reason", maxlength: "300", value: defaultReason, "aria-label": ˋ${label}的理由ˋ, placeholder }); const out = h("div"); const go = btn(label, null, danger |
| I005 | 按钮构造 | [admin.js:189](../../src/dalton_core/cockpit_v2/admin/admin.js#L189) | btn(label, null, danger ? "danger" : primary ? "primary" : ""); go.addEventListener |
| I006 | 按钮构造 | [admin.js:233](../../src/dalton_core/cockpit_v2/admin/admin.js#L233) | btn("复制", async () =&gt; { try { if (!navigator.clipboard) throw new Error("浏览器不允许复制"); await navigator.clipboard.writeText(text); status.className = "adm-status ok"; status.textContent = "已复制" |
| I007 | 动态控件构造 | [admin.js:257](../../src/dalton_core/cockpit_v2/admin/admin.js#L257) | h("button", { type: "button", role: "tab", id: ˋadm-tab-${id}ˋ, "aria-controls": ˋadm-panel-${id}ˋ, "aria-selected": "false", tabindex: "-1", dataset: { tab: id } }, label); b.addEventListen |
| I008 | 按钮构造 | [admin.js:303](../../src/dalton_core/cockpit_v2/admin/admin.js#L303) | btn("重新读取", () =&gt; loadTab(id))); } } function reload(id) { state.loaded[id] = true; return loadTab(id); } async function shellBackLink() { const back = document.getElementById("adm-back"); i |
| I009 | 按钮构造 | [admin.js:338](../../src/dalton_core/cockpit_v2/admin/admin.js#L338) | btn("↑", () =&gt; { [picked[i - 1], picked[i]] = [picked[i], picked[i - 1]]; render(); }); const down = btn("↓", () =&gt; { [picked[i + 1], picked[i]] = [picked[i], picked[i + 1]]; render(); }); c |
| I010 | 按钮构造 | [admin.js:339](../../src/dalton_core/cockpit_v2/admin/admin.js#L339) | btn("↓", () =&gt; { [picked[i + 1], picked[i]] = [picked[i], picked[i + 1]]; render(); }); const rm = btn("移除", () =&gt; { picked.splice(i, 1); render(); }); up.disabled = i === 0; down.disabled = |
| I011 | 按钮构造 | [admin.js:340](../../src/dalton_core/cockpit_v2/admin/admin.js#L340) | btn("移除", () =&gt; { picked.splice(i, 1); render(); }); up.disabled = i === 0; down.disabled = i === picked.length - 1; up.setAttribute("aria-label", ˋ上移 ${d.name}ˋ); down.setAttribute("aria-la |
| I012 | 动态控件构造 | [admin.js:351](../../src/dalton_core/cockpit_v2/admin/admin.js#L351) | h("select", { "aria-label": ˋ${ariaLabel}：添加备用服务ˋ }, h("option", { value: "" }, "选择要添加的备用服务…"), choices.map(c =&gt; h("option", { value: c.id, disabled: c.disabled &#124;&#124; null }, c.label))); const |
| I013 | 按钮构造 | [admin.js:354](../../src/dalton_core/cockpit_v2/admin/admin.js#L354) | btn("添加备用服务", () =&gt; { const v = select.value; if (v && !picked.includes(v)) { picked.push(v); render(); } }); render(); return { node: h("div", null, list, h("div", { class: "adm-form" }, se |
| I014 | 管理员动作表单 | [admin.js:392](../../src/dalton_core/cockpit_v2/admin/admin.js#L392) | writeForm({ action: "model_select", label: ˋ保存「${card.label}」的选择顺序（同步到所有环境）ˋ, payload: () =&gt; ({ tier: card.tier, mode: "explicit", chain: [...editor.picked] }), validate: () =&gt; (editor.picke |
| I015 | 管理员动作表单 | [admin.js:397](../../src/dalton_core/cockpit_v2/admin/admin.js#L397) | writeForm({ action: "model_select", label: "恢复系统推荐顺序", primary: false, payload: { tier: card.tier, mode: "tier" }, confirmText: ˋ「${card.label}」将恢复为系统推荐顺序，并发布到所有研究环境。确认？ˋ, after: () =&gt; reloa |
| I016 | 动态控件构造 | [admin.js:417](../../src/dalton_core/cockpit_v2/admin/admin.js#L417) | h("select", { "aria-label": "选择要单独调整的环节" }, purposes.map(p =&gt; h("option", { value: p.purpose }, ˋ${p.label}（${p.tier_label &#124;&#124; ""}）ˋ))); const holder = h("div"); const render = |
| I017 | 动态控件构造 | [admin.js:425](../../src/dalton_core/cockpit_v2/admin/admin.js#L425) | h("input", { type: "number", min: "256", step: "256", value: eff.max_output_tokens ?? "", "aria-label": ˋ${p.label}的单次输出上限ˋ }); swap(holder, h("div", { class: "adm-meta" }, ˋ当前：${p.mode_labe |
| I018 | 管理员动作表单 | [admin.js:429](../../src/dalton_core/cockpit_v2/admin/admin.js#L429) | writeForm({ action: "model_select", label: ˋ为「${p.label}」单独指定顺序ˋ, payload: () =&gt; ({ purpose: p.purpose, mode: "explicit", chain: [...editor.picked] }), validate: () =&gt; (editor.picked.length |
| I019 | 管理员动作表单 | [admin.js:432](../../src/dalton_core/cockpit_v2/admin/admin.js#L432) | writeForm({ action: "model_select", label: "改回按档位选择", primary: false, payload: { purpose: p.purpose, mode: "tier" }, after: () =&gt; reload("ai") }), cb.editable ? h("div", null, h("h3", null, |
| I020 | 管理员动作表单 | [admin.js:436](../../src/dalton_core/cockpit_v2/admin/admin.js#L436) | writeForm({ action: "model_budget", label: "保存输出上限", primary: false, extra: h("label", { class: "adm-field" }, h("span", null, "单次输出上限（token）"), out), validate: |
| I021 | 按钮构造 | [admin.js:483](../../src/dalton_core/cockpit_v2/admin/admin.js#L483) | btn("把推荐顺序填入上方（不会保存）", () =&gt; editor.set(rec.chain &#124;&#124; []))); put(body, writeForm({ action: "model_select", label: "保存起草顺序（同步到所有环境）", defaultReason: ed.default_reason &#124;&#124; "", payload: () =&gt; ({ |
| I022 | 管理员动作表单 | [admin.js:484](../../src/dalton_core/cockpit_v2/admin/admin.js#L484) | writeForm({ action: "model_select", label: "保存起草顺序（同步到所有环境）", defaultReason: ed.default_reason &#124;&#124; "", payload: () =&gt; ({ purpose: row.draft_purpose, mode: "explicit", chain: [...editor.picked |
| I023 | 管理员动作表单 | [admin.js:487](../../src/dalton_core/cockpit_v2/admin/admin.js#L487) | writeForm({ action: "model_select", label: "恢复按档位的系统推荐（会直接发布）", primary: false, defaultReason: "恢复按档位的系统推荐顺序", payload: { purpose: row.draft_purpose, mode: "tier" }, confirmText: ˋ「${row.dra |
| I024 | 动态控件构造 | [admin.js:494](../../src/dalton_core/cockpit_v2/admin/admin.js#L494) | h("summary", null, ˋ其余 ${idle.length} 个核验环节在任何环境都还没有配置ˋ), h("ul", null, idle.map(r =&gt; h("li", null, ˋ${r.draft_label} → ${(r.pairs &#124;&#124; []).map(p =&gt; p.verifier_label).join("、")}ˋ))))); return |
| I025 | 管理员动作表单 | [admin.js:508](../../src/dalton_core/cockpit_v2/admin/admin.js#L508) | writeForm({ action: "model_allow", label: "允许使用", primary: false, payload: { model_ref: name }, confirmText: ˋ允许 Dalton 使用「${name}」？这会改模型网关的授权，可能产生费用；重启模型网关后生效。ˋ, after: () =&gt; reload("ai") } |
| I026 | 动态控件构造 | [admin.js:519](../../src/dalton_core/cockpit_v2/admin/admin.js#L519) | h("select", { "aria-label": "要补充信息的 AI 服务" }, choices.map(c =&gt; h("option", { value: c.model }, c.display_name))); const family = h("input", { type: "text", "aria-label": "模型家族", maxlength: " |
| I027 | 动态控件构造 | [admin.js:520](../../src/dalton_core/cockpit_v2/admin/admin.js#L520) | h("input", { type: "text", "aria-label": "模型家族", maxlength: "128" }); const caps = h("input", { type: "text", "aria-label": "能力（逗号分隔）", maxlength: "400" }); const fill = |
| I028 | 动态控件构造 | [admin.js:521](../../src/dalton_core/cockpit_v2/admin/admin.js#L521) | h("input", { type: "text", "aria-label": "能力（逗号分隔）", maxlength: "400" }); const fill = |
| I029 | 管理员动作表单 | [admin.js:526](../../src/dalton_core/cockpit_v2/admin/admin.js#L526) | writeForm({ action: "model_metadata", label: "保存 AI 服务信息", primary: false, extra: [h("label", { class: "adm-field" }, h("span", null, "服务"), pick), h("label", { class: "adm-field" }, h("span |
| I030 | 管理员动作表单 | [admin.js:543](../../src/dalton_core/cockpit_v2/admin/admin.js#L543) | writeForm({ action: "model_notice_ack", label: "标为已读", primary: false, defaultReason: "已查看", payload: { ref: n.ref }, after: () =&gt; reload("ai") })))) : null, section("各档位的 AI 服务选择顺序", "每个档位按 |
| I031 | 动态控件构造 | [admin.js:547](../../src/dalton_core/cockpit_v2/admin/admin.js#L547) | h("summary", null, "单独调整某个环节（高级）"), stageEditor(models))), section("冷却中的 AI 服务", "连续失败的服务会暂时冷却，冷却期内各顺序自动跳过它。", cooldownList(models)), section("核验独立性 · 起草服务顺序", "这些环节写出的内容要由另一家模型独立核验；起草如果落在核验 |
| I032 | 动态控件构造 | [admin.js:653](../../src/dalton_core/cockpit_v2/admin/admin.js#L653) | h("input", { type: "number", min: "1", max: "720", step: "1", placeholder: "不填＝直到手动恢复", "aria-label": "暂停小时数" }); } function leversSection(r) { const box = h("div"); if (r.banner) put(box, h |
| I033 | 管理员动作表单 | [admin.js:659](../../src/dalton_core/cockpit_v2/admin/admin.js#L659) | writeForm({ action: "lever", label: "紧急停机", danger: true, confirmText: "紧急停机：所有研究立即暂停，不再发起新的付费调用，直到手动恢复。确定？", payload: () =&gt; ({ lever: "emergency_stop", request_id: rid() }), after: () =&gt; re |
| I034 | 管理员动作表单 | [admin.js:663](../../src/dalton_core/cockpit_v2/admin/admin.js#L663) | writeForm({ action: "lever", label: "恢复全部运行", confirmText: "恢复全部运行：所有未单独暂停的研究流程将重新开始接任务。确认？", payload: () =&gt; ({ lever: "resume_all", request_id: rid() }), after: () =&gt; reload("run") })); put |
| I035 | 管理员动作表单 | [admin.js:671](../../src/dalton_core/cockpit_v2/admin/admin.js#L671) | writeForm({ action: "lever", label: ˋ恢复「${l.label}」ˋ, primary: false, payload: () =&gt; ({ lever: "resume", scope_kind: "lane", scope_ref: l.lane, request_id: rid() }), after: () =&gt; reload("run |
| I036 | 管理员动作表单 | [admin.js:673](../../src/dalton_core/cockpit_v2/admin/admin.js#L673) | writeForm({ action: "lever", label: ˋ暂停「${l.label}」ˋ, primary: false, extra: h("label", { class: "adm-field" }, h("span", null, "暂停小时数"), hrs), payload: |
| I037 | 管理员动作表单 | [admin.js:680](../../src/dalton_core/cockpit_v2/admin/admin.js#L680) | writeForm({ action: "lever", label: "恢复研究", primary: false, payload: () =&gt; ({ lever: "resume", scope_kind: "company", scope_ref: c.company_ref, request_id: rid() }), after: () =&gt; reload("run |
| I038 | 管理员动作表单 | [admin.js:681](../../src/dalton_core/cockpit_v2/admin/admin.js#L681) | writeForm({ action: "lever", label: "暂停研究", primary: false, payload: () =&gt; ({ lever: "pause", scope_kind: "company", scope_ref: c.company_ref, request_id: rid() }), after: () =&gt; reload("run" |
| I039 | 管理员动作表单 | [admin.js:682](../../src/dalton_core/cockpit_v2/admin/admin.js#L682) | writeForm({ action: "prioritize", label: c.prioritized ? "恢复常规跟踪频率" : "提高跟踪频率", primary: false, payload: () =&gt; ({ company_ref: c.company_ref, mode: c.prioritized ? "normal" : "prioritize", r |
| I040 | 管理员动作表单 | [admin.js:686](../../src/dalton_core/cockpit_v2/admin/admin.js#L686) | writeForm({ action: "stop_line", label: "停止这项专项研究", danger: true, confirmText: ˋ停止「${x.label}」：已产生的成果保留，但这项研究不会继续；以后需要时由规划重新立项。确认？ˋ, payload: () =&gt; ({ line_ref: x.line_ref, request_id: rid() |
| I041 | 动态控件构造 | [admin.js:689](../../src/dalton_core/cockpit_v2/admin/admin.js#L689) | h("summary", null, ˋ运行控制操作记录（${(r.receipts &#124;&#124; []).length}）ˋ), (r.receipts &#124;&#124; []).length ? table(["时间", "操作", "对象", "结果", "理由"], r.receipts.map(x =&gt; [when(x.at), txt(x.lever_label), txt(x.tar |
| I042 | 动态控件构造 | [admin.js:695](../../src/dalton_core/cockpit_v2/admin/admin.js#L695) | h("input", { type: "text", "aria-label": "新增公司代码", placeholder: "例如 ACN, IBM" }); const tier = h("select", { "aria-label": "覆盖等级" }, h("option", { value: "A" }, "重点（A）"), h("option", { value |
| I043 | 动态控件构造 | [admin.js:696](../../src/dalton_core/cockpit_v2/admin/admin.js#L696) | h("select", { "aria-label": "覆盖等级" }, h("option", { value: "A" }, "重点（A）"), h("option", { value: "B", selected: true }, "常规（B）"), h("option", { value: "C" }, "观察（C）")); const removeBox = h(" |
| I044 | 动态控件构造 | [admin.js:698](../../src/dalton_core/cockpit_v2/admin/admin.js#L698) | h("input", { type: "checkbox", value: c.company_ref, checked: state.coverageRemove.has(c.company_ref) &#124;&#124; null }); cb.addEventListener |
| I045 | 管理员动作表单 | [admin.js:711](../../src/dalton_core/cockpit_v2/admin/admin.js#L711) | writeForm({ action: "publish", label: "确认覆盖名单调整", confirmText: "确认后新的覆盖名单立即生效，检索计划随之调整。确认？", payload: { draft_id: r2.draft_id, draft_hash: r2.draft_hash, request_id: rid() }, after: |
| I046 | 管理员动作表单 | [admin.js:714](../../src/dalton_core/cockpit_v2/admin/admin.js#L714) | writeForm({ action: "decide", label: "放弃这份草稿", primary: false, defaultReason: "不采用这份名单草案", payload: { kind: "draft:coverage", ref: r2.draft_id, hash: r2.draft_hash, decision: "discard", requ |
| I047 | 管理员动作表单 | [admin.js:721](../../src/dalton_core/cockpit_v2/admin/admin.js#L721) | writeForm({ action: "coverage_draft", label: "生成名单调整草案", validate: () =&gt; (add.value.trim() &#124;&#124; state.coverageRemove.size ? null : "请填写要新增的代码，或勾选要移除的公司"), payload: () =&gt; ({ add: add.value, rem |
| I048 | 动态控件构造 | [admin.js:734](../../src/dalton_core/cockpit_v2/admin/admin.js#L734) | h("input", { type: "number", min: "0.01", max: "20", step: "0.01", "aria-label": "费用上限（美元）" }) : null; return writeForm({ action: "owner_door", label: d.label, danger: !!(d.danger &#124;&#124; d.needs |
| I049 | 管理员动作表单 | [admin.js:735](../../src/dalton_core/cockpit_v2/admin/admin.js#L735) | writeForm({ action: "owner_door", label: d.label, danger: !!(d.danger &#124;&#124; d.needs_cost_cap), confirmText: d.confirm &#124;&#124; null, extra: cap ? h("label", { class: "adm-field" }, h("span", null, "费 |
| I050 | 动态控件构造 | [admin.js:782](../../src/dalton_core/cockpit_v2/admin/admin.js#L782) | h("input", { type: "number", min: "0", step, value: b[key] ?? "", "aria-label": label }); return h("label", { class: "adm-field" }, h("span", null, label), inputs[key]); }); const pools = h( |
| I051 | 动态控件构造 | [admin.js:784](../../src/dalton_core/cockpit_v2/admin/admin.js#L784) | h("input", { type: "checkbox", checked: b.pools_enforcement === "off" &#124;&#124; null }); extra.push(h("label", { class: "adm-field" }, h("span", null, "分项预算池"), h("span", null, pools, " 只统计、不拦截"))) |
| I052 | 管理员动作表单 | [admin.js:787](../../src/dalton_core/cockpit_v2/admin/admin.js#L787) | writeForm({ action: "research_budget", label: "保存研究预算上限", extra, confirmText: "新的预算上限会写入研究目标的授权链并立即生效。确认？", validate: () =&gt; (Number(inputs.max_daily_cost_usd.value) &gt; 0 ? null : "每天总费用上限必须大于 |
| I053 | 动态控件构造 | [admin.js:825](../../src/dalton_core/cockpit_v2/admin/admin.js#L825) | h("select", { "aria-label": "环节" }, shared.map(p =&gt; h("option", { value: p.purpose }, p.label))); const cost = h("input", { type: "number", min: "0", step: "any", "aria-label": "单次费用上限（美元）" |
| I054 | 动态控件构造 | [admin.js:826](../../src/dalton_core/cockpit_v2/admin/admin.js#L826) | h("input", { type: "number", min: "0", step: "any", "aria-label": "单次费用上限（美元）" }); return h("details", { class: "adm-more" }, h("summary", null, "调整某个环节的单次费用上限（全部环境）"), writeForm({ action: " |
| I055 | 动态控件构造 | [admin.js:827](../../src/dalton_core/cockpit_v2/admin/admin.js#L827) | h("summary", null, "调整某个环节的单次费用上限（全部环境）"), writeForm({ action: "model_budget", label: "保存单次费用上限", primary: false, extra: [h("label", { class: "adm-field" }, h("span", null, "环节"), pick), h(" |
| I056 | 管理员动作表单 | [admin.js:828](../../src/dalton_core/cockpit_v2/admin/admin.js#L828) | writeForm({ action: "model_budget", label: "保存单次费用上限", primary: false, extra: [h("label", { class: "adm-field" }, h("span", null, "环节"), pick), h("label", { class: "adm-field" }, h("span", n |
| I057 | 按钮构造 | [admin.js:858](../../src/dalton_core/cockpit_v2/admin/admin.js#L858) | btn(label, () =&gt; { current = f; render(); }); b.dataset.f = f; put(filter, b); }); render(); return h("div", null, h("p", { class: "adm-explain" }, x.note), h("div", { class: "adm-meta" }, O |
| I058 | 动态控件构造 | [admin.js:900](../../src/dalton_core/cockpit_v2/admin/admin.js#L900) | h("summary", null, ˋ最近 ${x.exhausted_items.length} 份语言质检放弃的报告（可重新质检）ˋ), x.exhausted_items.map(it =&gt; h("div", { class: "adm-row" }, h("div", { class: "grow" }, ˋ${txt(it.kind)} · ${when(it.at |
| I059 | 管理员动作表单 | [admin.js:902](../../src/dalton_core/cockpit_v2/admin/admin.js#L902) | writeForm({ action: "language_retry", label: "重新进行语言质检", primary: false, payload: () =&gt; ({ identity_hash: it.identity_hash, product_hash: it.product_hash, request_id: rid() }), after: () =&gt; |
| I060 | 按钮构造 | [admin.js:929](../../src/dalton_core/cockpit_v2/admin/admin.js#L929) | btn(k === "all" ? ˋ全部（${events.length}）ˋ : kindLabel.get(k) &#124;&#124; k, |
| I061 | 动态控件构造 | [admin.js:945](../../src/dalton_core/cockpit_v2/admin/admin.js#L945) | h("summary", null, ˋ${txt(dep.dependency_label, "外部服务")} · ${count(dep.item_count)} 件 · 首次受阻 ${when(dep.first_seen)} · 最近尝试 ${when(dep.last_seen)} · 累计 ${count(dep.attempts)} 次ˋ), h("ul", nu |
| I062 | 动态控件构造 | [admin.js:956](../../src/dalton_core/cockpit_v2/admin/admin.js#L956) | h("summary", null, ˋ已处理的历史记录（共 ${count(r.historical_count)} 条，配置或研究目标已更新，仅作记录）ˋ), list); let next = r.historical_page && r.historical_page.next_offset; if (next != null) { const more = btn |
| I063 | 按钮构造 | [admin.js:959](../../src/dalton_core/cockpit_v2/admin/admin.js#L959) | btn("再看一页", async () =&gt; { more.disabled = true; try { const page = await getJson(ˋ/v1/cockpit/ops?historical_offset=${encodeURIComponent(next)}ˋ); put(list, (page.historical_items &#124;&#124; []).map |
| I064 | 动态控件构造 | [admin.js:997](../../src/dalton_core/cockpit_v2/admin/admin.js#L997) | h("input", { type: "text", maxlength: "80", "aria-label": "新项目名称" }); let createId = rid(); return h("div", null, items.length ? items.map(item =&gt; { const label = item.display_name &#124;&#124; item.n |
| I065 | 动态控件构造 | [admin.js:1002](../../src/dalton_core/cockpit_v2/admin/admin.js#L1002) | h("input", { type: "text", maxlength: "80", value: label, "aria-label": ˋ${label}的新名称ˋ }); return h("div", { class: "adm-row" }, h("div", { class: "grow" }, h("div", null, label, " ", item.c |
| I066 | 动态控件构造 | [admin.js:1005](../../src/dalton_core/cockpit_v2/admin/admin.js#L1005) | h("a", { href: item.url, class: "adm-btn" }, "进入") : null, writeForm({ action: "workspace_rename", label: "重命名", primary: false, extra: h("label", { class: "adm-field" }, h("span", null, "新名 |
| I067 | 管理员动作表单 | [admin.js:1006](../../src/dalton_core/cockpit_v2/admin/admin.js#L1006) | writeForm({ action: "workspace_rename", label: "重命名", primary: false, extra: h("label", { class: "adm-field" }, h("span", null, "新名称"), next), validate: |
| I068 | 管理员动作表单 | [admin.js:1012](../../src/dalton_core/cockpit_v2/admin/admin.js#L1012) | writeForm({ action: "workspace_create", label: "新建研究项目", extra: h("label", { class: "adm-field" }, h("span", null, "项目名称"), name), validate: |
| I069 | 动态控件构造 | [admin.js:1018](../../src/dalton_core/cockpit_v2/admin/admin.js#L1018) | h("select", { "aria-label": "模板" }, withNone ? h("option", { value: "" }, "（不指定）") : null, templates.map(t =&gt; h("option", { value: t.version_ref, selected: t.version_ref === selected &#124;&#124; null |
| I070 | 管理员动作表单 | [admin.js:1020](../../src/dalton_core/cockpit_v2/admin/admin.js#L1020) | writeForm({ action: "fund_template_select", label, primary: false, payload: () =&gt; { const t = templates.find(x =&gt; x.version_ref === sel.value); return { scope_kind: scopeKind, scope_ref: sco |
| I071 | 动态控件构造 | [admin.js:1031](../../src/dalton_core/cockpit_v2/admin/admin.js#L1031) | h("summary", null, "按行业分类指定默认模板"), r.industries.map(i =&gt; { const sel = opt(i.template_version_ref &#124;&#124; "", true); return h("div", { class: "adm-row" }, h("div", { class: "grow" }, ˋ行业分类「${i.la |
| I072 | 动态控件构造 | [admin.js:1036](../../src/dalton_core/cockpit_v2/admin/admin.js#L1036) | h("summary", null, ˋ模板操作记录（${r.receipts.length}）ˋ), table(["时间", "操作", "范围", "模板", "理由"], r.receipts.map(x =&gt; [when(x.at), txt(x.action_label), ˋ${txt(x.scope_label, "")} ${txt(x.target, "") |
| I073 | 动态控件构造 | [admin.js:1038](../../src/dalton_core/cockpit_v2/admin/admin.js#L1038) | h("a", { href: "/#/settings?focus=templates" }, "/#/settings?focus=templates"), "）；登记后在这里指定使用范围，单家公司的模板在公司页「导出财务模型」里指定。")); } async function loadProjects(p) { const [w, t] = await Promise.al |

## cockpit_v2/pages/ask.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I074 | 动态控件构造 | [ask.js:23](../../src/dalton_core/cockpit_v2/pages/ask.js#L23) | h("summary", null, "完整回答"), ...ui.paragraphs(rest, ctx.seen))); const gaps = (r.display_gaps &#124;&#124; r.gaps &#124;&#124; []).filter(Boolean); if (gaps.length) box.append(h("h3", null, "尚不能确认"), h("ul", nul |
| I075 | 动态控件构造 | [ask.js:30](../../src/dalton_core/cockpit_v2/pages/ask.js#L30) | h("summary", null, ˋ其余 ${cites.length - 5} 条依据ˋ), h("ol", {class: "v2-cite", start: "6"}, cites.slice(5).map(c =&gt; h("li", null, lead(say(c.statement), 200)))))); } box.append(h("p", {class: |
| I076 | 表单字段 | [ask.js:54](../../src/dalton_core/cockpit_v2/pages/ask.js#L54) | ui.field({id: "ask-scope", label: "研究对象", control: "select", help: "选择一家公司时，公司名会写在问题开头"}); scope.input.append(h("option", {value: ""}, "本项目全部公司"), ...list.map(c =&gt; h("option", {value: compan |
| I077 | 表单字段 | [ask.js:56](../../src/dalton_core/cockpit_v2/pages/ask.js#L56) | ui.field({id: "ask-question", label: "你的问题", help: example, required: true, maxLength: 1900}); ctx.drafts.bind(question.input, "ask:question"); const status = h("div", {role: "status", "aria |
| I078 | 按钮构造 | [ask.js:59](../../src/dalton_core/cockpit_v2/pages/ask.js#L59) | ui.button("提问", null); if (!modelOk) { submit.disabled = true; status.append(ui.alert(say(ov.value.model_available.reason &#124;&#124; "AI 服务暂时不可用，暂时不能提问。"))); } const form = h("form", {class: "v2-pan |
| I079 | 表单字段 | [ask.js:95](../../src/dalton_core/cockpit_v2/pages/ask.js#L95) | ui.field({id: "ask-search", label: "搜索历史问题", control: "input", attrs: {type: "search", value: ctx.state.filters.q &#124;&#124; ""}}); const listEl = h("ul", {class: "v2-list"}); const draw = |
| I080 | 动态控件构造 | [ask.js:105](../../src/dalton_core/cockpit_v2/pages/ask.js#L105) | h("summary", null, "查看回答"), renderAnswer(ctx, it.result)) : null, h |
| I081 | 按钮构造 | [ask.js:107](../../src/dalton_core/cockpit_v2/pages/ask.js#L107) | ui.button(it.status === "failed" ? "编辑后重问" : "基于这条追问", () =&gt; { question.input.value = it.status === "failed" ? (it.request?.question &#124;&#124; "") : ˋ关于「${lead(it.request?.question &#124;&#124; "", 60)}」的回答： |

## cockpit_v2/pages/common.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I082 | 动态控件构造 | [common.js:105](../../src/dalton_core/cockpit_v2/pages/common.js#L105) | h("button", {type: "button", class: "v2-sort", "aria-pressed": pressed ? "true" : "false", on: {click: () =&gt; ctx.navigate({filters: {...ctx.state.filters, sort: key, dir: nextDir}}, {replace |
| I083 | 动态控件构造 | [common.js:113](../../src/dalton_core/cockpit_v2/pages/common.js#L113) | h("a", {href: href({page: "company", company: r.key})}, r.name)}, {key: "view", label: "核心情景", render: r =&gt; r.view &#124;&#124; h("span", {class: "v2-note"}, "尚未形成投资判断")}, {key: "ret", label: SORTS.re |
| I084 | 动态控件构造 | [common.js:120](../../src/dalton_core/cockpit_v2/pages/common.js#L120) | h("a", {href: href({page: "tasks", filters: {company: r.key}})}, ˋ${r.tasks} 项ˋ) : "—")}, ], rows: sorted, }); } export function tasksByCompany(needs) { const map = new Map(); for (const it |

## cockpit_v2/pages/companies.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I085 | 动态控件构造 | [companies.js:38](../../src/dalton_core/cockpit_v2/pages/companies.js#L38) | h("a", {class: "v2-btn", href: ctx.href({page: "settings", filters: {focus: "goal"}})}, "设定研究目标")); return; } const q = ctx.h("div", {class: "v2-field"}); const search = ui.field({id: "compa |
| I086 | 表单字段 | [companies.js:42](../../src/dalton_core/cockpit_v2/pages/companies.js#L42) | ui.field({id: "company-filter", label: "查找公司", help: "输入公司名或代码", control: "input", attrs: {type: "search", value: ctx.state.filters.q &#124;&#124; ""}}); search.input.addEventListener |
| I087 | 动态控件构造 | [companies.js:87](../../src/dalton_core/cockpit_v2/pages/companies.js#L87) | h("a", {href: href({page: "company", company: key, tab: s.id}), "data-sec": s.id, "aria-current": s.id === current ? "true" : null}, s.title))))); } function scrollToTab(ctx, tab, focus) { c |
| I088 | 动态控件构造 | [companies.js:110](../../src/dalton_core/cockpit_v2/pages/companies.js#L110) | h("a", {href: href({page: "companies"})}, "返回公司列表")); return; } const key = companyKey(c); const tab = SECTION_IDS.includes(ctx.state.tab) ? ctx.state.tab : null; const m = c.market &#124;&#124; {}; r |
| I089 | 动态控件构造 | [companies.js:116](../../src/dalton_core/cockpit_v2/pages/companies.js#L116) | h("a", {href: href({page: "companies"})}, "公司研究"), " › ", companyName(c))); root.append(h("div", {class: "v2-page-head"}, h("h1", null, companyName(c)), m.close ? h("span", {class: "v2-sub"} |
| I090 | 按钮构造 | [companies.js:202](../../src/dalton_core/cockpit_v2/pages/companies.js#L202) | ui.button(ˋ阅读${say(source.label)}全文ˋ, |
| I091 | 动态控件构造 | [companies.js:221](../../src/dalton_core/cockpit_v2/pages/companies.js#L221) | h("summary", null, "判断理由"), ...ui.paragraphs(say(j.because), ctx.seen))); } const figures = (c.figures?.latest &#124;&#124; []).slice(0, 3); if (figures.length) { el.append(h("h3", null, "关键数字"), h("u |
| I092 | 按钮构造 | [companies.js:315](../../src/dalton_core/cockpit_v2/pages/companies.js#L315) | ui.button("阅读核心争议全文", () =&gt; openReader(ctx, debate), {variant: "quiet"})); } const gaps = c.source_readiness?.ready === false ? c.note : null; if (gaps) section.append(ui.alert(ˋ资料缺口：${say(g |
| I093 | 动态控件构造 | [companies.js:360](../../src/dalton_core/cockpit_v2/pages/companies.js#L360) | h("a", {href: ctx.href({filters: {...f, line: "", accession: "", importance: ""}})}, "清除")) : null; const vocab = v.vocabulary &#124;&#124; {}; const select = |
| I094 | 表单字段 | [companies.js:363](../../src/dalton_core/cockpit_v2/pages/companies.js#L363) | ui.field({id, label, control: "select"}); field.input.append(h("option", {value: ""}, "全部"), ...(options &#124;&#124; []).map(o =&gt; h("option", {value: o.value, selected: o.value === current &#124;&#124; null}, |
| I095 | 按钮构造 | [companies.js:372](../../src/dalton_core/cockpit_v2/pages/companies.js#L372) | ui.button("查看原文与出处", () =&gt; ctx.side.open({heading: "来源与原文", render: el =&gt; { el.append(h("p", null, h("span", {lang: "en"}, x.display_statement &#124;&#124; x.statement)), ui.kv([["公司", say(x.company)] |
| I096 | 按钮构造 | [companies.js:381](../../src/dalton_core/cockpit_v2/pages/companies.js#L381) | ui.button("加载更多", async () =&gt; { moreButton.disabled = true; const p = new URLSearchParams(params); p.set("cursor", cursor); const next = await load(ctx, ˋ/v1/cockpit/claims?${p}ˋ); moreButto |
| I097 | 动态控件构造 | [companies.js:394](../../src/dalton_core/cockpit_v2/pages/companies.js#L394) | h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle", "aria-pressed": allVersions ? "true" : "false", on: {click: () =&gt; ctx.setFilter("versions", allVersions ? "" : "all")}}, |
| I098 | 表单字段 | [companies.js:431](../../src/dalton_core/cockpit_v2/pages/companies.js#L431) | ui.field({id: "model-template", label: "导出模板", control: "select", help: mine ? ˋ这家公司现在使用：${say(mine.template_label)}（${say(mine.basis_label &#124;&#124; "")}）ˋ : "按本项目的默认模板导出"}); select.input.append(. |
| I099 | 按钮构造 | [companies.js:436](../../src/dalton_core/cockpit_v2/pages/companies.js#L436) | ui.button("导出财务模型（Excel）", () =&gt; exportFile(ctx, c, "xlsx", xlsx, status, chosen()), {variant: "secondary"}); el.append(h("h3", {id: "model-export"}, "导出财务模型"), ui.note(missing.length ? ˋ导出的 |
| I100 | 表单字段 | [companies.js:446](../../src/dalton_core/cockpit_v2/pages/companies.js#L446) | ui.field({id: "model-template-reason", label: "理由", required: true, maxLength: 300, control: "input", help: "会和这次指定一起存档，例如：这家公司按 TMT 组的模板出模型。"}); ctx.drafts.bind(reason.input, ˋtemplate:${c. |
| I101 | 按钮构造 | [companies.js:467](../../src/dalton_core/cockpit_v2/pages/companies.js#L467) | ui.button("把所选模板设为这家公司的模板", () =&gt; choose(select.input.value, setDefault), {variant: "quiet"}); const reset = mine.company_choice_ref ? ui.button("恢复按行业或项目默认", () =&gt; choose(null, reset), {var |
| I102 | 按钮构造 | [companies.js:468](../../src/dalton_core/cockpit_v2/pages/companies.js#L468) | ui.button("恢复按行业或项目默认", () =&gt; choose(null, reset), {variant: "quiet"}) : null; el.append(h("details", {class: "v2-more", id: "model-template-default"}, h("summary", null, "更改这家公司的默认模板"), rea |
| I103 | 动态控件构造 | [companies.js:469](../../src/dalton_core/cockpit_v2/pages/companies.js#L469) | h("summary", null, "更改这家公司的默认模板"), reason.wrap, h("div", {class: "v2-actions"}, setDefault, reset), result, ui.note("项目默认和按行业指定的模板在研究设置的「报告模板」里查看；上传新模板也在那里。"))); } async function modelSectio |
| I104 | 动态控件构造 | [companies.js:496](../../src/dalton_core/cockpit_v2/pages/companies.js#L496) | h("summary", null, ˋ${missing.length} 项结果缺少计算条件（不能当作正式预测使用）ˋ), h("ul", null, missing.map(x =&gt; h("li", null, say(typeof x === "string" ? x : x.label &#124;&#124; JSON.stringify(x))))))); if (m.table) e |
| I105 | 动态控件构造 | [companies.js:500](../../src/dalton_core/cockpit_v2/pages/companies.js#L500) | h("summary", null, "版本记录"), h("ul", null, m.history.map(x =&gt; h("li", null, ˋ第 ${x.version} 版 · ${fmt.datetime(x.created_at)} · ${say(x.change_reason_label &#124;&#124; "")}ˋ))))); }); await exporting; |
| I106 | 动态控件构造 | [companies.js:525](../../src/dalton_core/cockpit_v2/pages/companies.js#L525) | h("summary", null, "完整理由"), ...ui.paragraphs(say(j.because), ctx.seen)))))); out.push(ui.note(ˋ共 ${fmt.number(c.judgements.total)} 条研究判断，显示最近 ${judgements.length} 条（相同理由已合并）。ˋ)); } else out. |
| I107 | 动态控件构造 | [companies.js:557](../../src/dalton_core/cockpit_v2/pages/companies.js#L557) | h("summary", null, ˋ数据更新与跟踪频率（${cadence.length} 项）ˋ), ui.table({caption: "每类资料多久更新一次；「系统已调整」表示研究系统按最近的事件改过频率", columns: [ {key: "source", label: "资料", render: r =&gt; say(r.source_label &#124;&#124; "资料" |
| I108 | 动态控件构造 | [companies.js:591](../../src/dalton_core/cockpit_v2/pages/companies.js#L591) | h("summary", null, lead(say(x.narrative), 60)), ...ui.paragraphs(say(x.narrative)))}, {key: "triggers", label: "触发与失效条件", render: x =&gt; { const conditions = [...(x.triggers &#124;&#124; []), ...(x.inva |
| I109 | 动态控件构造 | [companies.js:603](../../src/dalton_core/cockpit_v2/pages/companies.js#L603) | h("a", {href: href({page: "tasks"})}, "请确认情景假设"))); }); const live = c.scenarios?.in_force &#124;&#124; []; if (!live.length) return; const key = companyKey(c); if (ctx.state.filters.edit === "probabi |
| I110 | 动态控件构造 | [companies.js:625](../../src/dalton_core/cockpit_v2/pages/companies.js#L625) | h("a", {class: "v2-btn v2-btn-secondary", href: href({page: "company", company: key, tab: "scenarios", filters: {edit: "probabilities"}})}, "调整情景概率"), h("span", {class: "v2-note"}, "在一张表里同时调 |
| I111 | 动态控件构造 | [companies.js:682](../../src/dalton_core/cockpit_v2/pages/companies.js#L682) | h("summary", null, "尚不能确认"), h("ul", null, gaps.filter(Boolean).map(g =&gt; h("li", null, say(g)))))); const srcs = s.sources &#124;&#124; []; if (srcs.length) el.append(h("details", {class: "v2-more"}, |
| I112 | 动态控件构造 | [companies.js:684](../../src/dalton_core/cockpit_v2/pages/companies.js#L684) | h("summary", null, ˋ来源（${srcs.length}）ˋ), h("ol", {class: "v2-cite"}, srcs.map(x =&gt; h("li", null, h("span", {lang: untranslated(x.text) ? "en" : null}, x.text &#124;&#124; "来源信息待补齐"), x.period ? ˋ · $ |
| I113 | 按钮构造 | [companies.js:698](../../src/dalton_core/cockpit_v2/pages/companies.js#L698) | ui.button("下载研究报告", () =&gt; exportFile(ctx, c, "html", html, status), {variant: "secondary"}); fill(section, products.length ? ui.table({ caption: "本公司的研究报告；点「阅读」在右侧打开，关闭后回到这里", columns: [ {ke |
| I114 | 按钮构造 | [companies.js:707](../../src/dalton_core/cockpit_v2/pages/companies.js#L707) | ui.button("阅读", () =&gt; openReader(ctx, p), {variant: "quiet", "aria-label": ˋ阅读${say(p.label)}ˋ}) : "—")}, ], rows: products, }) : ui.empty("还没有研究报告。"), h("div", {class: "v2-actions"}, html, |

## cockpit_v2/pages/decision_basis.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I115 | 动态控件构造 | [decision_basis.js:32](../../src/dalton_core/cockpit_v2/pages/decision_basis.js#L32) | h("summary", null, ˋ可能漏掉的争论（${r.missed_debates.length}）ˋ), h("ul", null, r.missed_debates.map(m =&gt; h("li", null, text(ctx, r, m.question))))) : null, follow.length && !isPending ? h("p", {cl |
| I116 | 动态控件构造 | [decision_basis.js:40](../../src/dalton_core/cockpit_v2/pages/decision_basis.js#L40) | h("summary", null, "投资建议的三部分依据"), h("h4", null, ˋ① 证据置信度：${say(c.evidence?.level_label &#124;&#124; "未注明")}ˋ), c.evidence?.statement ? h("p", null, say(c.evidence.statement)) : null, c.evidence?.rests |
| I117 | 动态控件构造 | [decision_basis.js:62](../../src/dalton_core/cockpit_v2/pages/decision_basis.js#L62) | h("summary", null, "投资备忘录的核验依据"), h("h4", null, ˋ生成记录（${groups.length}）ˋ), groups.length ? h |
| I118 | 动态控件构造 | [decision_basis.js:81](../../src/dalton_core/cockpit_v2/pages/decision_basis.js#L81) | h("summary", null, "完整复盘依据")); if (pending(it)) { box.append(ui.note(PENDING_TEXT)); return box; } box.append(h("h4", null, say(z.title &#124;&#124; "从零复盘"))); for (const s of z.sections &#124;&#124; []) box.ap |

## cockpit_v2/pages/feedback.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I119 | 表单字段 | [feedback.js:46](../../src/dalton_core/cockpit_v2/pages/feedback.js#L46) | ui.field({id: ˋ${base}-noteˋ, label: "补充意见（选填）", control: "textarea", maxLength: NOTE_MAX, help: "写下理由或希望补充的证据；和反馈一起存档。", attrs: {rows: "2", class: "v2-input-short"}}); ctx.drafts.bind(note. |
| I120 | 动态控件构造 | [feedback.js:50](../../src/dalton_core/cockpit_v2/pages/feedback.js#L50) | h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle", "aria-pressed": given.has(v.value) ? "true" : "false", "data-verdict": v.value}, v.label)); const send = async button =&gt; |
| I121 | 动态控件构造 | [feedback.js:86](../../src/dalton_core/cockpit_v2/pages/feedback.js#L86) | h("summary", null, given ? ˋ${label}（已给 ${given} 次）ˋ : label), bar); } |

## cockpit_v2/pages/overview.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I122 | 动态控件构造 | [overview.js:56](../../src/dalton_core/cockpit_v2/pages/overview.js#L56) | h("a", {class: "v2-btn", href: href({page: "settings", filters: {focus: "goal"}})}, "设定研究目标")))); renderDecisions(root, ctx, nh); if (ctx.state.filters.panel) openPanel(ctx, overview); retur |
| I123 | 动态控件构造 | [overview.js:63](../../src/dalton_core/cockpit_v2/pages/overview.js#L63) | h("summary", null, ˋ研究目标：${say(goal.display_title &#124;&#124; goal.title)}ˋ), ...ui.paragraphs(say(goal.display_objective &#124;&#124; goal.objective), ctx.seen)); root.append(about); // The headline block (th |
| I124 | 动态控件构造 | [overview.js:87](../../src/dalton_core/cockpit_v2/pages/overview.js#L87) | h("a", {href: href({page: "company", company: r.key})}, r.name)), h("p", null, r.view ? ˋ核心情景：${r.view}ˋ : h("span", {class: "v2-note"}, "尚未形成投资判断")), j ? h("p", {class: "v2-note"}, ˋ最近一次研究判 |
| I125 | 动态控件构造 | [overview.js:100](../../src/dalton_core/cockpit_v2/pages/overview.js#L100) | h("a", {href: href({page: "company", company: companyKey(x.company), tab: x.tab})}, "查看详情"))))); } else { const last = lastJudgement(overview); changeSection.append(ui.empty(last ? ˋ近期没有观点调整 |
| I126 | 动态控件构造 | [overview.js:139](../../src/dalton_core/cockpit_v2/pages/overview.js#L139) | h("a", {href: href({filters: {...keep, panel: "reflection"}})}, "本周复盘")), h("p", {class: "v2-note"}, "这一周的研究花费、历史判断跟踪结果，以及下周可以研究的问题。"), overview.ops?.acceptance?.note ? h("p", {class: "v2-no |
| I127 | 动态控件构造 | [overview.js:142](../../src/dalton_core/cockpit_v2/pages/overview.js#L142) | h("a", {href: href({filters: {...keep, panel: "trajectory"}})}, "研究轨迹")), h("p", {class: "v2-note"}, "系统最近每一步做了什么：AI 调用、研究任务和预算。")), ctx.adminHref() ? h("li", {class: "v2-card"}, h("h3", nul |
| I128 | 动态控件构造 | [overview.js:144](../../src/dalton_core/cockpit_v2/pages/overview.js#L144) | h("a", {href: "/admin#health"}, "运行故障与失败台账")), h("p", {class: "v2-note"}, "受阻任务、等待外部服务的工作和已停止的尝试（管理员页面）。")) : null)); root.append(records); if (ctx.state.filters.panel) openPanel(ctx, overvi |
| I129 | 动态控件构造 | [overview.js:162](../../src/dalton_core/cockpit_v2/pages/overview.js#L162) | h("a", {href: href({page: "tasks"})}, ˋ查看全部 ${items.length} 项并处理ˋ))); } } root.append(section); } async function update(ctx) { // The side panel follows ?panel= / ?kind= without rebuilding t |

## cockpit_v2/pages/policy.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I130 | 动态控件构造 | [policy.js:86](../../src/dalton_core/cockpit_v2/pages/policy.js#L86) | h("select", null, (opts &#124;&#124; []).map(o =&gt; h("option", {value: o.value, selected: o.value === values[key] &#124;&#124; null}, say(o.label)))); const number = |
| I131 | 动态控件构造 | [policy.js:87](../../src/dalton_core/cockpit_v2/pages/policy.js#L87) | h("input", {type: "text", inputmode: unit === "美元" ? "decimal" : "numeric", class: "v2-input", value: values[key] ?? "", size: "6"}); const checks = |
| I132 | 动态控件构造 | [policy.js:91](../../src/dalton_core/cockpit_v2/pages/policy.js#L91) | h("input", {type: "checkbox", value: o.value, id: ˋpol-${key}-${o.value}ˋ}); input.checked = set.has(o.value); keyOf.set(input, key); return h("label", {class: "v2-check", for: input.id}, in |
| I133 | 动态控件构造 | [policy.js:105](../../src/dalton_core/cockpit_v2/pages/policy.js#L105) | h("input", {type: "text", inputmode: "decimal", class: "v2-input v2-num", id, size: "5", value: values[key]?.[k] ?? "", "aria-describedby": ˋpol-${key}-${k}-errˋ}); controls.set(ˋ${key}.${k} |
| I134 | 表单字段 | [policy.js:114](../../src/dalton_core/cockpit_v2/pages/policy.js#L114) | ui.field({id: "policy-reason", label: "发布理由", required: true, maxLength: REASON_MAX, help: "为什么要改；会和这一版规则一起存档。", attrs: {rows: "2"}}); ctx.drafts.bind(reason.input, "policy:reason"); const s |
| I135 | 动态控件构造 | [policy.js:118](../../src/dalton_core/cockpit_v2/pages/policy.js#L118) | h("button", {type: "submit", class: "v2-btn"}, "发布新一版规则"); const reset = ui.button |
| I136 | 按钮构造 | [policy.js:119](../../src/dalton_core/cockpit_v2/pages/policy.js#L119) | ui.button("恢复为当前生效的规则", () =&gt; { ctx.drafts.clear("policy:form"); write(start); status.replaceChildren(ui.receipt("表单已恢复为当前生效的规则；还没有发布任何修改。")); }, {variant: "quiet"}); const read = () =&gt; { co |
| I137 | 表单字段 | [policy.js:247](../../src/dalton_core/cockpit_v2/pages/policy.js#L247) | ui.field({id, label, control: "select"}); f.input.append(...choices.map(c =&gt; h("option", {value: c.key, selected: c.key === current &#124;&#124; null}, say(c.label)))); f.input.addEventListener |

## cockpit_v2/pages/research_log.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I138 | 按钮构造 | [research_log.js:74](../../src/dalton_core/cockpit_v2/pages/research_log.js#L74) | ui.button("写进「调整研究方向」", () =&gt; { ctx.drafts.set("steer:text", withQuestion(ctx.drafts.get("steer:text"), say(c.question))); ctx.navigate({page: "settings", filters: {focus: "steer"}}); }, {va |
| I139 | 动态控件构造 | [research_log.js:82](../../src/dalton_core/cockpit_v2/pages/research_log.js#L82) | h("a", {href: href({page: "settings", filters: {focus: "brief"}})}, "每周简报设置"))); return box; } function eventRow(ctx, e) { const {h, ui, fmt, say} = ctx; return h("li", null, h("details", {c |
| I140 | 动态控件构造 | [research_log.js:89](../../src/dalton_core/cockpit_v2/pages/research_log.js#L89) | h("summary", null, h("span", {class: "v2-num"}, fmt.datetime(e.at)), " ", ui.tag(KIND_LABEL[e.kind] &#124;&#124; "其他"), " ", ui.tag(STATUS[e.status] &#124;&#124; "状态未知", TONE[e.status] &#124;&#124; null), " ", say(e.titl |
| I141 | 动态控件构造 | [research_log.js:105](../../src/dalton_core/cockpit_v2/pages/research_log.js#L105) | h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle", "data-k": k.key, "aria-pressed": (state.kind &#124;&#124; "") === k.key ? "true" : "false", on: {click: |
| I142 | 按钮构造 | [research_log.js:111](../../src/dalton_core/cockpit_v2/pages/research_log.js#L111) | ui.button("加载更早的记录", async () =&gt; { more.disabled = true; try { await onOlder(); } catch (e) { box.append(ui.alert(ˋ没有读到更早的记录：${e.display_message &#124;&#124; "请稍后重试"}ˋ, "danger")); } finally { more.di |

## cockpit_v2/pages/scenario_edit.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I143 | 动态控件构造 | [scenario_edit.js:81](../../src/dalton_core/cockpit_v2/pages/scenario_edit.js#L81) | h("input", {type: "text", inputmode: "numeric", class: "v2-input v2-num", id: sid, value: keep && keep.value != null ? String(keep.value) : String(current ?? ""), "aria-describedby": ˋ${sid} |
| I144 | 动态控件构造 | [scenario_edit.js:83](../../src/dalton_core/cockpit_v2/pages/scenario_edit.js#L83) | h("input", {type: "checkbox", id: ˋ${sid}-retireˋ}); retire.checked = Boolean(keep && keep.retire); const err = h("div", {class: "v2-field-error", id: ˋ${sid}-errˋ, "aria-live": "polite"}); |
| I145 | 表单字段 | [scenario_edit.js:96](../../src/dalton_core/cockpit_v2/pages/scenario_edit.js#L96) | ui.field({id: "scenario-reason", label: "调整理由", required: true, maxLength: REASON_MAX, help: "例如：本季度订单数据弱于预期，下调乐观情景。理由会和每个情景的新版本一起存档。", attrs: {rows: "2"}}); if (held.reason && !reason.input |
| I146 | 动态控件构造 | [scenario_edit.js:100](../../src/dalton_core/cockpit_v2/pages/scenario_edit.js#L100) | h("button", {type: "submit", class: "v2-btn"}, "保存这组概率"); const close = ui.button |
| I147 | 按钮构造 | [scenario_edit.js:101](../../src/dalton_core/cockpit_v2/pages/scenario_edit.js#L101) | ui.button("收起", () =&gt; onClose && onClose(), {variant: "quiet"}); const discard = ui.button("放弃修改", () =&gt; { ctx.drafts.clear(draftKey); for (const x of inputs.values()) { x.input.value = Stri |
| I148 | 按钮构造 | [scenario_edit.js:102](../../src/dalton_core/cockpit_v2/pages/scenario_edit.js#L102) | ui.button("放弃修改", () =&gt; { ctx.drafts.clear(draftKey); for (const x of inputs.values()) { x.input.value = String(x.current ?? ""); x.retire.checked = false; } reason.input.value = ""; refresh |

## cockpit_v2/pages/settings.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I149 | 按钮构造 | [settings.js:25](../../src/dalton_core/cockpit_v2/pages/settings.js#L25) | ui.button("确认发布目标", async () =&gt; { publish.disabled = discard.disabled = true; result.replaceChildren(ui.receipt("正在发布…")); try { const out = await ctx.api.post("/v1/cockpit/publish", {draft_ |
| I150 | 按钮构造 | [settings.js:35](../../src/dalton_core/cockpit_v2/pages/settings.js#L35) | ui.button("放弃这份草稿", async () =&gt; { publish.disabled = discard.disabled = true; try { await ctx.api.post("/v1/cockpit/decide", {kind: "draft:goal", ref: r.draft_id, hash: r.draft_hash, decisio |
| I151 | 动态控件构造 | [settings.js:110](../../src/dalton_core/cockpit_v2/pages/settings.js#L110) | h("summary", null, "目标全文与研究问题"), ...ui.paragraphs(say(goal.display_objective &#124;&#124; goal.objective)), h("ol", null, (goal.research_questions &#124;&#124; []).map(q =&gt; h("li", null, say(typeof q === "strin |
| I152 | 动态控件构造 | [settings.js:113](../../src/dalton_core/cockpit_v2/pages/settings.js#L113) | h("summary", null, ˋ研究目标的历史版本（${versions.length}）ˋ), ui.table({caption: "每次确认目标或研究范围变更都会生成一个新版本；最新的在最上面", columns: [ {key: "v", label: "版本", render: x =&gt; ˋ第 ${x.version} 版ˋ}, {key: "at", lab |
| I153 | 动态控件构造 | [settings.js:130](../../src/dalton_core/cockpit_v2/pages/settings.js#L130) | h("summary", null, ˋ待核实的问题（${plan.inquiries.length}）ˋ), h("ul", null, plan.inquiries.map(q =&gt; h("li", null, ˋ${say(q.subject &#124;&#124; "")}：${say(q.question &#124;&#124; "")}ˋ, q.wants ? h("div", {class: "v2 |
| I154 | 表单字段 | [settings.js:149](../../src/dalton_core/cockpit_v2/pages/settings.js#L149) | ui.field({id: "goal-text", label: goal ? "新的研究目标" : "研究目标", required: true, maxLength: 4000, help: goal ? "写下要改变的研究方向、公司或问题。系统会整理成草案，你确认后才生效。" : "写下要研究的行业、公司范围和想回答的问题，例如：美国云计算基础设施公司未来四个季度的资本 |
| I155 | 按钮构造 | [settings.js:155](../../src/dalton_core/cockpit_v2/pages/settings.js#L155) | ui.button("整理成研究目标草案", null); submit.type = "submit"; const modelOk = o.model_available?.available !== false; if (!modelOk) { submit.disabled = true; status.append(ui.alert(say(o.model_avail |
| I156 | 动态控件构造 | [settings.js:224](../../src/dalton_core/cockpit_v2/pages/settings.js#L224) | h("a", {class: "v2-btn v2-btn-secondary", href: "/weekly-brief"}, "打开每周简报设置")), ui.note("设置页属于当前研究项目；看完后用浏览器的「返回」回到这里。"))); // 6. 管理员 const admin = ui.section("admin", "AI 服务、资料来源与诊断"); cons |
| I157 | 动态控件构造 | [settings.js:234](../../src/dalton_core/cockpit_v2/pages/settings.js#L234) | h("a", {class: "v2-btn v2-btn-secondary", href}, "打开管理员界面")); else el.append(h("p", null, "管理员界面尚未开放。AI 服务、资料来源和诊断目前仍在", h("a", {href: "/legacy"}, "旧版工作台"), "里处理。")); }); if (ctx.state.filte |
| I158 | 动态控件构造 | [settings.js:235](../../src/dalton_core/cockpit_v2/pages/settings.js#L235) | h("a", {href: "/legacy"}, "旧版工作台"), "里处理。")); }); if (ctx.state.filters.focus) setTimeout |
| I159 | 动态控件构造 | [settings.js:254](../../src/dalton_core/cockpit_v2/pages/settings.js#L254) | h("summary", null, "全部规则"), ui.kv(fields.slice(6).map(f =&gt; [say(f.label), say(String(f.value ?? "—"))]))) : null, v.form ? policyEditor(ctx, v, {onPublished: async out =&gt; { policyReceipt = ˋ |

## cockpit_v2/pages/steer.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I160 | 按钮构造 | [steer.js:34](../../src/dalton_core/cockpit_v2/pages/steer.js#L34) | ui.button("放弃这份草案", () =&gt; decide(discard), {variant: "quiet"}); box.append(h("div", {class: "v2-actions"}, discard), result); } else { const publish = ui.button("确认研究范围变更", async () =&gt; { pub |
| I161 | 按钮构造 | [steer.js:37](../../src/dalton_core/cockpit_v2/pages/steer.js#L37) | ui.button("确认研究范围变更", async () =&gt; { publish.disabled = discard.disabled = true; result.replaceChildren(ui.receipt("正在发布…")); try { const out = await ctx.api.post("/v1/cockpit/publish", {draf |
| I162 | 按钮构造 | [steer.js:47](../../src/dalton_core/cockpit_v2/pages/steer.js#L47) | ui.button("放弃这份草案", () =&gt; decide(discard, publish), {variant: "quiet"}); box.append(ui.note("确认后发布为新一版研究目标；在此之前研究方向不会改变。"), h("div", {class: "v2-actions"}, publish, discard), result); } asyn |
| I163 | 动态控件构造 | [steer.js:72](../../src/dalton_core/cockpit_v2/pages/steer.js#L72) | h("summary", null, ˋ更早的 ${items.length - 5} 条ˋ), h("ul", {class: "v2-list"}, items.slice(5).map(row))) : null); } /** The steer form with its draft and history (#/settings?focus=steer). */ e |
| I164 | 表单字段 | [steer.js:85](../../src/dalton_core/cockpit_v2/pages/steer.js#L85) | ui.field({id: "steer-text", label: "想怎样调整研究方向", required: true, maxLength: TEXT_MAX, help: "例如：本周加大对 IBM 软件和云业务的跟踪；暂时不看大众科技媒体的通稿。系统会整理成具体改动，你确认后才生效。"}); ctx.drafts.bind(text.input, "steer:te |
| I165 | 动态控件构造 | [steer.js:90](../../src/dalton_core/cockpit_v2/pages/steer.js#L90) | h("button", {type: "submit", class: "v2-btn"}, "整理成调整方案"); const replaceNote = h("p", {class: "v2-note", hidden: true}, "上面还有一份没有确认的草案；重新整理会替换它，并再产生一次 AI 费用。"); if (!modelOk) { submit.disabl |

## cockpit_v2/pages/task_entry.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I166 | 表单字段 | [task_entry.js:21](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L21) | ui.field({id: ˋwhy-${slug(key)}ˋ, label: "理由（必填，会和操作一起存档）", required: true, maxLength: 600}); const result = h("div", {role: "status"}); const go = ui.button |
| I167 | 按钮构造 | [task_entry.js:23](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L23) | ui.button(submit, async () =&gt; { if (!reason.check()) { reason.input.focus(); return; } if (confirm && !globalThis.confirm(confirm)) return; go.disabled = true; result.replaceChildren(ui.rece |
| I168 | 按钮构造 | [task_entry.js:43](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L43) | ui.button(label, () =&gt; { open.hidden = true; box.append(build(() =&gt; { box.lastChild?.remove(); open.hidden = false; })); }, {variant}); box.append(open); return box; } function acknowledgeBo |
| I169 | 动态控件构造 | [task_entry.js:54](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L54) | h("select", {id: ˋdays-${slug(it.ref)}ˋ, "aria-label": "暂不提醒的天数"}, (c.days &#124;&#124; [7]).map(d =&gt; h("option", {value: String(d), selected: d === 7 ? true : null}, ˋ${d} 天ˋ))); return reasonForm(ct |
| I170 | 动态控件构造 | [task_entry.js:79](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L79) | h("input", {type: "number", min: "0.01", max: "20", step: "0.01", id: ˋcap-${slug(it.ref)}ˋ, "aria-label": "这次愿意花的上限（美元）"}) : null; return reasonForm(ctx, {key: ˋ${door.door}-${it.ref}ˋ, lab |
| I171 | 动态控件构造 | [task_entry.js:90](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L90) | h("a", {class: "v2-btn v2-btn-quiet", href: entry.href}, entry.label) : null; return h("div", null, h("div", {class: "v2-actions"}, ...doors, admin), entry.note ? ui.note(entry.note) : null) |
| I172 | 动态控件构造 | [task_entry.js:103](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L103) | h("a", {class: "v2-btn v2-btn-secondary", href: entry.href}, entry.label), entry.note ? ui.note(entry.note) : null); } if (entry.mode === "acknowledge" && (entry.choices &#124;&#124; []).length) { con |
| I173 | 动态控件构造 | [task_entry.js:108](../../src/dalton_core/cockpit_v2/pages/task_entry.js#L108) | h("a", {class: "v2-btn v2-btn-quiet", href: second.href, target: "_blank", rel: "noopener noreferrer"}, second.label) : null; return h("div", null, acknowledgeBox(ctx, it, entry), link, entr |

## cockpit_v2/pages/tasks.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I174 | 动态控件构造 | [tasks.js:28](../../src/dalton_core/cockpit_v2/pages/tasks.js#L28) | h("a", {class: "v2-btn v2-btn-secondary", href}, "交给管理员处理"); const status = h("span", {class: "v2-note", role: "status"}); const copyButton = ui.button |
| I175 | 按钮构造 | [tasks.js:30](../../src/dalton_core/cockpit_v2/pages/tasks.js#L30) | ui.button("复制给管理员的摘要", async () =&gt; { const text = ˋ${ctx.say(it.title)}\n编号：${it.ref}ˋ; try { await globalThis.navigator.clipboard.writeText(text); status.textContent = "已复制，可以发给管理员。"; } cat |
| I176 | 按钮构造 | [tasks.js:42](../../src/dalton_core/cockpit_v2/pages/tasks.js#L42) | ui.button("停止这项专项研究", () =&gt; { stop.hidden = true; const reason = ui.field({id: ˋstop-${it.ref.replace(/[^a-z0-9]/gi, "").slice(-24)}ˋ, label: "停止原因（会存档）", required: true, maxLength: 500}); c |
| I177 | 表单字段 | [tasks.js:44](../../src/dalton_core/cockpit_v2/pages/tasks.js#L44) | ui.field({id: ˋstop-${it.ref.replace(/[^a-z0-9]/gi, "").slice(-24)}ˋ, label: "停止原因（会存档）", required: true, maxLength: 500}); ctx.drafts.bind(reason.input, ˋstop:${d.admission_ref}ˋ); const re |
| I178 | 按钮构造 | [tasks.js:47](../../src/dalton_core/cockpit_v2/pages/tasks.js#L47) | ui.button("确认停止", async () =&gt; { if (!reason.check()) { reason.input.focus(); return; } confirm.disabled = true; cancel.disabled = true; try { const out = await ctx.api.post("/v1/cockpit/stop |
| I179 | 按钮构造 | [tasks.js:62](../../src/dalton_core/cockpit_v2/pages/tasks.js#L62) | ui.button("取消", () =&gt; { form.remove(); stop.hidden = false; stop.focus(); }, {variant: "quiet"}); const form = h("div", {class: "v2-panel"}, reason.wrap, ui.note("停止后这项研究不再继续；已有成果和记录保留。"), h |
| I180 | 动态控件构造 | [tasks.js:76](../../src/dalton_core/cockpit_v2/pages/tasks.js#L76) | h("a", {class: "v2-btn v2-btn-quiet", href: href({page: "company", company: object.key, tab: k.group === "material" ? "risks" : "view"})}, "查看公司研究") : null; if (k.entry === "goal") return h( |
| I181 | 动态控件构造 | [tasks.js:77](../../src/dalton_core/cockpit_v2/pages/tasks.js#L77) | h("a", {class: "v2-btn", href: href({page: "settings", filters: {focus: "goal"}})}, "设定研究目标"); if (k.entry === "approval") return hasApprovals ? ctx.ui.button |
| I182 | 按钮构造 | [tasks.js:79](../../src/dalton_core/cockpit_v2/pages/tasks.js#L79) | ctx.ui.button("去确认", () =&gt; { const el = globalThis.document.getElementById("approvals-title"); el?.scrollIntoView({block: "start"}); el?.focus({preventScroll: true}); }) : h("span", null, co |
| I183 | 动态控件构造 | [tasks.js:99](../../src/dalton_core/cockpit_v2/pages/tasks.js#L99) | h("summary", null, "原因"), h("p", null, say(it.why_blocked)))); const entry = entryFor(ctx, it, object, hasApprovals); if (entry) card.append(h("div", {class: "v2-actions"}, entry)); return c |
| I184 | 表单字段 | [tasks.js:120](../../src/dalton_core/cockpit_v2/pages/tasks.js#L120) | ui.field({id: ˋwhy-${String(it.ref &#124;&#124; "").replace(/[^a-z0-9]/gi, "").slice(-24)}ˋ, label: "补充说明（会随决定保存）", help: say(it.rationale_hint &#124;&#124; "选填"), maxLength: 2000}); ctx.drafts.bind(rationale.i |
| I185 | 按钮构造 | [tasks.js:129](../../src/dalton_core/cockpit_v2/pages/tasks.js#L129) | ui.button(say(a.label), |
| I186 | 按钮构造 | [tasks.js:167](../../src/dalton_core/cockpit_v2/pages/tasks.js#L167) | ui.button("重试", () =&gt; ctx.navigate({}, {replace: true}), {variant: "quiet"}))); needs = last; } else ctx.drafts.setJson("tasks:last", {as_of: needs.as_of, items: needs.items &#124;&#124; []}); const o |
| I187 | 动态控件构造 | [tasks.js:181](../../src/dalton_core/cockpit_v2/pages/tasks.js#L181) | h("a", {href: ctx.href({filters: {}})}, "显示全部"))); const grouping = ctx.state.filters.group === "urgency" ? "urgency" : "kind"; const toggle = h |
| I188 | 按钮构造 | [tasks.js:186](../../src/dalton_core/cockpit_v2/pages/tasks.js#L186) | ui.button(label, () =&gt; ctx.navigate({filters: {...ctx.state.filters, group: value}}, {replace: true}), {variant: grouping === value ? "primary" : "quiet", "aria-pressed": grouping === value |

## cockpit_v2/pages/templates.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I189 | 动态控件构造 | [templates.js:122](../../src/dalton_core/cockpit_v2/pages/templates.js#L122) | h("summary", null, ˋ已登记的模板（${templates.length}）ˋ), ui.table({caption: "登记后的模板在公司页「导出财务模型」里可以选用", columns: [ {key: "label", label: "模板", render: t =&gt; h("span", null, say(t.label), t.is_defaul |
| I190 | 动态控件构造 | [templates.js:129](../../src/dalton_core/cockpit_v2/pages/templates.js#L129) | h("summary", null, "各公司现在使用的模板"), ui.table({caption: "要为某家公司改用其他模板，请到该公司页的「导出财务模型」", columns: [ {key: "c", label: "公司", render: c =&gt; h("a", {href: ctx.href({page: "company", company: company |
| I191 | 动态控件构造 | [templates.js:131](../../src/dalton_core/cockpit_v2/pages/templates.js#L131) | h("a", {href: ctx.href({page: "company", company: companyKey(c.company_ref), tab: "model"})}, say(c.label))}, {key: "t", label: "模板", render: c =&gt; say(c.template_label)}, {key: "b", label: " |
| I192 | 动态控件构造 | [templates.js:141](../../src/dalton_core/cockpit_v2/pages/templates.js#L141) | h("input", {type: "file", id: "tpl-file", accept: ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "aria-describedby": "tpl-file-help tpl-file-error"}); const fileE |
| I193 | 表单字段 | [templates.js:145](../../src/dalton_core/cockpit_v2/pages/templates.js#L145) | ui.field({id: "tpl-name", label: "模板名称", required: true, maxLength: 80, control: "input", help: "例如：TMT 组模型模板"}); const desc = ui.field({id: "tpl-desc", label: "说明（选填）", maxLength: 600, cont |
| I194 | 表单字段 | [templates.js:146](../../src/dalton_core/cockpit_v2/pages/templates.js#L146) | ui.field({id: "tpl-desc", label: "说明（选填）", maxLength: 600, control: "input"}); ctx.drafts.bind(name.input, "template:name"); ctx.drafts.bind(desc.input, "template:desc"); // Sheets: choices |
| I195 | 动态控件构造 | [templates.js:158](../../src/dalton_core/cockpit_v2/pages/templates.js#L158) | h("select", {id, "aria-describedby": ˋ${id}-errorˋ}, h("option", {value: ""}, "请选择"), names.map(n =&gt; h("option", {value: n, selected: guess[role.key] === n &#124;&#124; null}, n))) : h("input", {type: |
| I196 | 动态控件构造 | [templates.js:160](../../src/dalton_core/cockpit_v2/pages/templates.js#L160) | h("input", {type: "text", id, maxlength: "31", "aria-describedby": ˋ${id}-errorˋ, placeholder: ˋ${role.label}的表名ˋ}); sheetControls[role.key] = control; sheetErrors[role.key] = h("div", {clas |
| I197 | 动态控件构造 | [templates.js:171](../../src/dalton_core/cockpit_v2/pages/templates.js#L171) | h("input", {type: "checkbox", value: i.value, id: ˋtpl-ind-${i.value}ˋ}); return h("label", {class: "v2-check", for: input.id}, input, ˋ ${say(i.label)}ˋ); }); const indBox = inds.length ? h |
| I198 | 表单字段 | [templates.js:177](../../src/dalton_core/cockpit_v2/pages/templates.js#L177) | ui.field({id: "tpl-slug-mode", label: "登记为", control: "select", help: "选已有模板时，这次上传成为它的新版本；原版本保留。"}); const slugsSeen = [...new Map((view.templates &#124;&#124; []).filter(t =&gt; t.origin !== "builtin"). |
| I199 | 表单字段 | [templates.js:182](../../src/dalton_core/cockpit_v2/pages/templates.js#L182) | ui.field({id: "tpl-slug", label: "模板代号", control: "input", maxLength: 48, help: "小写字母、数字和连字符；一般不用改。"}); const advanced = h("details", {class: "v2-more"}, h("summary", null, "高级：新模板还是新版本、模板代号 |
| I200 | 动态控件构造 | [templates.js:184](../../src/dalton_core/cockpit_v2/pages/templates.js#L184) | h("summary", null, "高级：新模板还是新版本、模板代号"), slugSelect.wrap, slug.wrap); const autoSlug = |
| I201 | 表单字段 | [templates.js:189](../../src/dalton_core/cockpit_v2/pages/templates.js#L189) | ui.field({id: "tpl-reason", label: "理由", required: true, maxLength: REASON_MAX, control: "input", help: "为什么登记这份模板；会和登记记录一起存档。"}); ctx.drafts.bind(reason.input, "template:reason"); const sta |
| I202 | 动态控件构造 | [templates.js:193](../../src/dalton_core/cockpit_v2/pages/templates.js#L193) | h("button", {type: "submit", class: "v2-btn"}, "上传并登记"); fileInput.addEventListener |

## cockpit_v2/shell.js

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I203 | 动态控件构造 | [shell.js:473](../../src/dalton_core/cockpit_v2/shell.js#L473) | h("a", {href, ...attrs}, text); }, button(text, onClick, {variant = "primary", ...attrs} = {}) { const cls = {primary: "v2-btn", secondary: "v2-btn v2-btn-secondary", quiet: "v2-btn v2-btn-q |
| I204 | 动态控件构造 | [shell.js:476](../../src/dalton_core/cockpit_v2/shell.js#L476) | h("button", {type: "button", class: cls, on: onClick ? {click: onClick} : null, ...attrs}, text); }, kv(pairs) { const dl = h("dl", {class: "v2-kv"}); for (const [k, v] of pairs) if (v != nu |
| I205 | 按钮构造 | [shell.js:525](../../src/dalton_core/cockpit_v2/shell.js#L525) | ui.button("重试", retry, {variant: "quiet"})); return box; }, /** Text with finance terms glossed on first use in this page. */ glossed(text, seen) { const span = h("span"); for (const part of |
| I206 | 动态控件构造 | [shell.js:542](../../src/dalton_core/cockpit_v2/shell.js#L542) | h("a", {href: url, download: payload.filename &#124;&#124; "dalton-download"}); doc.body.append(a); a.click(); a.remove(); setTimeout |
| I207 | 动态控件构造 | [shell.js:680](../../src/dalton_core/cockpit_v2/shell.js#L680) | h("a", {href: state.toHash({page: p.id, company: null, tab: null, filters: {}}), "aria-current": p.id === activeNav ? "page" : null}, h("span", {class: "v2-nav-ic", "aria-hidden": "true"}, N |

## cockpit_v2/charts/bridge.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I208 | 动态控件构造 | [bridge.mjs:54](../../src/dalton_core/cockpit_v2/charts/bridge.mjs#L54) | el("button", {type: "button", class: "dv2c-seg-btn", "aria-pressed": String(o.value === b.semantics), text: o.label, onclick: |
| I209 | 动态控件构造 | [bridge.mjs:58](../../src/dalton_core/cockpit_v2/charts/bridge.mjs#L58) | el("select", {class: "dv2c-select", "aria-label": "选择报告期"}, same.map((o) =&gt; el("option", {value: o.ref, selected: o.ref === b.ref, text: ˋ${o.fiscal_label &#124;&#124; ""} · ${o.period}ˋ}))); pick.add |

## cockpit_v2/charts/comparison.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I210 | 动态控件构造 | [comparison.mjs:46](../../src/dalton_core/cockpit_v2/charts/comparison.mjs#L46) | el("button", {type: "button", class: "dv2c-sort", "aria-pressed": String(sorted), onclick: |

## cockpit_v2/charts/core.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I211 | 动态控件构造 | [core.mjs:186](../../src/dalton_core/cockpit_v2/charts/core.mjs#L186) | el("summary", {text: "查看数据表"}), el("div", {class: "dv2c-scroll"}, table)); } export async function fetchChart(kind, params, options = {}) { const query = new URLSearchParams(); for (const [k |

## cockpit_v2/charts/model.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I212 | 动态控件构造 | [model.mjs:39](../../src/dalton_core/cockpit_v2/charts/model.mjs#L39) | svg("rect", {class: "dv2c-forecast-wash", x: bx, y: padT - 18, width: width - padR - bx, height: height - padB - padT + 18})); } for (const t of yt) { root.appendChild(svg("line", {class: "d |
| I213 | 动态控件构造 | [model.mjs:86](../../src/dalton_core/cockpit_v2/charts/model.mjs#L86) | svg("rect", {class: "dv2c-hit", x: padL + band * i, y: 0, width: band, height, tabindex: 0, "aria-label": ˋ${c.title}：${p && p.v !== null ? p.display : "缺失"}ˋ}); const show = |
| I214 | 动态控件构造 | [model.mjs:146](../../src/dalton_core/cockpit_v2/charts/model.mjs#L146) | el("summary", {text: ˋ缺失说明（${footnotes.length} 行有“—”）ˋ}), el |
| I215 | 动态控件构造 | [model.mjs:177](../../src/dalton_core/cockpit_v2/charts/model.mjs#L177) | el("select", {class: "dv2c-select", "aria-label": "选择利润率口径"}, margins.map((m, i) =&gt; el("option", {value: i, text: m.label}))); select.addEventListener("change", () =&gt; drawMargin(Number(selec |

## cockpit_v2/charts/price.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I216 | 动态控件构造 | [price.mjs:34](../../src/dalton_core/cockpit_v2/charts/price.mjs#L34) | svg("rect", {class: "dv2c-event", x: x(i) - 1, y: height - padB + 2, width: 2, height: 6})); } const last = points[points.length - 1]; if (last) root.appendChild(svg("circle", {class: "dv2c- |
| I217 | 动态控件构造 | [price.mjs:42](../../src/dalton_core/cockpit_v2/charts/price.mjs#L42) | svg("rect", {class: "dv2c-hit", x: padL, y: 0, width: width - padL - padR, height, tabindex: 0, "aria-label": "用左右方向键逐日查看收盘价"}); root.appendChild(cross); root.appendChild(hit); return {root, |

## cockpit_v2/charts/scenarios.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I218 | 动态控件构造 | [scenarios.mjs:36](../../src/dalton_core/cockpit_v2/charts/scenarios.mjs#L36) | el("summary", {text: s.linked_drivers.length ? ˋ关键驱动：${s.linked_drivers.join("、")}ˋ : "展开情景叙述"}), el("p", {text: s.narrative &#124;&#124; "未写叙述"}))), el("td", null, s.target_label ? s.target_label : m |

## cockpit_v2/charts/waterfall.mjs

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I219 | 动态控件构造 | [waterfall.mjs:36](../../src/dalton_core/cockpit_v2/charts/waterfall.mjs#L36) | svg("rect", {class: ˋdv2c-bar dv2c-${tone}${r.derived ? " dv2c-derived" : ""}ˋ, x: x(lo), y: cy - bar / 2, width: w, height: bar, rx: 2})); if (i + 1 &lt; rows.length && rows[i + 1].type === "s |
| I220 | 动态控件构造 | [waterfall.mjs:43](../../src/dalton_core/cockpit_v2/charts/waterfall.mjs#L43) | svg("rect", {class: "dv2c-hit", x: 0, y: top, width, height: rowH, tabindex: 0, role: r.onClick ? "button" : null, "aria-label": ˋ${r.label}：${text}${r.derived ? "（推算）" : ""}ˋ}); const show |

## cockpit_v2/shell.html

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I221 | HTML a | [shell.html:11](../../src/dalton_core/cockpit_v2/shell.html#L11) | 跳到正文 |
| I222 | HTML a | [shell.html:14](../../src/dalton_core/cockpit_v2/shell.html#L14) | &lt;a class="v2-brand" href="#/overview"&gt; |
| I223 | HTML a | [shell.html:16](../../src/dalton_core/cockpit_v2/shell.html#L16) | 返回旧版 |
| I224 | HTML select | [shell.html:22](../../src/dalton_core/cockpit_v2/shell.html#L22) | v2-project-select |
| I225 | HTML a | [shell.html:27](../../src/dalton_core/cockpit_v2/shell.html#L27) | 研究受阻：读取中 |
| I226 | HTML button | [shell.html:36](../../src/dalton_core/cockpit_v2/shell.html#L36) | v2-side-close 返回公司研究 |

## cockpit_v2/admin/admin.html

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I227 | HTML a | [admin.html:13](../../src/dalton_core/cockpit_v2/admin/admin.html#L13) | adm-back ← 返回投研工作台 |

## cockpit_weekly_brief.html

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I228 | HTML a | [cockpit_weekly_brief.html:21](../../src/dalton_core/cockpit_weekly_brief.html#L21) | ← 返回投研工作台 |
| I229 | HTML select | [cockpit_weekly_brief.html:31](../../src/dalton_core/cockpit_weekly_brief.html#L31) | weekday |
| I230 | HTML input | [cockpit_weekly_brief.html:32](../../src/dalton_core/cockpit_weekly_brief.html#L32) | hour |
| I231 | HTML input | [cockpit_weekly_brief.html:33](../../src/dalton_core/cockpit_weekly_brief.html#L33) | minute |
| I232 | HTML input | [cockpit_weekly_brief.html:35](../../src/dalton_core/cockpit_weekly_brief.html#L35) | timezone |
| I233 | HTML input | [cockpit_weekly_brief.html:36](../../src/dalton_core/cockpit_weekly_brief.html#L36) | destination |
| I234 | HTML button | [cockpit_weekly_brief.html:37](../../src/dalton_core/cockpit_weekly_brief.html#L37) | save 保存每周简报计划 |

## cockpit_control.html

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I235 | HTML button | [cockpit_control.html:351](../../src/dalton_core/cockpit_control.html#L351) | &lt;button type="button" class="nav-item" data-view="workspace"&gt; |
| I236 | HTML button | [cockpit_control.html:352](../../src/dalton_core/cockpit_control.html#L352) | &lt;button type="button" class="nav-item active" data-view="goal"&gt; |
| I237 | HTML button | [cockpit_control.html:353](../../src/dalton_core/cockpit_control.html#L353) | &lt;button type="button" class="nav-item" data-view="steer"&gt; |
| I238 | HTML button | [cockpit_control.html:354](../../src/dalton_core/cockpit_control.html#L354) | &lt;button type="button" class="nav-item" data-view="log"&gt; |
| I239 | HTML button | [cockpit_control.html:355](../../src/dalton_core/cockpit_control.html#L355) | &lt;button type="button" class="nav-item" data-view="ask"&gt; |
| I240 | HTML button | [cockpit_control.html:356](../../src/dalton_core/cockpit_control.html#L356) | &lt;button type="button" class="nav-item" data-view="approve"&gt; |
| I241 | HTML a | [cockpit_control.html:363](../../src/dalton_core/cockpit_control.html#L363) | try-v2 返回新版 |
| I242 | HTML button | [cockpit_control.html:377](../../src/dalton_core/cockpit_control.html#L377) | workspace-open-models 查看可用模型 |
| I243 | HTML button | [cockpit_control.html:377](../../src/dalton_core/cockpit_control.html#L377) | workspace-open-sources 查看资料来源 |
| I244 | HTML a | [cockpit_control.html:377](../../src/dalton_core/cockpit_control.html#L377) | 每周简报设置 |
| I245 | HTML input | [cockpit_control.html:381](../../src/dalton_core/cockpit_control.html#L381) | workspace-input |
| I246 | HTML button | [cockpit_control.html:383](../../src/dalton_core/cockpit_control.html#L383) | workspace-create 新建空白研究环境 |
| I247 | HTML button | [cockpit_control.html:401](../../src/dalton_core/cockpit_control.html#L401) | hero-models 查看模型与预算 |
| I248 | HTML summary | [cockpit_control.html:402](../../src/dalton_core/cockpit_control.html#L402) | 展开研究问题与执行计划 |
| I249 | HTML summary | [cockpit_control.html:416](../../src/dalton_core/cockpit_control.html#L416) | 目标的修改记录 |
| I250 | HTML summary | [cockpit_control.html:438](../../src/dalton_core/cockpit_control.html#L438) | 展开运行明细 |
| I251 | HTML button | [cockpit_control.html:453](../../src/dalton_core/cockpit_control.html#L453) | open-sources 资料来源 |
| I252 | HTML button | [cockpit_control.html:454](../../src/dalton_core/cockpit_control.html#L454) | open-reflection 每周复盘 |
| I253 | HTML button | [cockpit_control.html:455](../../src/dalton_core/cockpit_control.html#L455) | open-models 模型与预算 |
| I254 | HTML textarea | [cockpit_control.html:466](../../src/dalton_core/cockpit_control.html#L466) | goal-input |
| I255 | HTML button | [cockpit_control.html:467](../../src/dalton_core/cockpit_control.html#L467) | goal-submit 整理成研究目标 |
| I256 | HTML textarea | [cockpit_control.html:481](../../src/dalton_core/cockpit_control.html#L481) | steer-input |
| I257 | HTML button | [cockpit_control.html:482](../../src/dalton_core/cockpit_control.html#L482) | steer-submit 生成调整方案 |
| I258 | HTML input | [cockpit_control.html:494](../../src/dalton_core/cockpit_control.html#L494) | lever-reason |
| I259 | HTML input | [cockpit_control.html:494](../../src/dalton_core/cockpit_control.html#L494) | lever-hours |
| I260 | HTML button | [cockpit_control.html:495](../../src/dalton_core/cockpit_control.html#L495) | lever-emergency 紧急停机 |
| I261 | HTML button | [cockpit_control.html:495](../../src/dalton_core/cockpit_control.html#L495) | lever-resume-all 恢复全部运行 |
| I262 | HTML summary | [cockpit_control.html:498](../../src/dalton_core/cockpit_control.html#L498) | 研究车道（按车道暂停） |
| I263 | HTML summary | [cockpit_control.html:499](../../src/dalton_core/cockpit_control.html#L499) | 研究线（停止某一条专项研究） |
| I264 | HTML summary | [cockpit_control.html:500](../../src/dalton_core/cockpit_control.html#L500) | 操作存档 |
| I265 | HTML summary | [cockpit_control.html:506](../../src/dalton_core/cockpit_control.html#L506) | 修改阈值并发布新一版 |
| I266 | HTML summary | [cockpit_control.html:507](../../src/dalton_core/cockpit_control.html#L507) | 对比两个版本 |
| I267 | HTML select | [cockpit_control.html:507](../../src/dalton_core/cockpit_control.html#L507) | cpolicy-left |
| I268 | HTML select | [cockpit_control.html:507](../../src/dalton_core/cockpit_control.html#L507) | cpolicy-right |
| I269 | HTML summary | [cockpit_control.html:508](../../src/dalton_core/cockpit_control.html#L508) | 已发布的版本 |
| I270 | HTML input | [cockpit_control.html:513](../../src/dalton_core/cockpit_control.html#L513) | ftpl-reason |
| I271 | HTML summary | [cockpit_control.html:519](../../src/dalton_core/cockpit_control.html#L519) | 已登记的模板 |
| I272 | HTML summary | [cockpit_control.html:520](../../src/dalton_core/cockpit_control.html#L520) | 上传并登记新模板 |
| I273 | HTML input | [cockpit_control.html:523](../../src/dalton_core/cockpit_control.html#L523) | ftpl-file |
| I274 | HTML input | [cockpit_control.html:525](../../src/dalton_core/cockpit_control.html#L525) | ftpl-slug |
| I275 | HTML input | [cockpit_control.html:527](../../src/dalton_core/cockpit_control.html#L527) | ftpl-name |
| I276 | HTML input | [cockpit_control.html:529](../../src/dalton_core/cockpit_control.html#L529) | ftpl-desc |
| I277 | HTML input | [cockpit_control.html:531](../../src/dalton_core/cockpit_control.html#L531) | ftpl-sheet-valuation |
| I278 | HTML input | [cockpit_control.html:531](../../src/dalton_core/cockpit_control.html#L531) | ftpl-sheet-financials |
| I279 | HTML input | [cockpit_control.html:531](../../src/dalton_core/cockpit_control.html#L531) | ftpl-sheet-driver |
| I280 | HTML button | [cockpit_control.html:534](../../src/dalton_core/cockpit_control.html#L534) | ftpl-submit 上传并登记 |
| I281 | HTML summary | [cockpit_control.html:538](../../src/dalton_core/cockpit_control.html#L538) | 操作存档 |
| I282 | HTML input | [cockpit_control.html:545](../../src/dalton_core/cockpit_control.html#L545) | cov-add |
| I283 | HTML select | [cockpit_control.html:546](../../src/dalton_core/cockpit_control.html#L546) | cov-tier |
| I284 | HTML input | [cockpit_control.html:548](../../src/dalton_core/cockpit_control.html#L548) | cov-reason |
| I285 | HTML button | [cockpit_control.html:549](../../src/dalton_core/cockpit_control.html#L549) | cov-submit 生成名单调整草案 |
| I286 | HTML button | [cockpit_control.html:559](../../src/dalton_core/cockpit_control.html#L559) | open-trajectory 打开执行轨迹（每步调用了什么、结果如何） |
| I287 | HTML button | [cockpit_control.html:561](../../src/dalton_core/cockpit_control.html#L561) | 全部动态 |
| I288 | HTML button | [cockpit_control.html:561](../../src/dalton_core/cockpit_control.html#L561) | 资料检索 |
| I289 | HTML button | [cockpit_control.html:561](../../src/dalton_core/cockpit_control.html#L561) | 资料采集 |
| I290 | HTML button | [cockpit_control.html:562](../../src/dalton_core/cockpit_control.html#L562) | 阅读与提取 |
| I291 | HTML button | [cockpit_control.html:562](../../src/dalton_core/cockpit_control.html#L562) | 新增结论 |
| I292 | HTML button | [cockpit_control.html:562](../../src/dalton_core/cockpit_control.html#L562) | 操作记录 |
| I293 | HTML button | [cockpit_control.html:562](../../src/dalton_core/cockpit_control.html#L562) | 运行问题 |
| I294 | HTML textarea | [cockpit_control.html:576](../../src/dalton_core/cockpit_control.html#L576) | ask-input |
| I295 | HTML button | [cockpit_control.html:577](../../src/dalton_core/cockpit_control.html#L577) | ask-submit 提交问题 |
| I296 | 动态控件构造 | [cockpit_control.html:645](../../src/dalton_core/cockpit_control.html#L645) | node("button","重试语言质检","ghost"),st=node("span","","status");btn.type="button"; btn.onclick=async |
| I297 | 动态控件构造 | [cockpit_control.html:752](../../src/dalton_core/cockpit_control.html#L752) | node("summary",label)); const shown=typeof value==="string"?value:JSON.stringify(value,null,2);d.append(rawNode("pre",shown));return d } function readableLaneDetail(value){ if(typeof value!= |
| I298 | 动态控件构造 | [cockpit_control.html:998](../../src/dalton_core/cockpit_control.html#L998) | node("button","重新读取","ghost");b.type="button";b.onclick=retry;sheet.append(b)}} function stageState(c,ref,index){const at=RESEARCH_STAGES.findIndex(x=&gt;x[0]===c.stage_ref);if(index&lt;at)return" |
| I299 | 动态控件构造 | [cockpit_control.html:1000](../../src/dalton_core/cockpit_control.html#L1000) | node("button",null,"company-launch");b.setAttribute("aria-label",ˋ打开 ${c.name} 研究详情ˋ);b.dataset.companyRef=c.company_ref;const stage=RESEARCH_STAGES.find(x=&gt;x[0]===c.stage_ref)?.[1]&#124;&#124;c.stage |
| I300 | 动态控件构造 | [cockpit_control.html:1005](../../src/dalton_core/cockpit_control.html#L1005) | node("button","关闭","ghost close");close.setAttribute("aria-label","关闭公司详情");close.onclick= |
| I301 | 动态控件构造 | [cockpit_control.html:1012](../../src/dalton_core/cockpit_control.html#L1012) | node("button",label,"btn");b.type="button";b.onclick= |
| I302 | 动态控件构造 | [cockpit_control.html:1017](../../src/dalton_core/cockpit_control.html#L1017) | node("button",label,"detail-tab");b.type="button";b.id=ˋcompany-tab-${i}ˋ;b.setAttribute("role","tab");b.setAttribute("aria-controls",ˋcompany-panel-${i}ˋ);b.onclick= |
| I303 | 动态控件构造 | [cockpit_control.html:1129](../../src/dalton_core/cockpit_control.html#L1129) | node("button",item.current?"当前环境":"进入","ghost"); enter.disabled=item.current&#124;&#124;!item.url;enter.onclick= |
| I304 | 动态控件构造 | [cockpit_control.html:1132](../../src/dalton_core/cockpit_control.html#L1132) | node("button","重命名","ghost"),renameStatus=rawNode("span","","status row-status");renameStatus.setAttribute("role","status");copy.append(renameStatus); rename.onclick=async |
| I305 | 动态控件构造 | [cockpit_control.html:1161](../../src/dalton_core/cockpit_control.html#L1161) | node("button","进入研究环境","primary");enter.onclick= |
| I306 | 动态控件构造 | [cockpit_control.html:1249](../../src/dalton_core/cockpit_control.html#L1249) | node("button","模型与预算","ghost"); budgetEntry.onclick=openModels; $("totals").append(budgetEntry); /* 2026-09-17: the day budget is one number for the whole machine. The mission's own spend on |
| I307 | 动态控件构造 | [cockpit_control.html:1337](../../src/dalton_core/cockpit_control.html#L1337) | node("button","重新读取","ghost");retry.type="button";retry.onclick=box.loadDetail;box.append(retry)})}; return box} function qualityBlock(q){if(!q)return null;const low=(q.below_passing&&q.belo |
| I308 | 动态控件构造 | [cockpit_control.html:1366](../../src/dalton_core/cockpit_control.html#L1366) | node("button",FB_LABELS[v]);if(given.has(v))b.classList.add("on"); b.onclick=async |
| I309 | 动态控件构造 | [cockpit_control.html:1449](../../src/dalton_core/cockpit_control.html#L1449) | node("button","调整概率","ghost");adj.type="button";adj.onclick= |
| I310 | 动态控件构造 | [cockpit_control.html:1450](../../src/dalton_core/cockpit_control.html#L1450) | node("button","停用这个情景","ghost");ret.type="button";ret.onclick= |
| I311 | 动态控件构造 | [cockpit_control.html:1495](../../src/dalton_core/cockpit_control.html#L1495) | node("summary","数据更新与跟踪频率");d.append(s); const ul=node("ul",null,"latest"); rows.forEach(r=&gt;{const li=node("li"); li.append(node("span",ˋ${r.source_label}：${r.interval_label}ˋ,"figv")); li.a |
| I312 | 动态控件构造 | [cockpit_control.html:1639](../../src/dalton_core/cockpit_control.html#L1639) | node("button","↑","ghost"),down=node("button","↓","ghost"),remove=node("button","移除","ghost"); up.type=down.type=remove.type="button";up.setAttribute("aria-label","上移");down.setAttribute("ar |
| I313 | 动态控件构造 | [cockpit_control.html:1639](../../src/dalton_core/cockpit_control.html#L1639) | node("button","↓","ghost"),remove=node("button","移除","ghost"); up.type=down.type=remove.type="button";up.setAttribute("aria-label","上移");down.setAttribute("aria-label","下移"); up.disabled=ind |
| I314 | 动态控件构造 | [cockpit_control.html:1639](../../src/dalton_core/cockpit_control.html#L1639) | node("button","移除","ghost"); up.type=down.type=remove.type="button";up.setAttribute("aria-label","上移");down.setAttribute("aria-label","下移"); up.disabled=index===0;down.disabled=index===picke |
| I315 | 动态控件构造 | [cockpit_control.html:1674](../../src/dalton_core/cockpit_control.html#L1674) | node("button","← 加入链尾","ghost"),fromChain=node("button","从链中移除 →","ghost"); toChain.type=fromChain.type="button"; toChain.onclick= |
| I316 | 动态控件构造 | [cockpit_control.html:1674](../../src/dalton_core/cockpit_control.html#L1674) | node("button","从链中移除 →","ghost"); toChain.type=fromChain.type="button"; toChain.onclick= |
| I317 | 动态控件构造 | [cockpit_control.html:1684](../../src/dalton_core/cockpit_control.html#L1684) | node("button","保存这一类的模型顺序","primary"),st=node("span","","status"); save.dataset.role="tier-save"; save.onclick=async |
| I318 | 动态控件构造 | [cockpit_control.html:1707](../../src/dalton_core/cockpit_control.html#L1707) | node("button","保存输出上限","ghost"),st=node("span","","status");save.type="button"; save.onclick=async |
| I319 | 动态控件构造 | [cockpit_control.html:1750](../../src/dalton_core/cockpit_control.html#L1750) | node("button","↑","ghost"),down=node("button","↓","ghost"),remove=node("button","移除","ghost"); up.type=down.type=remove.type="button";up.setAttribute("aria-label","上移");down.setAttribute("ar |
| I320 | 动态控件构造 | [cockpit_control.html:1750](../../src/dalton_core/cockpit_control.html#L1750) | node("button","↓","ghost"),remove=node("button","移除","ghost"); up.type=down.type=remove.type="button";up.setAttribute("aria-label","上移");down.setAttribute("aria-label","下移"); up.disabled=ind |
| I321 | 动态控件构造 | [cockpit_control.html:1750](../../src/dalton_core/cockpit_control.html#L1750) | node("button","移除","ghost"); up.type=down.type=remove.type="button";up.setAttribute("aria-label","上移");down.setAttribute("aria-label","下移"); up.disabled=index===0;down.disabled=index===picke |
| I322 | 动态控件构造 | [cockpit_control.html:1770](../../src/dalton_core/cockpit_control.html#L1770) | node("button","加入链尾","ghost");add.type="button"; add.onclick= |
| I323 | 动态控件构造 | [cockpit_control.html:1772](../../src/dalton_core/cockpit_control.html#L1772) | node("button","推荐顺序","ghost");recommend.type="button";recommend.dataset.role="verifier-recommend"; const rec=ed.recommended&#124;&#124;{}; recommend.disabled=!(rec.status==="proposed"); recommend.oncl |
| I324 | 动态控件构造 | [cockpit_control.html:1779](../../src/dalton_core/cockpit_control.html#L1779) | node("button","保存","primary"),restore=node("button","恢复系统推荐（按档位）","ghost"),st=node("span","","status"); save.type=restore.type="button";save.dataset.role="verifier-save";restore.dataset.role |
| I325 | 动态控件构造 | [cockpit_control.html:1779](../../src/dalton_core/cockpit_control.html#L1779) | node("button","恢复系统推荐（按档位）","ghost"),st=node("span","","status"); save.type=restore.type="button";save.dataset.role="verifier-save";restore.dataset.role="verifier-restore"; const lines=node( |
| I326 | 动态控件构造 | [cockpit_control.html:1820](../../src/dalton_core/cockpit_control.html#L1820) | node("summary",ˋ其余 ${idle.length} 个核验环节在任何环境都还没有配置ˋ)); idle.forEach(row=&gt;details.append(node("small",ˋ${row.draft_label} → ${row.pairs.map(p=&gt;p.verifier_label).join("、")}ˋ,"raw")));section.a |
| I327 | 动态控件构造 | [cockpit_control.html:1831](../../src/dalton_core/cockpit_control.html#L1831) | node("button","知道了","ghost");b.onclick=async |
| I328 | 动态控件构造 | [cockpit_control.html:1853](../../src/dalton_core/cockpit_control.html#L1853) | node("summary","这一类包含哪些环节");details.append(summary); card.purposes.forEach(p=&gt;{ const line=node("small",ˋ${p.label} · ${p.mode_label}${p.requires_restart?" · 修改需重启":""}ˋ,"raw"); details.appe |
| I329 | 动态控件构造 | [cockpit_control.html:1881](../../src/dalton_core/cockpit_control.html#L1881) | node("button","保存预算上限","primary"),st=node("span","","status"); save.onclick=async |
| I330 | 动态控件构造 | [cockpit_control.html:1898](../../src/dalton_core/cockpit_control.html#L1898) | node("button","补充模型信息","ghost");declare.onclick=async |
| I331 | 动态控件构造 | [cockpit_control.html:1913](../../src/dalton_core/cockpit_control.html#L1913) | node("button","允许使用","ghost");b.onclick=async |
| I332 | 动态控件构造 | [cockpit_control.html:1948](../../src/dalton_core/cockpit_control.html#L1948) | node("button",label,"chip"+(trajFilter===key?" on":""));chip.type="button";chip.setAttribute("aria-pressed",String(trajFilter===key)); chip.onclick= |
| I333 | 动态控件构造 | [cockpit_control.html:2001](../../src/dalton_core/cockpit_control.html#L2001) | node("button","加载更早","ghost");more.className="ghost traj-more"; more.onclick=async |
| I334 | 动态控件构造 | [cockpit_control.html:2070](../../src/dalton_core/cockpit_control.html#L2070) | node("button","查看公司研究结论","ghost"); b.style.cssText="padding:4px 10px;font-size:12px";b.onclick= |
| I335 | 动态控件构造 | [cockpit_control.html:2082](../../src/dalton_core/cockpit_control.html#L2082) | node("button",back?ˋ返回「${back.company.ticker}」研究详情ˋ:"关闭","ghost");b.className="ghost close";b.type="button";b.onclick= |
| I336 | 动态控件构造 | [cockpit_control.html:2083](../../src/dalton_core/cockpit_control.html#L2083) | node("button","重新读取","ghost");retry.type="button";retry.onclick=loadClaims;sheet.append(box,retry)} function renderClaims(sheet,r){sheet.replaceChildren(readerClose(),node("h2","已核实的结论")); c |
| I337 | 动态控件构造 | [cockpit_control.html:2093](../../src/dalton_core/cockpit_control.html#L2093) | node("button",r.filters.canonical_only?"已合并重复（点击展开全部）":"正在显示全部（点击合并重复）","ghost"); dedupe.onclick= |
| I338 | 动态控件构造 | [cockpit_control.html:2109](../../src/dalton_core/cockpit_control.html#L2109) | node("button","上一页","ghost"),next=node("button","下一页","ghost");back.disabled=!claimPages.length;next.disabled=!r.next_cursor; back.onclick= |
| I339 | 动态控件构造 | [cockpit_control.html:2109](../../src/dalton_core/cockpit_control.html#L2109) | node("button","下一页","ghost");back.disabled=!claimPages.length;next.disabled=!r.next_cursor; back.onclick= |
| I340 | 动态控件构造 | [cockpit_control.html:2142](../../src/dalton_core/cockpit_control.html#L2142) | node("button","阅读完整报告","ghost");btn.style.padding="5px 12px";btn.style.fontSize="12px";btn.style.marginTop="8px";btn.onclick= |
| I341 | 动态控件构造 | [cockpit_control.html:2154](../../src/dalton_core/cockpit_control.html#L2154) | node("button",null,"stat link"+(alarm?" alarm":""));d.type="button"; d.append(node("span",String(n),"n"),node("span",l,"l"),node("span",s,"s"),node("span",go,"go"));d.onclick=onclick;return |
| I342 | 动态控件构造 | [cockpit_control.html:2188](../../src/dalton_core/cockpit_control.html#L2188) | node("summary","查看已处理的历史记录"), node("div","这些任务对应的配置或研究目标已更新。这里保留当时的失败记录，最新结果以任务运行情况为准。","hint"));const ul=node("ul",null,"latest"); const addHistory=items=&gt;items.forEach(it=&gt;{const li=node(" |
| I343 | 动态控件构造 | [cockpit_control.html:2195](../../src/dalton_core/cockpit_control.html#L2195) | node("button",ˋ再看一页（共 ${r.historical_count} 条）ˋ,"ghost");more.type="button"; more.onclick=async |
| I344 | 动态控件构造 | [cockpit_control.html:2200](../../src/dalton_core/cockpit_control.html#L2200) | node("summary","查看已停止的研究尝试"), node("div","这里记录未通过内容、证据或格式检查的尝试。后续可在资料或条件更新后重新研究，历史尝试次数不计入当前受阻任务。","hint")); const ul=node("ul",null,"latest"); /* P1-7: the server groups every stopped attemp |
| I345 | 动态控件构造 | [cockpit_control.html:2219](../../src/dalton_core/cockpit_control.html#L2219) | node("button","重新读取","ghost");b.id="overview-retry";b.onclick= |
| I346 | 动态控件构造 | [cockpit_control.html:2256](../../src/dalton_core/cockpit_control.html#L2256) | node("button",kind==="goal"?"确认发布目标":"确认调整","primary"),no=node("button","放弃","ghost"),st=node("span","","status");st.setAttribute("role","status");ok.type=no.type="button";ok.onclick=async |
| I347 | 动态控件构造 | [cockpit_control.html:2256](../../src/dalton_core/cockpit_control.html#L2256) | node("button","放弃","ghost"),st=node("span","","status");st.setAttribute("role","status");ok.type=no.type="button";ok.onclick=async |
| I348 | 动态控件构造 | [cockpit_control.html:2295](../../src/dalton_core/cockpit_control.html#L2295) | node("textarea");reason.id="cpolicy-reason";reason.placeholder="为什么要改（必填，会和这一版一起存档）";reason.style.width="100%";const reasonLabel=node("label","发布理由（必填，会和这一版一起存档）","field-label");reasonLabel. |
| I349 | 动态控件构造 | [cockpit_control.html:2295](../../src/dalton_core/cockpit_control.html#L2295) | node("button","发布新一版","primary"),st=node("span","","status");pub.type="button";a.append(pub,st); pub.onclick=async |
| I350 | 动态控件构造 | [cockpit_control.html:2324](../../src/dalton_core/cockpit_control.html#L2324) | node("button",label,cls);b.type="button";b.onclick=ev=&gt;fn(ev,st);a.append(b)});d.append(left,a);return d} function renderBanner(r){const b=$("lever-banner");if(r&&r.banner){b.className="leve |
| I351 | 动态控件构造 | [cockpit_control.html:2325](../../src/dalton_core/cockpit_control.html#L2325) | node("button","去恢复","ghost");go.type="button";go.onclick= |
| I352 | 动态控件构造 | [cockpit_control.html:2392](../../src/dalton_core/cockpit_control.html#L2392) | node("button","查看引用","ghost");btn.style.padding="4px 10px";btn.style.fontSize="12px";const c=node("div",null,"cites");r.citations.forEach(x=&gt;{const d=node("div",null,"cite"),shown=displayTex |
| I353 | 动态控件构造 | [cockpit_control.html:2396](../../src/dalton_core/cockpit_control.html#L2396) | node("summary","展开投资备忘录核验依据"));const groups=m.producer_groups&#124;&#124;[];box.append(node("h5",ˋ生成记录（${groups.length}）ˋ));if(groups.length){const ul=node("ul",null,"memo-list");groups.forEach |
| I354 | 动态控件构造 | [cockpit_control.html:2409](../../src/dalton_core/cockpit_control.html#L2409) | node("summary","展开完整复盘依据"));if(!finalResearchReady(fields,finalState(it))){review.append(node("div",FINAL_TEXT_PENDING,"empty"));const raw=technicalDetails({original_review:z});if(raw)review |
| I355 | 动态控件构造 | [cockpit_control.html:2409](../../src/dalton_core/cockpit_control.html#L2409) | node("textarea");ta.id=ˋapproval-note-${++approvalNoteSeq}ˋ;ta.placeholder=it.rationale_hint&#124;&#124;"补充说明（选填，会随决定保存）";ta.style.minHeight="64px";ta.value=draft&#124;&#124;it.rationale_default&#124;&#124;"";ta.oninput= |
| I356 | 动态控件构造 | [cockpit_control.html:2409](../../src/dalton_core/cockpit_control.html#L2409) | node("button",ac.label,ac.decision==="reject"&#124;&#124;ac.decision==="discard"?"danger":"primary");if(ac.disabled){btn.disabled=true;if(ac.hint)btn.title=ac.hint}btn.onclick=async |
| I357 | 动态控件构造 | [cockpit_control.html:2417](../../src/dalton_core/cockpit_control.html#L2417) | node("summary",ˋ${group.who&#124;&#124;"未注明公司"} · 历史修订申请 ${group.entries.length} 项（展开逐项决定）ˋ)); group.entries.forEach(card=&gt;d.append(card));content.push(d)}else content.push(...group.entries)}); box.re |
| I358 | 动态控件构造 | [cockpit_control.html:2443](../../src/dalton_core/cockpit_control.html#L2443) | node("summary","展开逐题缺口"));gaps.forEach(g=&gt;list.append(node("div",ˋ第 ${g.question_number} 问缺：${g.missing}；下一步：${g.next_step}ˋ,"gap")));d.append(list)}if((it.detail&#124;&#124;{}).stop_line&&it.detail.a |
| I359 | 动态控件构造 | [cockpit_control.html:2443](../../src/dalton_core/cockpit_control.html#L2443) | node("button","停止这条研究线","ghost");b.type="button";b.onclick= |
| I360 | 动态控件构造 | [cockpit_control.html:2453](../../src/dalton_core/cockpit_control.html#L2453) | node("button",text,"ghost");b.type="button";b.onclick=fn;return b}; switch(entry&&entry.mode){ case "dialog":return WHERE_OPENERS[entry.open]?button(entry.label,WHERE_OPENERS[entry.open]):nu |
| I361 | 动态控件构造 | [cockpit_control.html:2460](../../src/dalton_core/cockpit_control.html#L2460) | node("a",entry.label);a.href=entry.href;a.target="_blank";a.rel="noopener noreferrer";return a} case "none":return node("span",entry.label&#124;&#124;it.where&#124;&#124;"")} return null} function whereNode(it) |
| I362 | 动态控件构造 | [cockpit_control.html:2465](../../src/dalton_core/cockpit_control.html#L2465) | node("button",ˋ打开「${label}」ˋ,"ghost");b.type="button";b.onclick=WHERE_OPENERS[it.where_open];return b} if(it.where_view&&document.querySelector(ˋ.nav-item[data-view="${it.where_view}"]ˋ)){co |
| I363 | 动态控件构造 | [cockpit_control.html:2466](../../src/dalton_core/cockpit_control.html#L2466) | node("button",ˋ${label} 页ˋ,"ghost");b.type="button";b.onclick= |
| I364 | 动态控件构造 | [cockpit_control.html:2468](../../src/dalton_core/cockpit_control.html#L2468) | node("summary","技术详情 · 可复制的命令")); commands.forEach(c=&gt;{if(c.label)d.append(node("div",c.label,"hint"));[["只读预览",c.preview],["执行",c.apply]].forEach |
| I365 | 动态控件构造 | [cockpit_control.html:2470](../../src/dalton_core/cockpit_control.html#L2470) | node("button","复制","ghost");b.type="button";b.onclick= |
| I366 | 动态控件构造 | [cockpit_control.html:2476](../../src/dalton_core/cockpit_control.html#L2476) | node("button","重新读取","ghost");retry.type="button";retry.onclick= |

## cockpit_control_legacy.html

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I367 | HTML button | [cockpit_control_legacy.html:18](../../src/dalton_core/cockpit_control_legacy.html#L18) | 待审 |
| I368 | HTML button | [cockpit_control_legacy.html:18](../../src/dalton_core/cockpit_control_legacy.html#L18) | 轨迹 |
| I369 | HTML button | [cockpit_control_legacy.html:18](../../src/dalton_core/cockpit_control_legacy.html#L18) | 问答 |
| I370 | HTML button | [cockpit_control_legacy.html:18](../../src/dalton_core/cockpit_control_legacy.html#L18) | 方向 |
| I371 | HTML button | [cockpit_control_legacy.html:18](../../src/dalton_core/cockpit_control_legacy.html#L18) | 议程 |
| I372 | HTML select | [cockpit_control_legacy.html:38](../../src/dalton_core/cockpit_control_legacy.html#L38) | answer-subject |
| I373 | HTML textarea | [cockpit_control_legacy.html:39](../../src/dalton_core/cockpit_control_legacy.html#L39) | answer-input |
| I374 | HTML button | [cockpit_control_legacy.html:40](../../src/dalton_core/cockpit_control_legacy.html#L40) | answer-submit 检查并回答 |
| I375 | HTML textarea | [cockpit_control_legacy.html:48](../../src/dalton_core/cockpit_control_legacy.html#L48) | intent-input |
| I376 | HTML button | [cockpit_control_legacy.html:49](../../src/dalton_core/cockpit_control_legacy.html#L49) | intent-submit 生成候选 |
| I377 | 动态控件构造 | [cockpit_control_legacy.html:70](../../src/dalton_core/cockpit_control_legacy.html#L70) | node("button","查看原文与抽取建议","revise"),status=node("span","","result"),body=node("div"); let busy=false,context=null; actions.append(open,status);panel.append(actions,body); const request= |
| I378 | 动态控件构造 | [cockpit_control_legacy.html:77](../../src/dalton_core/cockpit_control_legacy.html#L77) | node("summary","原文版本与完整 SHA-256")); [context.document_ref,context.source_manifest_ref,context.source_manifest_hash,context.source_content_hash,context.content_hash].forEach(value=&gt;refs.appen |
| I379 | 动态控件构造 | [cockpit_control_legacy.html:79](../../src/dalton_core/cockpit_control_legacy.html#L79) | node("summary","已获取原文（不可信内容，仅按文字展示）")); context.quotes.forEach(q=&gt;{const part=node("div");part.append(node("div",ˋ${q.source_start}–${q.source_end} · SHA-256 ${q.source_sha256}ˋ,"ref"));cons |
| I380 | 动态控件构造 | [cockpit_control_legacy.html:81](../../src/dalton_core/cockpit_control_legacy.html#L81) | node("button","上一段","revise"),next=node("button","下一段","revise"),generate=node("button","生成本段抽取建议","accept"); prev.disabled=context.offset===0;next.disabled=context.next_offset===null;genera |
| I381 | 动态控件构造 | [cockpit_control_legacy.html:81](../../src/dalton_core/cockpit_control_legacy.html#L81) | node("button","下一段","revise"),generate=node("button","生成本段抽取建议","accept"); prev.disabled=context.offset===0;next.disabled=context.next_offset===null;generate.disabled=!data.generation_enable |
| I382 | 动态控件构造 | [cockpit_control_legacy.html:81](../../src/dalton_core/cockpit_control_legacy.html#L81) | node("button","生成本段抽取建议","accept"); prev.disabled=context.offset===0;next.disabled=context.next_offset===null;generate.disabled=!data.generation_enabled; prev.onclick= |
| I383 | 动态控件构造 | [cockpit_control_legacy.html:98](../../src/dalton_core/cockpit_control_legacy.html#L98) | node("textarea");input.maxLength=max;input.value=suggestion[name]&#124;&#124;"";input.setAttribute("aria-label",label);fields[name]=input;lab.append(input);editor.append(lab); } const start=node("inpu |
| I384 | 动态控件构造 | [cockpit_control_legacy.html:100](../../src/dalton_core/cockpit_control_legacy.html#L100) | node("input"),end=node("input"),selected=node("pre",q.raw_text,"quote"); selected.style.cssText="white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow-y:auto"; for(const [inp |
| I385 | 动态控件构造 | [cockpit_control_legacy.html:100](../../src/dalton_core/cockpit_control_legacy.html#L100) | node("input"),selected=node("pre",q.raw_text,"quote"); selected.style.cssText="white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow-y:auto"; for(const [input,label,value] of |
| I386 | 动态控件构造 | [cockpit_control_legacy.html:107](../../src/dalton_core/cockpit_control_legacy.html#L107) | node("input"),correctionHash=node("input"); for(const [input,label] of [[correctionRef,"已有修正集版本（可选）"],[correctionHash,"已有修正集 SHA-256（成对填写）"]]){input.setAttribute("aria-label",label);input.st |
| I387 | 动态控件构造 | [cockpit_control_legacy.html:107](../../src/dalton_core/cockpit_control_legacy.html#L107) | node("input"); for(const [input,label] of [[correctionRef,"已有修正集版本（可选）"],[correctionHash,"已有修正集 SHA-256（成对填写）"]]){input.setAttribute("aria-label",label);input.style.width="100%";const lab=no |
| I388 | 动态控件构造 | [cockpit_control_legacy.html:110](../../src/dalton_core/cockpit_control_legacy.html#L110) | node("input");checked.type="checkbox";checked.setAttribute("aria-label","已核对原文引用及校订陈述");confirmLabel.append(checked,document.createTextNode(" 我已核对选中原文、归属、否定和不确定性，确认此引用及校订后的语义陈述。"));editor.ap |
| I389 | 动态控件构造 | [cockpit_control_legacy.html:111](../../src/dalton_core/cockpit_control_legacy.html#L111) | node("button","确认引用并保存为待审候选","accept"),saved=node("span","","result"),controls=node("div",null,"actions"); let frozen=null,submitted=false;submit.disabled=true;checked.onchange= |
| I390 | 动态控件构造 | [cockpit_control_legacy.html:126](../../src/dalton_core/cockpit_control_legacy.html#L126) | node("summary","建议与模型来源记录"));[suggestion.id,suggestion.content_hash,suggestion.producer_ref,suggestion.work_order_ref,suggestion.result_ref,suggestion.result_hash,suggestion.invocation_ref,s |
| I391 | 动态控件构造 | [cockpit_control_legacy.html:144](../../src/dalton_core/cockpit_control_legacy.html#L144) | node("summary","技术详情"),node("div",item.mission_version_ref,"ref"),node("div",item.review_id,"ref"));card.append(refs); card.append(documentEvidencePanel(item)); const label=node("label","关联这 |
| I392 | 动态控件构造 | [cockpit_control_legacy.html:146](../../src/dalton_core/cockpit_control_legacy.html#L146) | node("select"); select.id=ˋcandidate-${item.review_id}ˋ;label.htmlFor=select.id; select.style.cssText="width:100%;padding:10px;margin-top:8px;background:#08141d;color:var(--text);border:1px |
| I393 | 动态控件构造 | [cockpit_control_legacy.html:152](../../src/dalton_core/cockpit_control_legacy.html#L152) | node("textarea");rationale.maxLength=4000;rationale.placeholder="说明抽取结果，或忽略这份文档的原因";rationale.setAttribute("aria-label","处理理由"); const actions=node("div",null,"actions"),staged=node("button" |
| I394 | 动态控件构造 | [cockpit_control_legacy.html:153](../../src/dalton_core/cockpit_control_legacy.html#L153) | node("button","登记抽取完成","accept"),dismiss=node("button","忽略文档","revise"),result=node("span","","result");staged.disabled=true; select.onchange= |
| I395 | 动态控件构造 | [cockpit_control_legacy.html:153](../../src/dalton_core/cockpit_control_legacy.html#L153) | node("button","忽略文档","revise"),result=node("span","","result");staged.disabled=true; select.onchange= |
| I396 | 动态控件构造 | [cockpit_control_legacy.html:170](../../src/dalton_core/cockpit_control_legacy.html#L170) | node("button","确认未决标记并绑定引用","accept"),result=node("span","","result");accept.onclick=async |
| I397 | 动态控件构造 | [cockpit_control_legacy.html:174](../../src/dalton_core/cockpit_control_legacy.html#L174) | node("summary","技术详情"));entries.forEach |
| I398 | 动态控件构造 | [cockpit_control_legacy.html:176](../../src/dalton_core/cockpit_control_legacy.html#L176) | node("textarea");rationale.placeholder="写明你核对了什么，以及接受、修订或拒绝的理由";const actions=node("div",null,"actions"),accept=node("button","接受并写入正式 Ledger","accept"),revise=node("button","要求修订","revise") |
| I399 | 动态控件构造 | [cockpit_control_legacy.html:176](../../src/dalton_core/cockpit_control_legacy.html#L176) | node("button","接受并写入正式 Ledger","accept"),revise=node("button","要求修订","revise"),reject=node("button","拒绝","reject"),result=node("span","","result");async function submit(verdict){if(!rational |
| I400 | 动态控件构造 | [cockpit_control_legacy.html:176](../../src/dalton_core/cockpit_control_legacy.html#L176) | node("button","要求修订","revise"),reject=node("button","拒绝","reject"),result=node("span","","result");async function submit(verdict){if(!rationale.value.trim()){result.textContent="请先写审阅理由";ret |
| I401 | 动态控件构造 | [cockpit_control_legacy.html:176](../../src/dalton_core/cockpit_control_legacy.html#L176) | node("button","拒绝","reject"),result=node("span","","result");async function submit(verdict){if(!rationale.value.trim()){result.textContent="请先写审阅理由";return}let proposed=null;if(verdict==="re |
| I402 | 动态控件构造 | [cockpit_control_legacy.html:179](../../src/dalton_core/cockpit_control_legacy.html#L179) | node("summary",ˋ技术详情（${itemNode.exact_refs.length} 条记录标识）ˋ),list=node("div",null,"ref-list");itemNode.exact_refs.forEach(ref=&gt;list.append(node("div",ˋ${ref.authority?"authority":"projection |
| I403 | 动态控件构造 | [cockpit_control_legacy.html:182](../../src/dalton_core/cockpit_control_legacy.html#L182) | node("button","确认并运行有限刷新","accept"),result=node("span","尚未 dispatch","result");dispatchButton.onclick=async |
| I404 | 动态控件构造 | [cockpit_control_legacy.html:186](../../src/dalton_core/cockpit_control_legacy.html#L186) | node("button",dispatch?.status==="failed"?"重新提交原 writer":"确认并提交原 writer","accept"),result=node("span","","result");confirmButton.onclick=async |
| I405 | 动态控件构造 | [cockpit_control_legacy.html:189](../../src/dalton_core/cockpit_control_legacy.html#L189) | node("button","同意","agree"),disagree=node("button","不同意","disagree"),result=node("span",item.own_verdict?ˋ你已选择：${item.own_verdict}ˋ:"","result");async function submit(verdict){agree.disabled |
| I406 | 动态控件构造 | [cockpit_control_legacy.html:189](../../src/dalton_core/cockpit_control_legacy.html#L189) | node("button","不同意","disagree"),result=node("span",item.own_verdict?ˋ你已选择：${item.own_verdict}ˋ:"","result");async function submit(verdict){agree.disabled=disagree.disabled=true;result.textCo |

## needs_human.py

| 编号 | 类型 | 位置 | 名称 / 动态入口定位片段 |
|---|---|---|---|
| I407 | 服务端动态动作描述 | [needs_human.py:2590](../../src/dalton_core/needs_human.py#L2590) | "label": "去写研究目标"} elif kind in _COMPANY_GAP_KINDS and detail.get("company_ref"): entry = {"mode": "company", "company_ref": str(detail["company_ref"]), "label": "打开这家公司的研究详情", "note": COMPA |
| I408 | 服务端动态动作描述 | [needs_human.py:2593](../../src/dalton_core/needs_human.py#L2593) | "label": "打开这家公司的研究详情", "note": COMPANY_GAP_NOTE} elif item.get("where_open") == SOURCES_DIALOG: entry = {"mode": "dialog", "open": SOURCES_DIALOG, "label": f"打开「{SOURCES_DIALOG_LABEL}」", "n |
| I409 | 服务端动态动作描述 | [needs_human.py:2596](../../src/dalton_core/needs_human.py#L2596) | "label": f"打开「{SOURCES_DIALOG_LABEL}」", "note": SOURCES_DIALOG_NOTE} elif item.get("where_open") == MODELS_DIALOG: entry = {"mode": "dialog", "open": MODELS_DIALOG, "label": f"打开「{MODELS_DIA |
| I410 | 服务端动态动作描述 | [needs_human.py:2598](../../src/dalton_core/needs_human.py#L2598) | "label": f"打开「{MODELS_DIALOG_LABEL}」"} elif kind == "controlled_recovery" and where == ADMIN_RUN_WHERE: # 2026-10-09: the doors are buttons -- on the card (ˋˋactionsˋˋ) and in # 管理员设置 → 运行控制 |
| I411 | 服务端动态动作描述 | [needs_human.py:2603](../../src/dalton_core/needs_human.py#L2603) | "label": "在「管理员设置 → 运行控制」处理", "actions": list(detail.get("owner_doors") or ()), "note": ("按钮通过写入服务以你的身份执行，每次都需要写理由并留下回执；" "命令行做法保留在技术详情里。")} elif kind == "publication_parked" and isinstance( |
| I412 | 服务端动态动作描述 | [needs_human.py:2610](../../src/dalton_core/needs_human.py#L2610) | "label": LANGUAGE_RETRY_LABEL, "confirm": ("重新进行语言质检：丢弃缓存的译稿，从头起草、质检和核验，" "会花一次起草和质检的模型费用。确认？"), "note": "约 5 分钟内开始；完成后这张卡片自动消失。"} elif item.get("where_view") == APPROVE_VIEW: # Only cards w |
| I413 | 服务端动态动作描述 | [needs_human.py:2611](../../src/dalton_core/needs_human.py#L2611) | "confirm": ("重新进行语言质检：丢弃缓存的译稿，从头起草、质检和核验，" "会花一次起草和质检的模型费用。确认？"), "note": "约 5 分钟内开始；完成后这张卡片自动消失。"} elif item.get("where_view") == APPROVE_VIEW: # Only cards with a decision on the approvals |
| I414 | 服务端动态动作描述 | [needs_human.py:2616](../../src/dalton_core/needs_human.py#L2616) | "label": "到「需要你确认的研究决定」处理"} elif item.get("where_view"): entry = {"mode": "view", "view": str(item["where_view"]), "label": f"打开「{where}」页"} elif kind == "earnings_release_unread" and str(de |
| I415 | 服务端动态动作描述 | [needs_human.py:2618](../../src/dalton_core/needs_human.py#L2618) | "label": f"打开「{where}」页"} elif kind == "earnings_release_unread" and str(detail.get("exhibit_url") or "").startswith("https://"): entry = {"mode": "link", "href": str(detail["exhibit_url"]), |
| I416 | 服务端动态动作描述 | [needs_human.py:2620](../../src/dalton_core/needs_human.py#L2620) | "label": "打开 SEC 上的 8-K 原文", "note": "无需你操作；申报数入库后这张卡会自动消失。"} elif kind == "connector_login_expired": entry = {"mode": "none", "label": where, "note": "工作台上没有重新登录的按钮；请在本机的这个应用里重新登录，恢复后系统自动继续 |
| I417 | 服务端动态动作描述 | [needs_human.py:2623](../../src/dalton_core/needs_human.py#L2623) | "label": where, "note": "工作台上没有重新登录的按钮；请在本机的这个应用里重新登录，恢复后系统自动继续。"} elif where == TECHNICAL_ADMIN_WHERE: entry = {"mode": "none", "label": where, "note": TECHNICAL_ADMIN_NOTE} elif where == N |
| I418 | 服务端动态动作描述 | [needs_human.py:2626](../../src/dalton_core/needs_human.py#L2626) | "label": where, "note": TECHNICAL_ADMIN_NOTE} elif where == NO_ACTION_WHERE or not actionable: entry = {"mode": "none", "label": where or NO_ACTION_WHERE, "note": "无需你操作。"} else: entry = {"m |
| I419 | 服务端动态动作描述 | [needs_human.py:2628](../../src/dalton_core/needs_human.py#L2628) | "label": where or NO_ACTION_WHERE, "note": "无需你操作。"} else: entry = {"mode": "none", "label": where, "note": NO_PAGE_ACTION_NOTE} # 2026-10-09: a card with nothing to press can be acknowledge |
| I420 | 服务端动态动作描述 | [needs_human.py:2630](../../src/dalton_core/needs_human.py#L2630) | "label": where, "note": NO_PAGE_ACTION_NOTE} # 2026-10-09: a card with nothing to press can be acknowledged (已知悉 / # 暂不提醒 N 天 / 放弃这份资料); it comes back by itself when what it is # about chang |
| I421 | 服务端动态动作描述 | [needs_human.py:2642](../../src/dalton_core/needs_human.py#L2642) | "label": "已知悉 / 暂不提醒", **acknowledge, **({"secondary": {"mode": entry["mode"], **secondary}} if entry.get("mode") not in {None, "none"} else {}), **({"secondary_note": entry.get("note")} if |
| I422 | 服务端动态动作描述 | [needs_human.py:2646](../../src/dalton_core/needs_human.py#L2646) | "secondary_note": entry.get("note")} if entry.get("note") else {})} return entry def _counts(items: Iterable[Mapping[str, Any]]) -&gt; dict[str, int]: out: dict[str, int] = {} for item in items |

## 完整性边界

共记录 **422 个源码交互构造/描述点**。扫描涵盖新版各页面、图表、管理员、简报、旧版及退役页；命名组件的用户级建议已逐组写进主报告。

此为结构扫描加人工审阅的定位台账，不是完整JavaScript语法树，也不是端到端测试。服务端运行时可能返回新的labels/actions；固定编号仅用于本次基线。模板上传、收费请求、审批、停止、预算、外发与项目创建等成功/失败链需要在隔离项目验证。
