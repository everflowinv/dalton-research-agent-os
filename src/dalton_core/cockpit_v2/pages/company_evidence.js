/* 证据 -- the conclusions and, one press away, their real source (review
   C10/C13/A03 P0, C11, C12, C14, financial 4.6).

   「查看原文与出处」 opens GET /v2/data/evidence/source?claim=<ref>: the
   document's title, house and date (or the SEC filing), where the passage sits
   (statement line, or character range and line), the passage itself with the
   cited span marked, and the original (SEC EDGAR, the public page) -- two
   steps at most from a conclusion: step 1 the drawer, step 2 「阅读存档全文」
   or the original link.  Internal refs live only under 「技术标识」, collapsed.

   The list shows current conclusions only; 「显示全部历史版本」 adds the earlier
   versions in their own list, marked 历史版本, never mixed into the current one.
   A profit-bridge bar arrives as ?line_id=<statement line> and opens that
   exact filed line -- no text search (C13). */

import {lead, load, showText, untranslated} from "./common.js";
import {feedbackDisclosure} from "./feedback.js";
import {cnDate, honestFromPayload, honestState, readFailure} from "./company_common.js";
import {LABELS} from "../copy.js";

const SRC = LABELS.source_kind;
export const NO_ORIGINAL = "未关联原始文件全文（仅保留系统摘录记录，请谨慎作为核心投资依据）";

export const SOURCE_PATH = "/v2/data/evidence/source";
export const DOCUMENT_PATH = "/v2/data/evidence/document";
export const LINE_PATH = "/v2/data/evidence/statement-line";

const isLatin = t => /[A-Za-z]{3,}/.test(String(t || "")) && !/[㐀-鿿]/.test(String(t || ""));

/** The passage with the cited span marked (<mark>), offsets from the server. */
export function passageNode(ctx, passage, {id = null} = {}) {
  const {h} = ctx;
  const text = String(passage.text || "");
  const [a, b] = Array.isArray(passage.highlight) ? passage.highlight : [passage.highlight?.start, passage.highlight?.end];
  const start = Number.isInteger(a) ? Math.max(0, Math.min(a, text.length)) : 0;
  const end = Number.isInteger(b) ? Math.max(start, Math.min(b, text.length)) : start;
  const lang = passage.lang === "en" || (passage.lang == null && isLatin(text)) ? "en" : null;
  return h("blockquote", {class: "v2-passage", lang},
    passage.truncated_before ? "…" : "",
    text.slice(0, start),
    end > start ? h("mark", {id}, text.slice(start, end)) : null,
    text.slice(end),
    passage.truncated_after ? "…" : "",
    passage.speaker ? h("footer", {class: "v2-note"}, `引述发言人：${passage.speaker}`) : null);
}

/** The original's button names where it goes (am 2.3). */
function originalLabel(link) {
  const text = String(link.label || "");
  if (/EDGAR|SEC/.test(text) || /sec\.gov/.test(String(link.url || ""))) return "在 SEC EDGAR 查看申报原件";
  return text && !/^打开原文$/.test(text) ? text : "在来源平台查看原文";
}
/** The reader's button names what it opens: a filing or a transcript (am 2.3). */
function readerLabel(source) {
  return /SEC|10-|8-K|申报/.test(String(source.document_type_label || "")) || /^sec/.test(String(source.source_kind || ""))
    ? "查看申报文件原件" : "查看纪要 / 资料全文";
}

function linkNodes(ctx, source, onRead) {
  const {h, ui} = ctx;
  const out = [];
  for (const link of source.links || []) {
    if (link.kind === "original" && link.url) {
      out.push(h("a", {class: "v2-btn v2-btn-secondary", href: link.url, target: "_blank", rel: "noopener noreferrer"},
        `${originalLabel(link)} ↗`));
    } else if (link.kind === "reader" && link.href) {
      out.push(ui.button(readerLabel(source), () => onRead(link.href, source), {variant: "quiet"}));
    }
  }
  return out.length ? h("div", {class: "v2-actions"}, ...out) : null;
}

