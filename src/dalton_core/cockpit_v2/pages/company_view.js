/* 投资观点 -- one current view, its history kept apart (review O02 P0, C02,
   C04, O06, O07).

   The conclusion is the current version of each thesis about this company
   (overview.theses[].current, with its date and what the last revision did),
   the base scenario and the latest research judgement.  Earlier thesis
   versions -- which read as opposing views when shown side by side -- go
   under 「历史观点与分歧」 with their dates and a 「非当前」 mark; the current
   industry thesis is its own section 「相关行业论点（当前）」, never in that fold.  A report's
   opening paragraph is never presented as the current view: it is a dated
   report summary, collapsed. */

import {lead, load, thesisVersions} from "./common.js";
import {cnDate, honestState} from "./company_common.js";
import {openEvidence} from "./company_evidence.js";
import {LABELS} from "../copy.js";

const CONFIDENCE = {low: "低（资料尚存缺口）", medium: "中", high: "高"};

/** The theses about one company (and the industry), current vs earlier. */
export function thesesFor(overview, companyRef) {
  const {current, history} = thesisVersions(overview);
  const mine = t => t.company_ref === companyRef, industry = t => !t.company_ref;
  const active=t => t.view_status !== "broken" && t.stance !== "counter";
  const counters=(overview?.counter_views || current.filter(t => t.stance === "counter")).filter(t => (mine(t) || industry(t)) && t.view_status !== "broken");
  return {current: current.filter(t => mine(t) && active(t)), history: history.filter(mine), counters,
          industryCurrent: current.filter(t => industry(t) && active(t)), industryHistory: history.filter(industry)};
}

