/* 研究问答: one question, one answer, with conclusion first (A01-A04).

   2026-10-09 (MUSE P0 #1, independent A01/A05/A06, financial 痛点2 / 4.12):
   - while an answer is prepared the page shows the elapsed time and a
     「后台继续分析（离开当前等待）」 that stops waiting here (the job keeps running on the server,
     which has no cancel; its result lands in the history);
   - 「追问」 is a real follow-up: the request names the answer it follows
     (`follow_up_of`) and the server carries that answer and its citations as
     bounded context; the page shows a 「基于上一回答」 chip, never pasted text;
   - no fee on this page at all (研究设置 → 费用 has the totals; am 6.3: a
     per-answer cost is never shown);
   - a question that failed after the analysis ran is asked again with the
     same request id, so the analysis already done is reused, not redone.

   Batch G (2026-10-09, research Q&A redesign): an answer reads in four parts
   -- 发生了什么 / 市场原本预期什么 / 我们的看法 / 不确定点 -- and every material
   request the question opened shows as a 「正在获取」 chip whose status is
   polled from /v2/data/research/material-requests; once one has material,
   「用新材料续答」 resumes the answer (once per request). */

import {V, OVERVIEW, companies, companyName, lead, load} from "./common.js";
import {feedbackBar, feedbackDisclosure} from "./feedback.js";
import {UI, LABELS} from "../copy.js";

const CONFIDENCE = {high: "信息完整度：扎实充分", medium: "信息完整度：部分完整", low: "信息完整度：依据不足"};
const PENDING = "ask:pending";
const REQUESTS = "/v2/data/research/material-requests";
const PART_TITLES = ["发生了什么", "市场原本预期什么", "我们的看法", "不确定点"];
let watchRequests = null;   // set by the page's render; other pages render answers without polling
const REQUEST_WORD = {open: "正在获取", partial: "已取到部分材料", found: "已取到", exhausted: "来源已用尽", timeout: "已超时", superseded: "已被取代"};
const FOLLOW = "ask:follow";

/* A09: questions an analyst often starts from; they fill the box, never send. */
const STARTERS = [
  ["季度业绩拆解", c => `${c}最新季度营收、营业利润率和 EPS 表现如何？与管理层指引及市场一致预期偏离的主要原因是什么？`],
  ["业务增长动能", c => `${c}未来 12 个月主要细分业务增长动力来自哪里？在手订单与资本开支假设是否有扎实依据？`],
  ["下行风险排查", c => `${c}当前面临的最主要下行风险与估值下杀隐患是什么？近期是否触发预警指标？`],
  ["同业比较", c => `${c}和同业相比，增长、利润率和估值处在什么位置？`],
];

/** Split an answer into its first conclusions and the rest. */
export function answerParts(text) {
  const t = String(text || "").trim();
  const sentences = t.match(/[^。！？!?]+[。！？!?]?/g) || (t ? [t] : []);
  return {conclusions: sentences.slice(0, 3).map(s => s.trim()).filter(Boolean), rest: sentences.slice(3).join("").trim()};
}

/** The answer's four parts: the reviewed text split at its headings, else the server's sections. */
export function answerSections(r) {
  const text = String(r?.display_answer || "").trim();
  if (text) {
    const out = [];
    let current = null;
    for (const raw of text.split(/\n+/)) {
      const line = raw.trim();
      if (!line) continue;
      const title = PART_TITLES.find(t => line.replace(/^[【\[]|[】\]：:]$/g, "") === t);
      if (title) { current = {title, body: []}; out.push(current); continue; }
      if (!current) { current = {title: null, body: []}; out.push(current); }
      current.body.push(line);
    }
    if (out.some(x => x.title)) return out.map(x => ({title: x.title, body: x.body.join("\n")})).filter(x => x.body);
  }
  const sections = Array.isArray(r?.sections) ? r.sections : [];
  return sections.filter(x => x && x.body).map(x => ({title: x.title, body: x.body}));
}