function technical(ctx, value) {
  const {h} = ctx;
  const pairs = Object.entries(value || {}).filter(([, v]) => v != null && v !== "");
  if (!pairs.length) return null;
  return h("details", {class: "v2-more v2-tech"}, h("summary", null, "数据版本与溯源指纹"),
    h("dl", {class: "v2-kv"}, ...pairs.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", {lang: "en"}, Array.isArray(v) ? v.join("、") : String(v))))));
}

/** One cited source block (drawer and the bridge card). */
export function sourceBlock(ctx, source, {onRead, index = 0} = {}) {
  const {h, ui} = ctx;
  const head = [source.relation_label, source.document_type_label].filter(Boolean).join(" · ");
  const byline = [source.house, cnDate(source.published_at) || null].filter(Boolean).join(" · ");
  const filing = source.filing;
  return h("section", {class: "v2-source", "aria-labelledby": `ev-src-${index}`},
    h("h3", {id: `ev-src-${index}`}, head || "出处"),
    source.title ? h("p", {class: "v2-source-title", lang: isLatin(source.title) ? "en" : null}, source.title) : null,
    byline ? h("p", {class: "v2-note"}, byline) : null,
    filing ? ui.kv([["申报文件", [filing.form, filing.entity_name].filter(Boolean).join(" · ")],
                    ["报告期", filing.period_label || cnDate(filing.report_date) || "—"],
                    ["申报日", cnDate(filing.filed) || "—"]]) : null,
    source.location?.label ? h("p", null, h("strong", null, "位置："), source.location.label) : null,
    source.passage ? passageNode(ctx, source.passage)
      : ui.note(`${NO_ORIGINAL}${source.passage_unavailable_reason ? `：${source.passage_unavailable_reason}` : ""}`),
    linkNodes(ctx, source, onRead || (() => {})),
    technical(ctx, source.technical));
}

/** The stored full text (or the filed statement) of one source. */
function documentView(ctx, el, path, back) {
  const {h, ui} = ctx;
  el.replaceChildren(h("p", {class: "v2-loading"}, "正在打开申报文件原件 / 纪要全文…"));
  load(ctx, path).then(res => {
    if (res.error) { el.replaceChildren(ui.button("返回出处", back, {variant: "quiet"}), readFailure(ctx, res.error, "存档全文")); return; }
    const d = res.value || {};
    if (d.status !== "ok") { el.replaceChildren(ui.button("返回出处", back, {variant: "quiet"}), honestFromPayload(ctx, d, {state: "存档全文暂时打不开。"})); return; }
    const header = [h("div", {class: "v2-actions"}, ui.button("返回出处", back, {variant: "quiet"}),
                          ...((d.links || []).filter(l => l.kind === "original" && l.url)
                            .map(l => h("a", {class: "v2-btn v2-btn-secondary", href: l.url, target: "_blank", rel: "noopener noreferrer"}, `${l.label} ↗`)))),
                    h("h3", null, [d.document_type_label, d.title].filter(Boolean).join(" · ") || "存档全文"),
                    h("p", {class: "v2-note"}, [d.house, cnDate(d.published_at), d.location_label].filter(Boolean).join(" · "))];
    if (Array.isArray(d.rows)) {
      el.replaceChildren(...header, ui.table({
        caption: `${d.statement_label || "报表"}（申报原文，所引的一行已标出）`,
        columns: [{key: "ordinal", label: "行", num: true, render: r => String(r.ordinal)},
                  {key: "label", label: SRC.as_reported, render: r => h(r.cited ? "mark" : "span", {lang: isLatin(r.label) ? "en" : null}, r.label)},
                  {key: "period", label: "期间", render: r => r.period_label || "—"},
                  {key: "value", label: SRC.as_reported_amount, num: true, render: r => r.value_display || "—"}],
        rows: d.rows}));
      return;
    }
    const text = String(d.text || "");
    const hl = d.highlight || {};
    const node = passageNode(ctx, {text, highlight: [hl.start, hl.end], lang: d.lang}, {id: "ev-doc-mark"});
    node.setAttribute("class", "v2-passage v2-doc");
    el.replaceChildren(...header, d.truncated ? ui.note("全文较长，这里显示所引段落附近的 30 万字。") : null, node);
    const mark = el.querySelector("mark");
    if (mark) setTimeout(() => { mark.scrollIntoView?.({block: "center"}); }, 0);
  });
}

/** The drawer for one conclusion.  `item` is the list row (display text already localized). */
export function openEvidence(ctx, item) {
  const {h, ui, fmt} = ctx;
  ctx.side.open({heading: "事实底稿与引用出处", render: el => {
    el.append(h("p", {class: "v2-loading"}, "正在读取出处…"));
    const draw = () => load(ctx, `${SOURCE_PATH}?claim=${encodeURIComponent(item.ref)}`).then(res => {
      if (res.error) { el.replaceChildren(readFailure(ctx, res.error, "这条结论的出处")); return; }
      const v = res.value || {};
      if (v.status !== "ok") { el.replaceChildren(honestFromPayload(ctx, v, {state: "这条结论的出处暂时读不到。"})); return; }
      const claim = v.claim || {};
      const shown = item.display || claim.statement;
      const original = claim.statement_original && claim.statement_original !== shown ? claim.statement_original : null;
      const onRead = href => documentView(ctx, el, href, draw);
      el.replaceChildren(
        h("h3", null, v.available_original ? "结论" : "结论记录"),
        h("p", {class: "v2-lead", lang: isLatin(shown) ? "en" : null}, shown),
        original ? h("p", {class: "v2-note"}, h("span", {class: "v2-tag"}, claim.original_lang === "zh" ? "原文（中文）" : "原文（英文）"), " ",
                     h("span", {lang: claim.original_lang === "zh" ? null : "en"}, original)) : null,
        ui.kv([["公司", claim.company || "—"], ["期间", claim.period_label || "—"],
               ["类型", claim.kind_label || "—"], ["入库日期", cnDate(claim.recorded_at) || fmt.date(claim.recorded_at)]]),
        v.available_original ? null : ui.alert(NO_ORIGINAL, "warn"),
        ...(v.sources || []).map((s, i) => sourceBlock(ctx, s, {onRead, index: i})),
        (v.sources || []).length ? null : honestState(ctx, {state: v.sources_empty_reason || "这条结论没有登记出处。",
          impact: "无法核对原文。", owner: "研究系统在补登出处后显示。", action: "把这条结论当作未核实的信息使用。"}),
        technical(ctx, v.technical));
      el.querySelector("h3")?.setAttribute("tabindex", "-1");
    });
    draw();
  }});
}

/** The exact filed line a profit-bridge bar points at (C13). */
async function bridgeLineCard(ctx, filters, clear) {
  const {h, ui} = ctx;
  const q = new URLSearchParams({line: filters.line_id});
  const res = await load(ctx, `${LINE_PATH}?${q}`);
  const title = `已锁定利润桥科目：${filters.line || "所选报表行"}`;
  if (res.error) return h("div", {class: "v2-panel"}, h("h3", null, title), readFailure(ctx, res.error, "这一报表行"), clear);
  const v = res.value || {};
  if (v.status !== "ok" || !(v.lines || []).length) {
    return h("div", {class: "v2-panel"}, h("h3", null, title), honestFromPayload(ctx, v, {state: "申报文件里没有找到这一行。"}), clear);
  }
  const blocks = [];
  v.lines.forEach((line, i) => {
    blocks.push(sourceBlock(ctx, line, {index: 100 + i, onRead: href => {
      ctx.side.open({heading: "申报原文", render: el => documentView(ctx, el, href, () => ctx.side.close())});
    }}));
    const claims = line.claims || [];
    if (claims.length) blocks.push(h("div", {class: "v2-actions"}, ...claims.slice(0, 3).map((ref, k) =>
      ui.button(claims.length > 1 ? `查看引用这一行的结论 ${k + 1}` : "查看引用这一行的结论",
        () => openEvidence(ctx, {ref, display: null}), {variant: "quiet"}))));
  });
  return h("div", {class: "v2-panel", id: "ev-bridge-line"}, h("h3", null, title), ...blocks, clear);
}

export async function evidenceSection(ctx, section, data, fill) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const f = ctx.state.filters || {};
  const params = new URLSearchParams({company: c.company_ref, limit: "20"});
  if (f.aspect) params.set("aspect", f.aspect);
  if (f.importance) params.set("importance", f.importance);
  const allVersions = f.versions === "all";
  const res = await load(ctx, `/v1/cockpit/claims?${params}`);
  if (res.error) { fill(section, readFailure(ctx, res.error, "研究结论")); return; }
  // The current list never shows a superseded version (C14), even if a read
  // returned one; those belong to 「历史版本」 below.
  const v = {...res.value, items: (res.value.items || []).filter(x => x.canonical !== false)};
  const bindingsFor = async items => {
    const refs = items.map(x => x.ref).filter(Boolean);
    if (!refs.length) return {};
    const b = await load(ctx, `/v2/data/feedback?${new URLSearchParams({company: c.company_ref, refs: refs.join(",")})}`);
    return b.value?.bindings || {};
  };
  let bindings = await bindingsFor(v.items);
  const clear = h("p", null, h("a", {href: ctx.href({filters: {...f, line: "", line_id: "", accession: "", importance: ""}})}, "取消科目穿透联动"));
  const lineCard = f.line_id ? await bridgeLineCard(ctx, f, clear)
    : f.line ? h("div", {class: "v2-panel"}, ui.note(`已锁定利润桥科目：${f.line}。该科目尚未关联具体申报行，请在下方证据中按主题查找原文。`), clear) : null;
  const vocab = v.vocabulary || {};
  const select = (id, label, options, current, key) => {
    const field = ui.field({id, label, control: "select"});
    field.input.append(h("option", {value: ""}, "全部"), ...(options || []).map(o => h("option", {value: o.value, selected: o.value === current || null}, (key === "importance" && SRC[o.value]) || say(o.label))));
    field.input.addEventListener("change", () => ctx.setFilter(key, field.input.value));
    return field.wrap;
  };
  const itemNode = (x, {history = false} = {}) => {
    const display = x.display_statement && !untranslated(x.display_statement) ? x.display_statement : null;
    const shownText = display || say(x.statement);
    return h("li", {class: "v2-card", "data-claim": x.ref},
      h("div", {class: "v2-card-meta"}, history ? ui.tag("已废止 / 已合并的历史证据", "warn") : null, ui.tag(SRC[x.importance] || say(x.importance_label || "")),
        h("span", null, say(x.aspect_label || "")), h("span", null, say(x.period_label || x.period || "期间未注明"))),
      h("p", null, showText(ctx, x.statement, display)),
      ui.button("核验申报出处与原文段落", () => openEvidence(ctx, {ref: x.ref, display: shownText}),
        {variant: "quiet", "aria-label": `核验「${lead(shownText, 40)}」的申报出处与原文段落`}),
      !history && bindings[x.ref] ? feedbackDisclosure(ctx, bindings[x.ref], {title: "此研究条目", id: `fb-${String(x.ref).slice(-16)}`, summary: "对此条目留反馈"}) : null);
  };
  const list = h("ul", {class: "v2-list", "aria-label": "研究证据与结论"});
  list.append(...v.items.map(x => itemNode(x)));
  const more = h("div", {class: "v2-actions"});
  let cursor = v.next_cursor;
  const moreButton = ui.button("加载更多", async () => {
    moreButton.disabled = true;
    const p = new URLSearchParams(params); p.set("cursor", cursor);
    const next = await load(ctx, `/v1/cockpit/claims?${p}`);
    moreButton.disabled = false;
    if (next.error) { more.append(readFailure(ctx, next.error, "更多结论")); return; }
    bindings = {...bindings, ...(await bindingsFor(next.value.items || []))};
    list.append(...(next.value.items || []).map(x => itemNode(x)));
    cursor = next.value.next_cursor;
    if (!cursor) moreButton.remove();
    ctx.announce(`已加载 ${next.value.returned_count || 0} 条`);
  }, {variant: "secondary"});
  if (cursor) more.append(moreButton);
  const versionsToggle = h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle", "aria-pressed": allVersions ? "true" : "false",
    on: {click: () => ctx.setFilter("versions", allVersions ? "" : "all")}}, allVersions ? "包含已废止 / 已合并历史证据：已开启" : "包含已废止 / 已合并历史证据：已关闭");
  // Earlier versions: their own list, after the current one, never mixed in.
  let historyBlock = null;
  if (allVersions) {
    const hp = new URLSearchParams(params); hp.set("canonical", "0");
    const hres = await load(ctx, `/v1/cockpit/claims?${hp}`);
    if (hres.error) historyBlock = readFailure(ctx, hres.error, "历史版本");
    else {
      const current = new Set(v.items.map(x => x.ref));
      const older = (hres.value.items || []).filter(x => x.canonical === false && !current.has(x.ref));
      historyBlock = h("section", {class: "v2-history", "aria-labelledby": "ev-history-title"},
        h("h3", {id: "ev-history-title"}, "已废止 / 已合并的历史证据（不是当前依据）"),
        ui.note("同一结论被合并或改写前的旧版本，供核对修改过程；当前结论只看上面的列表。"),
        older.length ? h("ul", {class: "v2-list", "aria-label": "历史版本"}, ...older.map(x => itemNode(x, {history: true})))
          : ui.note("这一页没有更早的版本。"));
    }
  }
  fill(section, lineCard,
    h("div", {class: "v2-grid"}, select("ev-aspect", "研究议题分类", vocab.aspects, f.aspect, "aspect"), select("ev-importance", "信息源渠道", vocab.importance, f.importance, "importance")),
    h("div", {class: "v2-actions"}, versionsToggle),
    ui.note(`已入库核心事实依据共 ${fmt.number(v.total)} 条（已剔除重复口径）${v.all_versions_total ? `；含历史证据 ${fmt.number(v.all_versions_total)} 条` : ""}；默认按证据重要度及时间排序，每次显示 20 条。`),
    v.items.length ? list : honestState(ctx, {
      state: f.aspect || f.importance ? "没有符合筛选条件的研究结论。" : "这家公司还没有入库的研究结论。",
      impact: "证据列表为空；投资观点和模型不受影响，但暂时无法从这里核对原文。",
      owner: "研究系统读取公司申报、电话会和研报后自动入库。",
      action: f.aspect || f.importance ? "把研究议题分类和信息源渠道改回「全部」。" : "稍后再看，或在「智能投研问答」里提问。"}),
    more, historyBlock);
}