/** Key figures once each: metric + period + value kind (C04). */
export function distinctFigures(figures) {
  const seen = new Set();
  return (figures || []).filter(f => {
    const key = `${f.display_label || f.label}|${f.period_label || f.period || ""}|${f.value}|${f.unit || ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function thesisCard(ctx, t, {history = false} = {}) {
  const {h, ui, say} = ctx;
  const meta = [
    t.confidence ? `证据充分度：${CONFIDENCE[t.confidence] || say(t.confidence)}` : null,
    t.at ? `${history ? "写于" : "生效基准日："}${cnDate(t.at)}` : null,
    t.change_label ? `最近变化：${t.change_label}` : null,
    history && t.superseded_at ? `${cnDate(t.superseded_at)}被修订取代` : null,
  ].filter(Boolean).join(" · ");
  return h("li", {class: `v2-card${history ? " v2-history-item" : ""}`},
    history ? h("div", {class: "v2-card-meta"}, ui.tag("历史归档", "warn")) : null,
    h("p", null, say(t.summary)),
    meta ? h("p", {class: "v2-note"}, meta) : null,
    t.disagreement ? h("details", {class:"v2-more"}, h("summary",null,"分歧依据与裁决安排"),
      h("p",null,say(t.disagreement.reason)), h("p",null,`截止日：${cnDate(t.disagreement.deadline)} · 裁决人：${t.disagreement.arbiter_display_label || "已指定负责人（姓名未提供）"}`),
      h("ul",null,...(t.disagreement.evidence_refs || []).map(ref => h("li",null,evidenceButton(ctx,ref)))),
      h("details",{class:"v2-more"},h("summary",null,"负责人登记信息"),h("p",null,`登记记录：${t.disagreement.arbiter_ref}`))) : null);
}

export async function viewSection(ctx, section, data, researchLoad, {fill, product, readyText, publicationTag, openReader, feedbackDisclosure, reflect}) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const r = await researchLoad;
  data.research = r.value;
  const el = h("div");
  const chart = h("div", {class: "v2-slot"});
  fill(section, el, chart);
  const th = thesesFor(data.overview, c.company_ref);
  const base = (c.scenarios?.in_force || []).find(s => s.key === "base");
  const j = (c.judgements?.latest || [])[0];
  const hv=c.house_view || {};
  // Five current decision blocks; history and research material follow in disclosures.
  el.append(h("section",{id:"house-statement"},h("h3",null,"当前生效论点 · 本所观点"),
    hv.statement ? h("p",{class:"v2-lead"},say(hv.statement)) : honestState(ctx,{state:th.current.some(t=>!t.question_ref || !t.stance || !(t.falsification_conditions || []).some(x=>x.deadline)) ? "下方保留已登记的历史论点；这些记录尚未完整绑定研究问题、观点立场及带期限的证伪条件。" : say(hv.missing_reason || LABELS.shell.no_view),
      impact:"正式本所观点尚未形成。",owner:"研究系统",action:"查看已登记观点与资料缺口。"}),
    hv.statement ? h("p",{class:"v2-note"},`${cnDate(hv.as_of)} · 第${hv.version}版${hv.change_label ? " · "+say(hv.change_label) : ""}`) : null,
    th.current.length ? h("details",{class:"v2-more"},h("summary",null,"已登记公司论点"),h("ul",{class:"v2-list"},...th.current.map(t=>thesisCard(ctx,t)))) : null,
    th.counters.length ? h("div",{class:"v2-counter-view"},h("h4",null,"反向观点（独立于本所观点）"),h("ul",{class:"v2-list"},...th.counters.map(t=>thesisCard(ctx,t)))) : null));
  el.append(h("section",{id:"house-reasons"},h("h3",null,"为什么这样看"),
    (hv.reasons || []).length ? h("ol",null,...hv.reasons.map(x=>h("li",null,say(x.text),
      evidenceButton(ctx,x.claim_ref || x.passage_ref,x.text)))) : ui.note("当前观点尚未绑定可读取的依据。"),
    hv.reasons_status === "incomplete" ? ui.note("三条核心依据尚未收齐；已有依据照实列出。") : null));
  const rr=hv.risk_reward;
  el.append(h("section",{id:"house-risk-reward"},h("h3",null,"收益与风险"),rr ? ui.kv([
    ["观察窗口",`${cnDate(rr.window?.start)} 至 ${cnDate(rr.window?.end)}（12个月）`],
    ["概率加权预期回报",fmt.change(rr.expected_return_percent)],
    ["悲观／基准／乐观目标价",[rr.bear_target_price,rr.base_target_price,rr.bull_target_price].map(x=>fmt.number(x,2)).join(" ／ ")],
    ["潜在收益／损失比",rr.upside_downside_ratio == null ? "未形成同时包含上涨和下跌的完整区间" : `${fmt.number(rr.upside_downside_ratio,2)} 倍`],
    ["悲观情景权重",fmt.probability(rr.bear_probability)],
  ]) : ui.note(say(hv.risk_reward_missing_reason || "模型、情景或估值依据尚不完整"))));
  const next=hv.next_catalyst;
  el.append(h("section",{id:"house-next-catalyst"},h("h3",null,"下一个催化剂"),next ? h("div",null,
    h("p",null,`${cnDate(next.date) || "日期未确认"} · ${say(next.label || "研究事件")}`),
    (next.what_to_watch || []).length ? h("ul",null,...next.what_to_watch.map(x=>h("li",null,say(x.text || x.label),
      x.why ? h("p",{class:"v2-note"},say(x.why)) : null))) : ui.note(say((next.observations_reason === "尚无来源绑定的前瞻观察条件" ? "尚未明确需要观察的经营指标" : next.observations_reason) || "尚未明确需要观察的经营指标"))) : ui.note("尚无已确认的下一项研究催化剂")));
  el.append(h("section",{id:"house-change-view"},h("h3",null,"什么会改变观点"),
    (hv.what_changes_view || []).length ? h("ul",null,...hv.what_changes_view.map(x=>h("li",null,
      x.display_status === "available" ? `${say(x.metric_display_label)}${say(x.comparison_label)}${x.threshold} ${say(x.unit_display_label)} · 截止 ${cnDate(x.deadline)}` : "一项条件的指标中文口径尚未解析，需查看详情核对。",
      h("details",{class:"v2-more"},h("summary",null,"条件详情与裁决依据"),
        x.display_status === "available" ? null : h("p",null,`${x.metric_ref} ${x.operator} ${x.threshold} ${x.unit} · ${x.deadline}`),
        ...(x.evidence_refs || []).map(ref=>evidenceButton(ctx,ref)))))) : ui.note("尚未登记可观察且带期限的观点失效条件")));
  // The report's opening is a dated summary, not the view (C02).
  const memo = product(r.value, "investment_memo"), screen = product(r.value, "initial_screen");
  const source = readyText(memo) ? memo : readyText(screen) ? screen : null;
  if (source) {
    const first = source.sections[0];
    const name = source.display_label || LABELS.report_kind[source.kind] || say(source.label);
    el.append(h("details", {class: "v2-more"},
      h("summary", null, source.kind === "investment_memo"
        ? `投资决策备忘录摘要（${cnDate(source.created_at)}）`
        : `历史底稿参考：立项初筛底稿（${cnDate(source.created_at)} 初稿摘要；当前结论见上方当前生效论点）`),
      h("p", null, lead(first.display_body || say(first.body), 360)),
      h("p", {class: "v2-note"}, `${say(source.version_label || "")} `, publicationTag(ctx, source)),
      h("div", {class: "v2-actions"}, ui.button(`阅读${name}全文`, () => openReader(ctx, source), {variant: "secondary"}))));
  } else if (r.error) el.append(ui.alert(`研究报告暂时读不到：${r.error.display_message}`, "warn"));
  const screenFeedback = c.document?.feedback;
  if (screenFeedback) el.append(feedbackDisclosure(ctx, screenFeedback, {title: "这份初步研究报告", id: "fb-screen", summary: "对初步研究报告留反馈"}));
  // The industry's current thesis is a current view too: its own section,
  // never inside the history fold (2026-10-09 P0).
  if (th.industryCurrent.length) el.append(h("h3", {id: "view-industry"}, "本研究行业的共同前提（生效中）"),
    h("ul", {class: "v2-list"}, ...th.industryCurrent.map(t => thesisCard(ctx, t))));
  // History and disagreement: superseded versions only, dated and marked (O02).
  const older = [...th.history, ...th.industryHistory];
  if (older.length) {
    el.append(h("details", {class: "v2-more", id: "view-history"},
      h("summary", null, `历史观点演变与分歧记录（${older.length} 条，已归档）`),
      h("h4", null, "已被修订取代的历史研判"),
      h("ul", {class: "v2-list"}, ...older.map(t => thesisCard(ctx, t, {history: true}))),
      ui.note("历史研判按写成日期列出，用于复盘研判逻辑演进；当前决策请以上方当前生效论点与行业前提为准。")));
  }
  const archive=h("details",{class:"v2-more",id:"view-supporting-material"},h("summary",null,"研究进度、关键数字与一致性详情"));
  el.append(archive);
  const readiness = c.source_readiness;
  archive.append(ui.kv([
    ["投研阶段", `${say(c.stage || "未开始")}${c.stage_status ? ` · ${say(c.stage_status)}；盈利预测进度见下方「预测底稿」` : ""}`],
    ["阶段性质量复核", c.gate_decision ? say(c.gate_decision.label) : "尚无复核"],
    ["本阶段资料", readiness ? `${say(readiness.label)}${readiness.ready ? "" : (c.note ? `：${say(c.note)}` : "")}` : "—"],
    ["预测底稿", c.model?.note ? say(c.model.note) : say(c.model_empty_label || "三大表盈利预测编制中")],
  ]));
  // Batch an 研究一致性: conflicts between this company's products and the
  // derived results computed from an older version (已过期, never current).
  const cons = c.consistency;
  if (cons && ((cons.conflicts || []).length || (cons.stale || []).length)) {
    archive.append(h("h3", {id: "view-consistency"}, say(cons.title || "研究一致性")),
      h("p", {class: "v2-note"}, say(cons.summary)));
    if ((cons.conflicts || []).length) archive.append(h("ul", {class: "v2-list"}, ...cons.conflicts.map(x => h("li", null,
      ui.tag(say(x.severity_label), x.severity === "low" ? "" : "warn"), " ", h("strong", null, say(x.label)), "：", say(x.text)))));
    if ((cons.stale || []).length) archive.append(h("details", {class: "v2-more"},
      h("summary", null, `已过期的派生结果（${cons.stale.length} 份，等待按新版本重算）`),
      h("ul", null, ...cons.stale.map(x => h("li", null, ui.tag(say(x.status_label), "warn"), " ", say(x.text))))));
  }
  const figures = distinctFigures(c.figures?.latest).slice(0, 3);
  if (figures.length) {
    archive.append(h("h3", null, "关键数字"), h("ul", null, ...figures.map(f => h("li", null,
      reflect.untranslated(f.display_label) || /原始名称/.test(f.display_label || "") ? h("span", {lang: "en"}, f.label) : say(f.display_label || f.label),
      `（${say(f.period_label || f.period || "期间未注明")}）：`,
      h("strong", {class: "v2-num"}, f.unit === "basis_points" ? `${Number(f.value) > 0 ? "+" : ""}${fmt.number(f.value)} bps（同比变化 ${fmt.number(Number(f.value) / 100, 2)} 个百分点）` : fmt.typed(f.value, {unit: f.unit, currency: f.currency, scale: f.scale, metric: /(^|[:-])eps($|-)/i.test(f.metric_ref || "") ? "eps" : (f.metric_ref || "")})),
      " ", ui.tag(say(f.grade_label || ""))))));
  }
  // The header already shows price, changes and multiples: the widget draws the chart only.
  if (!(await ctx.slot("company.summary", chart, {company: c, company_ref: c.company_ref, research: r.value, header_shown: true}, null))) chart.remove();
}


export function evidenceButton(ctx,ref,text=null) {
  const {h,ui}=ctx;
  return ui.button("查看原文与出处",()=>{
    if (!String(ref).startsWith("doc-passage:")) return openEvidence(ctx,{ref,display:text});
    ctx.side.open({heading:"原文段落与出处",render:el=>{
      el.append(ui.note("正在读取原文…"));
      load(ctx,`/v2/data/evidence/passage?ref=${encodeURIComponent(ref)}`).then(result=>{
        const p=result.value?.passage;
        if (!p) {el.replaceChildren(ui.note(result.error?.display_message || "这段原文暂时读不到；不能据此补全依据。"));return;}
        el.replaceChildren(h("h3",null,p.title || p.heading || "原文段落"),h("p",{class:"v2-note"},cnDate(p.date)),
          h("p",{class:"v2-passage",lang:/[\u4e00-\u9fff]/.test(p.text || "") ? "zh" : "en"},p.text || p.snippet || ""),
          p.url ? h("a",{href:p.url,target:"_blank",rel:"noopener"},"打开来源文件") : ui.note("来源全文链接尚未登记"));
      });
    }});
  },{variant:"quiet"});
}