/** One chip per material request: 「正在获取：…（待获取）」, then 已取到 / 来源已用尽 as it moves. */
export function requestChips(r, live = {}) {
  return (r?.material_requests || []).filter(x => x && x.id).map(x => {
    const now = live[x.id] || {};
    const status = now.status || x.status || "open";
    const label = now.status_label || x.status_label || "待获取";
    const word = REQUEST_WORD[status] || "正在获取";
    return {id: x.id, status, resolved: status === "found" || status === "partial",
            text: `${word}：${x.what || "补充材料"}（${label}）`};
  });
}

/** Seconds as the reader says them: "45 秒" / "1 分 05 秒". */
export function needsNarrowerQuestion(value) {
  const recovery = value?.recovery || value?.body?.recovery;
  return recovery?.schema_version === "cockpit-job-recovery:0.1"
    && recovery.code === "model_input_bound" && recovery.retryable === false
    && recovery.action === "edit_question";
}

export function elapsedText(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
}

/** The request body for one question (pure): a follow-up names its answer. */
export function askRequest(question, {requestId, followUp = null} = {}) {
  const body = {question: String(question || "").trim(), request_id: requestId};
  if (followUp && followUp.job_id) body.follow_up_of = followUp.job_id;
  return body;
}

export function renderAnswer(ctx, r, {feedback = "collapsed"} = {}) {
  const {h, ui, fmt, say} = ctx;
  const box = h("div", {class: "v2-answer"});
  if (!r) { box.append(ui.empty("没有回答内容。")); return box; }
  if (r.follow_up?.question) box.append(h("p", null, h("span", {class: "v2-chip"}, `已关联上下文：紧接上一回答「${lead(r.follow_up.question, 60)}」继续追问`)));
  if (r.refused) { box.append(ui.alert(say(r.refusal_label || r.refusal_reason || "这个问题现有资料无法回答。"))); }
  if (r.resume?.label) box.append(h("p", null, h("span", {class: "v2-chip"}, say(r.resume.label))));
  const parts = answerSections(r);
  if (parts.length) {
    // Batch G: the four parts, in the order an analyst answers.
    for (const part of parts) {
      if (part.title) box.append(h("h3", null, part.title));
      box.append(...ui.paragraphs(say(part.body), ctx.seen));
    }
  } else {
    const text = r.display_answer || say(r.answer || "");
    const {conclusions, rest} = answerParts(text);
    box.append(h("h3", null, "核心研判摘要"), h("ol", null, conclusions.map(s => h("li", null, ui.glossed(s, ctx.seen)))));
    if (rest) box.append(h("details", {class: "v2-more"}, h("summary", null, "展开详细推演与论证正文"), ...ui.paragraphs(rest, ctx.seen)));
    const gaps = (r.display_gaps || r.gaps || []).filter(Boolean);
    if (gaps.length) box.append(h("h3", null, "数据缺失与待求证事项"), h("ul", null, gaps.map(g => h("li", null, say(g)))));
  }
  const chips = requestChips(r);
  if (chips.length) {
    const holder = h("div", {class: "v2-request-chips", role: "status", "aria-live": "polite"});
    const draw = live => holder.replaceChildren(...requestChips(r, live).map(c => h("span", {class: "v2-chip", "data-request": c.id, "data-status": c.status}, c.text)));
    draw({});
    box.append(holder);
    if (watchRequests) watchRequests(r, draw);
  }
  const cites = r.citations || [];
  const meta = c => {
    const period = c.period_label || c.period || "";
    const form = c.form || c.form_type || String(c.title || "").match(/\b(?:Form\s+)?(10-[QK]|8-K)(?:\/A)?\b/i)?.[0];
    const filing = form ? `${/^Form\s/i.test(form) ? form : `Form ${form}`}（${period ? `${period} ` : ""}官方申报）` : "";
    return [c.company ? say(c.company) : "", filing || say(c.kind_label || c.block_label || ""),
      c.house ? say(c.house) : "", c.title && !form ? `《${lead(say(c.title), 60)}》` : "",
      !filing ? period : ""].filter(Boolean).join(" ｜ ");
  };
  if (cites.length) {
    box.append(h("h3", null, `支撑事实与引证底稿（${cites.length} 条）`), h("ol", {class: "v2-cite"}, cites.slice(0, 5).map(c => h("li", null,
      lead(say(c.statement), 200), h("div", {class: "v2-note"}, meta(c))))));
    // A03: the rest carry the same details as the first five.
    if (cites.length > 5) box.append(h("details", {class: "v2-more"}, h("summary", null, `其余 ${cites.length - 5} 条依据`),
      h("ol", {class: "v2-cite", start: "6"}, cites.slice(5).map(c => h("li", null, lead(say(c.statement), 200), h("div", {class: "v2-note"}, meta(c)))))));
  }
  // The answer's own details, collapsed; never its cost (am 6.3).
  const details = [CONFIDENCE[r.confidence] || null,
    r.answered_at ? `生成时间：${fmt.datetime(r.answered_at)}` : null,
    r.replayed ? "沿用已完成的分析" : null].filter(Boolean);
  if (details.length) box.append(h("details", {class: "v2-more"}, h("summary", null, "回答详情"), h("p", {class: "v2-note"}, details.join(" · "))));
  // Feedback on this answer (bound to what was said and cited), A03-A08.
  if (r.feedback && feedback !== "none") {
    if (feedback === "open") box.append(h("h3", null, "对这条回答的反馈"), feedbackBar(ctx, r.feedback, {title: "这条回答"}));
    else box.append(feedbackDisclosure(ctx, r.feedback, {title: "这条回答", summary: "对这条回答留反馈"}));
  }
  return box;
}

async function render(root, ctx) {
  const {h, ui, fmt, say} = ctx;
  root.append(h("div", {class: "v2-page-head"}, h("h1", null, LABELS.page_title.ask),
    h("span", {class: "v2-sub"}, "基于本项目已沉淀的申报、财报与纪要底稿作答；对任何一条回答点「继续深入追问」，系统会带上那条回答和它的依据。")));
  const [ov, hist] = await Promise.all([load(ctx, OVERVIEW), load(ctx, "/v1/cockpit/history?kind=ask", {fresh: true})]);
  if (!ctx.alive()) return;
  const list = ov.value ? companies(ov.value) : [];
  const example = list[0] ? `例如：${companyName(list[0])} 最近一个季度的收入增速和利润率变化，主要原因是什么？` : "例如：这个行业最近一个季度的需求变化如何？";
  // A08: an overview that could not be read is "unknown", not "available".
  const modelOk = ov.value ? ov.value.model_available?.available !== false : true;

  const scope = ui.field({id: "ask-scope", label: "聚焦标的", control: "select", help: "指定某家标的，或针对整个行业组合综合提问"});
  scope.input.append(h("option", {value: ""}, "整个行业组合"), ...list.map(c => h("option", {value: companyName(c), selected: ctx.state.filters.company === (c.ticker || c.company_ref) || null}, companyName(c))));
  const question = ui.field({id: "ask-question", label: "你的问题", help: example, required: true, maxLength: 1900});
  ctx.drafts.bind(question.input, "ask:question");
  const starters = h("div", {class: "v2-toggle-row", role: "group", "aria-label": "快捷投研问题模板（点击填入输入框，不会直接发送）"},
    STARTERS.map(([label, make]) => ui.button(label, () => {
      question.input.value = make(scope.input.value || "这个行业的公司");
      ctx.drafts.set("ask:question", question.input.value);
      question.input.focus();
    }, {variant: "quiet", class: "v2-btn v2-btn-quiet v2-toggle", "aria-label": `填入问题模板：${label}`})));

  // The follow-up chip: which answer this question builds on (A01).
  let followUp = ctx.drafts.getJson(FOLLOW);
  let boundQuestion = null;
  const chip = h("div", {id: "ask-follow", role: "status", "aria-live": "polite"});
  const drawChip = () => {
    chip.replaceChildren();
    if (!followUp) return;
    chip.append(h("span", {class: "v2-chip"}, `已关联上下文：紧接上一回答「${lead(followUp.question, 60)}」继续追问`), " ",
      ui.button("解除关联，提出独立新问题", () => { followUp = null; ctx.drafts.setJson(FOLLOW, null); drawChip(); question.input.focus(); },
                {variant: "quiet", "aria-label": `不再基于上一回答：${lead(followUp.question, 60)}`}));
  };
  drawChip();

  const status = h("div", {role: "status", "aria-live": "polite"});
  const submit = ui.button("提交投研提问", null);
  if (!modelOk) {
    submit.disabled = true;
    status.append(ui.state({tone: "warn", status: say(ov.value.model_available.reason || "AI 服务暂时不可用，暂时不能提问。"),
      impact: "暂时不能提新问题；下面的历史回答照常可看。", owner: "管理员会恢复 AI 服务。", next: `稍后点顶部的「${UI.refresh}」再试。`}));
  }
  const form = h("form", {class: "v2-panel", novalidate: true}, scope.wrap, starters, chip, question.wrap,
    h("div", {class: "v2-actions"}, submit), status);
  submit.type = "submit";
  const current = h("div", {id: "ask-current"});
  root.append(form, current);

  /* Material requests: fetch their status once per answer shown and again
     every minute while the page is open; a resolved one offers the resume. */
  const watchers = [];
  const pollRequests = async () => {
    const ids = [...new Set(watchers.flatMap(w => (w.r.material_requests || []).map(x => x.id).filter(Boolean)))].slice(0, 10);
    if (!ids.length || !ctx.alive()) return;
    const got = await load(ctx, `${REQUESTS}?ids=${encodeURIComponent(ids.join(","))}`, {fresh: true});
    if (!ctx.alive() || got.error) return;
    const live = Object.fromEntries((got.value?.requests || []).filter(x => x && x.id).map(x => [x.id, x]));
    for (const w of watchers) w.draw(live);
  };
  let pollTimer = null;
  watchRequests = (r, draw) => {
    watchers.push({r, draw});
    if (watchers.length > 50) watchers.splice(0, watchers.length - 50);
    clearTimeout(pollTimer);
    pollTimer = setTimeout(function tick() {
      pollRequests().finally(() => { if (ctx.alive()) { pollTimer = setTimeout(tick, 60000); pollTimer?.unref?.(); } });
    }, 0);
    pollTimer?.unref?.();
  };

  // History: searchable, each answer can be followed up or asked again.
  const history = ui.section("ask-history", "历史问题");
  const items = hist.error ? [] : (hist.value.items || []).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));

  /* Wait for one job with the elapsed time and a stop button; resolve the
     result, or reject (err.cancelled when the reader stopped waiting). */
  const wait = (card, jobId) => {
    const started = Date.now();
    const clock = h("span", null, "已耗时 0 秒");
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const stop = ui.button("后台继续分析（离开当前等待）", () => controller?.abort(), {variant: "quiet", "aria-label": "后台继续分析这条回答（离开当前等待，结果进入历史记录）"});
    const progress = h("div", {class: "v2-progress", role: "status", "aria-live": "off"},
      h("span", {class: "v2-progress-bar", "aria-hidden": "true"}, h("span")),
      h("span", null, "正在检索申报文件底稿并提炼研判论据（通常需 30–60 秒）· "), clock, stop);
    card.append(progress);
    const timer = setInterval(() => { clock.textContent = `已耗时 ${elapsedText((Date.now() - started) / 1000)}`; }, 1000);
    return ctx.api.job(jobId, j => {
      clock.textContent = `已耗时 ${elapsedText((Date.now() - started) / 1000)}`;
      if (j && j.status === "running") progress.setAttribute("data-status", "running");
    }, {signal: controller?.signal}).finally(() => { clearInterval(timer); progress.remove(); });
  };

  const showResult = (card, result, entry) => {
    card.append(renderAnswer(ctx, result, {feedback: "open"}));
    card.append(h("div", {class: "v2-actions"}, followButton(entry), resumeButton(entry)));
    status.replaceChildren(ui.receipt("回答已完成，也已加入下面的历史记录。"));
    ctx.announce("回答已完成");
  };

  /* Ask, or ask again with the same request id (the analysis already done
     is reused).  `pending` = {question, request_id, follow}. */
  const ask = async pending => {
    submit.disabled = true;
    ctx.drafts.setJson(PENDING, pending);
    status.replaceChildren(ui.receipt("已提交。"));
    const card = h("div", {class: "v2-card", tabindex: "-1"},
      pending.follow ? h("p", null, h("span", {class: "v2-chip"}, `已关联上下文：紧接上一回答「${lead(pending.follow.question, 60)}」继续追问`)) : null,
      h("h3", null, pending.question));
    current.replaceChildren(card);
    let jobId = pending.job_id || null;
    try {
      if (!jobId) {
        const job = await ctx.api.post("/v1/cockpit/ask", askRequest(pending.question, {requestId: pending.request_id, followUp: pending.follow}));
        jobId = job.job_id;
        ctx.drafts.setJson(PENDING, {...pending, job_id: jobId});
      }
      const result = await wait(card, jobId);
      ctx.drafts.setJson(PENDING, null);
      ctx.drafts.clear("ask:question");
      question.input.value = "";
      followUp = null; ctx.drafts.setJson(FOLLOW, null); drawChip();
      const entry = {job_id: jobId, status: "done", request: {question: pending.question, request_id: pending.request_id,
                     follow_up_question: pending.follow?.question || null}, result, created_at: new Date().toISOString()};
      items.unshift(entry);   // A06: the answer is in the history at once
      draw();
      showResult(card, result, entry);
    } catch (err) {
      if (err && err.cancelled) {
        card.append(ui.state({status: "已转入后台继续分析。", impact: "结果生成后将自动归档至下方历史记录，无需在此停留等候。",
          owner: "系统自动完成，不需要重新提问。", next: "可以继续做别的事，或点「回到前台等待」。",
          actions: [{label: "回到前台等待", onClick: () => ask({...pending, job_id: jobId})}]}));
        status.replaceChildren();
      } else {
        // A permanent input bound needs a new, narrower question, never replay.
        const narrow = needsNarrowerQuestion(err);
        if (narrow) { boundQuestion = pending.question; ctx.drafts.setJson(PENDING, null); }
        const edit = () => {
          question.input.value = pending.question;
          ctx.drafts.set("ask:question", question.input.value);
          question.input.focus();
          globalThis.window?.scrollTo?.(0, 0);
        };
        card.append(ui.state({tone: "danger", status: err.display_message || "这次没有得到回答。",
          impact: "这条问题暂时没有回答；你的问题还保留在输入框里。",
          owner: narrow ? "请指定公司、季度或具体指标，缩小问题范围。" : "多数情况重试即可；资料跨度较大时，建议缩小至具体季度或科目后重试。",
          next: narrow ? "修改问题后重新提问。" : "点「原问题直接重试」重新提问，也可以修改问题后再问。",
          actions: narrow ? [{label: "缩小范围后重新提问", onClick: edit}]
            : [{label: "原问题直接重试", onClick: () => ask({...pending, job_id: null})}]}));
        status.replaceChildren(ui.receipt("没有得到回答。", "danger"));
      }
    } finally { submit.disabled = !modelOk; }
  };

  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (!question.check()) { question.input.focus(); return; }
    // A04: the chosen company is part of the question the server reads.
    const text = (scope.input.value && !question.input.value.includes(scope.input.value) ? `关于${scope.input.value}：` : "") + question.input.value.trim();
    if (boundQuestion === text) {
      question.setError("请先指定公司、季度或具体指标，缩小原问题范围后再问。");
      question.input.focus();
      return;
    }
    boundQuestion = null;
    ask({question: text, request_id: ctx.requestId(), follow: followUp ? {job_id: followUp.job_id, question: followUp.question} : null});
  });

  /* 「用新材料续答」: the server re-reads the evidence and answers again only
     when a request it opened has brought new material (never twice per request). */
  function resumeButton(it) {
    if (!(it?.result?.material_requests || []).some(x => x && x.id)) return null;
    return ui.button("用新材料续答", async () => {
      status.replaceChildren(ui.receipt("正在用新取到的材料重新检索…"));
      try {
        const job = await ctx.api.post("/v1/cockpit/ask_resume", {job_id: it.job_id});
        const card = h("div", {class: "v2-card", tabindex: "-1"}, h("h3", null, it.request?.question || ""));
        current.replaceChildren(card);
        const result = await wait(card, job.job_id);
        const entry = {job_id: job.job_id, status: "done", request: {...(it.request || {}), resume_of: it.job_id}, result, created_at: new Date().toISOString()};
        items.unshift(entry);
        draw();
        showResult(card, result, entry);
      } catch (err) {
        status.replaceChildren(ui.receipt(err?.display_message || "材料还没有取到，暂时不能续答。", "danger"));
      }
    }, {variant: "secondary", "aria-label": `用新取到的材料续答：${lead(it.request?.question || "", 40)}`});
  }

  function followButton(it) {
    return ui.button("继续深入追问 →", () => {
      followUp = {job_id: it.job_id, question: it.request?.question || ""};
      ctx.drafts.setJson(FOLLOW, followUp);
      drawChip();
      question.input.value = "";
      question.input.focus();
      globalThis.window?.scrollTo?.(0, 0);
    }, {variant: "secondary", "aria-label": `基于这条回答继续深入追问：${lead(it.request?.question || "", 40)}`});
  }

  root.append(history);
  if (hist.error) { history.append(ui.errorBox(hist.error)); }
  const search = ui.field({id: "ask-search", label: "检索历次提问与回答要点", control: "input", attrs: {type: "search", value: ctx.state.filters.q || ""}});
  const listEl = h("ul", {class: "v2-list"});
  function draw() {
    const term = String(search.input.value || "").trim().toLowerCase();
    const hay = it => `${it.request?.question || ""} ${it.result?.display_answer || it.result?.answer || ""}`.toLowerCase();
    const shown = items.filter(it => !term || hay(it).includes(term));
    listEl.replaceChildren(...(shown.length ? shown.map(it => {
      const q = it.request?.question || "（问题内容缺失）";
      const actions = [];
      if (it.status === "done" && it.job_id) { actions.push(followButton(it)); const again = resumeButton(it); if (again) actions.push(again); }
      if (it.status === "failed") {
        actions.push(ui.button(needsNarrowerQuestion(it) ? "缩小范围后重新提问" : "编辑后重问", () => {
          if (needsNarrowerQuestion(it)) { boundQuestion = q; ctx.drafts.setJson(PENDING, null); }
          question.input.value = it.request?.question || "";
          ctx.drafts.set("ask:question", question.input.value);
          question.input.focus();
          globalThis.window?.scrollTo?.(0, 0);
        }, {variant: "quiet", "aria-label": `编辑后重问：${lead(q, 40)}`}));
        if (it.request?.request_id && !needsNarrowerQuestion(it)) actions.push(ui.button("原问题直接重试", () => ask({question: q, request_id: it.request.request_id,
          follow: it.request.follow_up_of ? {job_id: it.request.follow_up_of, question: it.request.follow_up_question || ""} : null}),
          {variant: "secondary", "aria-label": `原问题直接重试：${lead(q, 40)}`}));
      }
      return h("li", {class: "v2-card"},
        h("div", {class: "v2-card-meta"}, h("span", null, fmt.datetime(it.created_at)),
          ctx.ui.tag(it.status === "done" ? "已回答" : it.status === "failed" ? "没有回答" : "处理中", it.status === "failed" ? "danger" : it.status === "done" ? "ok" : null),
          it.request?.follow_up_question ? h("span", {class: "v2-chip"}, `追问：${lead(it.request.follow_up_question, 30)}`) : null,
          it.request?.resume_of ? h("span", {class: "v2-chip"}, "用新材料续答") : null),
        h("h3", null, q),
        it.status === "failed" ? ui.alert(say(it.display_message || "这次没有得到回答。"), "danger") : null,
        it.status === "done" ? h("details", {class: "v2-more"}, h("summary", {"aria-label": `查看回答：${lead(q, 40)}`}, "查看回答"), renderAnswer(ctx, it.result)) : null,
        actions.length ? h("div", {class: "v2-actions"}, actions) : null);
    }) : [ui.empty(term ? "没有找到相关的历史问题。" : "还没有提过问题。")]));
  }
  search.input.addEventListener("input", () => { draw(); clearTimeout(search.timer); search.timer = setTimeout(() => ctx.setFilter("q", search.input.value.trim()), 400); });
  history.append(search.wrap, listEl);
  draw();

  // A question that was still being answered when the page closed: keep waiting.
  const held = ctx.drafts.getJson(PENDING);
  if (held && held.job_id && held.question && modelOk) ask(held);
}

V().registerPage("ask", {title: UI.nav.ask, nav: {label: UI.nav.ask, order: 30}, render, update: () => true});
