/* 研究设置 → 每周投研周报 (2026-10-09, independent review W01-W06 / §9, MUSE §7,
   financial 4.16): the weekly brief inside the workbench instead of a page of
   its own.  Reads GET /v1/cockpit/weekly-brief and saves through the existing
   POST /v1/cockpit/weekly_brief_plan (the owner's principal, the same
   expected_plan_hash guard).  What it adds for the reader:

   - 发送到 is chosen from the destinations this project has used (and 「只在
     工作台保存」); a new group's address is typed only under 高级 (W02/W06);
   - 时区 is a choice with the reader's own zone first (W02);
   - 下一次发送 is shown in the plan's zone and the reader's (W03/W04);
   - 历史 lists each issue with its delivery result; an issue that was not
     delivered says so and what happens next (W04/W05, failure recovery).

   There is no pause: the server has no paused plan, and a fake pause would be
   a promise the scheduler does not keep. */

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const ZONES = [["Asia/Shanghai", "北京/上海时间"], ["Asia/Hong_Kong", "香港时间"], ["America/New_York", "纽约时间"],
               ["Europe/London", "伦敦时间"], ["Asia/Tokyo", "东京时间"], ["UTC", "协调世界时（UTC）"]];
const APPS = {discord: "Discord", feishu: "飞书", lark: "飞书", slack: "Slack", telegram: "Telegram"};

/** A destination in words; the internal address never shows (W06). */
export function destinationLabel(ref) {
  if (ref == null || ref === "") return "只在工作台保存";
  const s = String(ref);
  if (s.startsWith("cockpit:weekly-brief:")) return "只在工作台保存";
  const m = s.match(/^openclaw:([a-z0-9_-]+):([^:]+):(channel|chat|user|group):(.+)$/i);
  if (m) {
    const app = APPS[m[1].toLowerCase()] || "已连接的应用";
    const kind = {channel: "频道", chat: "群聊", user: "私信", group: "群组"}[m[3].toLowerCase()];
    return `${app}${m[2] === "default" ? "" : ` ${m[2]}`}${kind}（尾号 ${m[4].slice(-4)}）`;
  }
  return "已连接的群组";
}

const zoneName = zone => (ZONES.find(([z]) => z === zone) || [null, zone])[1];

/** "2026-10-14 07:00（纽约时间）" for an instant in a zone. */
export function zonedText(iso, zone) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(d).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}（${zoneName(zone)}）`;
  } catch (_e) { return d.toISOString().slice(0, 16).replace("T", " ") + "（UTC）"; }
}

/** Each scheduled issue with its delivery result (pure). */
export function briefHistory(view, now = Date.now()) {
  const delivered = new Map((view?.deliveries || []).map(d => [d.issue_version_ref, d]));
  return (view?.cycles || []).map(c => {
    const d = c.issue_version_ref ? delivered.get(c.issue_version_ref) : null;
    const due = new Date(c.scheduled_for).getTime();
    const late = Number.isFinite(due) && now - due > 6 * 3600 * 1000;
    const local = c.destination_ref == null || String(c.destination_ref).startsWith("cockpit:weekly-brief:");
    const state = d ? (local ? "saved" : "delivered") : c.manual && c.issue_version_ref ? "generated" : (late ? "failed" : "pending");
    return {scheduled_for: c.scheduled_for, destination: c.manual && !c.destination_ref ? "未安排发送" : destinationLabel(c.destination_ref), delivered_at: d?.delivered_at || null,
      issue_version_ref: c.issue_version_ref || null,
      manual: Boolean(c.manual), local,
      brief_kind_label: c.brief_kind === "mission_progress" ? "研究进展周报" : "投研周报", state};
  });
}

/** Only text nodes: official Markdown cannot inject HTML, scripts or links. */
export function briefBody(ctx, body, {title = ""} = {}) {
  const {h, ui} = ctx;
  const article = h("article", {class: "v2-panel v2-weekly-document"});
  let section = article;
  const lines = String(body).split(/\r?\n/);
  const cells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(x => x.trim());
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      if (i === 0 && heading[1] === "#" && title === heading[2].trim()) continue;
      if (/^(记录详情|来源与 authority|来源与记录详情|附录：版本与出处)$/.test(heading[2].trim())) {
        section = h("details", {class: "v2-more"}, h("summary", null, "来源与记录详情"));
        article.append(section);
      } else {
        section = article;
        section.append(h(`h${Math.min(heading[1].length + 1, 4)}`, null, heading[2]));
      }
      continue;
    }
    if (line.trim().startsWith("|") && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1] || "")) {
      const headers = cells(line);
      i++;
      const rows = [];
      while ((lines[i + 1] || "").trim().startsWith("|")) rows.push(cells(lines[++i]));
      section.append(ui.table({columns: headers.map((label, index) => ({key: String(index), label,
        render: row => row[index] || ""})), rows}));
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [line.replace(/^\s*[-*]\s+/, "")];
      while (/^\s*[-*]\s+/.test(lines[i + 1] || "")) items.push(lines[++i].replace(/^\s*[-*]\s+/, ""));
      section.append(h("ul", null, items.map(text => h("li", null, text))));
      continue;
    }
    const paragraph = [line];
    while (i + 1 < lines.length && lines[i + 1].trim() && !/^(#{1,3})\s+/.test(lines[i + 1]) && !lines[i + 1].trim().startsWith("|") && !/^\s*[-*]\s+/.test(lines[i + 1])) {
      paragraph.push(lines[++i]);
    }
    section.append(h("p", {class: "v2-pre-line"}, paragraph.join("\n")));
  }
  return article;
}

const STATE_WORDS = {delivered: "已送达", saved: "已保存", generated: "已生成", pending: "生成或发送中", failed: "未送达"};

export function weeklyBriefSection(ctx, view, {onSaved} = {}) {
  const {h, ui, fmt} = ctx;
  const box = h("div", {id: "brief-body"});
  if (!view || view.enabled === false) {
    box.append(ui.state({status: "这个研究项目还不能设定每周投研周报。", impact: "不会自动生成每周投研周报。",
      owner: "管理员可以开通。", next: "需要时请联系管理员。"}));
    return box;
  }
  const plan = view.plan;
  const browserZone = fmt.zone || "UTC";
  // Current plan, next time, in words.
  if (view.problem) box.append(ui.state({tone: "danger", status: "每周投研周报的计划读不出来。", impact: "到点可能不会生成简报。",
    owner: "管理员需要检查计划。", next: "重新保存一次计划通常可以恢复。", technical: h("p", null, String(view.problem))}));
  else if (!plan) box.append(ui.state({status: "还没有设定每周投研周报。", impact: "不会自动生成每周投研周报。",
    owner: "由你决定是否开启。", next: "在下面选择每周的时间和发送对象后保存。"}));
  else box.append(ui.kv([
    ["每周", `${plan.weekday_label || WEEKDAYS[plan.weekday]} ${String(plan.hour).padStart(2, "0")}:${String(plan.minute).padStart(2, "0")}（${zoneName(plan.timezone)}）`],
    ["发送到", destinationLabel(plan.local ? null : plan.destination_ref)],
    ["下一次", view.next_run_at ? `${zonedText(view.next_run_at, plan.timezone)}${plan.timezone !== browserZone ? `；你这里是 ${zonedText(view.next_run_at, browserZone)}` : ""}` : "暂时算不出"],
  ]));

  // The form.
  const form = h("form", {class: "v2-panel", id: "brief-form", novalidate: true, "aria-labelledby": "brief-form-title"});
  const day = ui.field({id: "brief-weekday", label: "星期", control: "select"});
  day.input.append(...WEEKDAYS.map((w, i) => h("option", {value: String(i), selected: (plan ? plan.weekday === i : i === 0) || null}, w)));
  const time = ui.field({id: "brief-time", label: "时间", control: "input",
    attrs: {type: "time", value: plan ? `${String(plan.hour).padStart(2, "0")}:${String(plan.minute).padStart(2, "0")}` : "07:00"}});
  const zone = ui.field({id: "brief-zone", label: "时区", control: "select", help: "按这个时区的时间生成；夏令时自动调整。"});
  const zones = [[browserZone, `${zoneName(browserZone)}（你的浏览器）`], ...ZONES.filter(([z]) => z !== browserZone)];
  if (plan && !zones.some(([z]) => z === plan.timezone)) zones.push([plan.timezone, plan.timezone]);
  zone.input.append(...zones.map(([z, label]) => h("option", {value: z, selected: (plan ? plan.timezone === z : z === browserZone) || null}, label)));
  const dest = ui.field({id: "brief-dest", label: "发送到", control: "select", help: "只能选已经连接好的群组；新增群组请管理员连接。"});
  const known = (view.destinations || []).filter(ref => !String(ref).startsWith("cockpit:weekly-brief:"));
  dest.input.append(h("option", {value: "", selected: (!plan || plan.local) || null}, "只在工作台保存（不外发）"),
    ...known.map(ref => h("option", {value: ref, selected: (plan && !plan.local && plan.destination_ref === ref) || null}, destinationLabel(ref))));
  const other = ui.field({id: "brief-dest-other", label: "其他已连接群组的地址", control: "input",
    help: "管理员连接新群组后会给你这个地址；填写后优先使用它。"});
  const status = h("div", {role: "status", "aria-live": "polite"});
  const save = h("button", {type: "submit", class: "v2-btn"}, plan ? "保存修改" : "开启每周投研周报");
  form.append(h("h3", {id: "brief-form-title"}, plan ? "修改每周投研周报" : "开启每周投研周报"),
    h("div", {class: "v2-grid v2-grid-tight"}, day.wrap, time.wrap, zone.wrap), dest.wrap,
    h("details", {class: "v2-more"}, h("summary", null, "高级：使用其他已连接的群组"), other.wrap),
    h("div", {class: "v2-actions"}, save), status);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(time.input.value || ""));
    if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) { time.setError("请选择时间，例如 07:00"); time.input.focus(); return; }
    time.setError("");
    save.disabled = true;
    status.replaceChildren(ui.receipt("正在保存…"));
    try {
      const out = await ctx.api.post("/v1/cockpit/weekly_brief_plan", {weekday: Number(day.input.value), hour: Number(m[1]), minute: Number(m[2]),
        timezone: zone.input.value, destination_ref: String(other.input.value || "").trim() || dest.input.value || null,
        expected_plan_hash: view.plan_hash || null});
      status.replaceChildren(ui.receipt(out?.status === "unchanged" ? "计划本来就是这样，没有改动。" : "已保存，下一次到点时生成。"));
      onSaved && onSaved();
    } catch (err) {
      status.replaceChildren(ui.receipt(`没有保存：${(err && err.display_message) || "请稍后重试"}`, "danger"));
    } finally { save.disabled = false; }
  });
  box.append(form);

  // History with the delivery result of each issue (W04/W05).
  const rows = briefHistory(view);
  const documentPanel = h("section", {class: "v2-panel v2-weekly-document", hidden: true, tabindex: "-1", "aria-live": "polite"});
  let documentRequest = 0;
  const viewBrief = async (row, trigger) => {
    const request = ++documentRequest;
    const close = h("button", {type: "button", class: "v2-btn v2-btn-secondary"}, "关闭周报");
    close.addEventListener("click", () => { documentRequest++; documentPanel.hidden = true; documentPanel.replaceChildren(); trigger.focus(); });
    documentPanel.hidden = false;
    documentPanel.replaceChildren(close, ui.receipt("正在读取周报…"));
    try {
      const document = await ctx.api(`/v1/cockpit/weekly-brief-document?issue_version_ref=${encodeURIComponent(row.issue_version_ref)}`);
      if (request !== documentRequest) return;
      if (document?.issue_version_ref !== row.issue_version_ref || typeof document.body !== "string") throw new Error("invalid brief response");
      documentPanel.replaceChildren(h("h3", null, document.title || row.brief_kind_label),
        h("p", null, `${fmt.datetime(document.period?.start)} 至 ${fmt.datetime(document.period?.end)}`),
        close, briefBody(ctx, document.body, {title: document.title || row.brief_kind_label}));
      const compact = documentPanel.ownerDocument.defaultView?.matchMedia?.("(max-width:1024px)").matches;
      const top = documentPanel.ownerDocument.querySelector(compact ? ".v2-rail" : ".v2-top")?.getBoundingClientRect?.();
      documentPanel.style.scrollMarginTop = `${top ? Math.max(0, Math.ceil(top.bottom)) + 16 : 148}px`;
      documentPanel.focus({preventScroll: true});
      documentPanel.scrollIntoView({block: "start"});
    } catch (err) {
      if (request !== documentRequest) return;
      documentPanel.replaceChildren(close, ui.receipt(err?.display_message || "周报暂时读不出来，请稍后再试。", "danger"));
    }
  };
  box.append(h("h3", null, "最近几期"), rows.length ? ui.table({caption: `时间按你的浏览器时区显示（${zoneName(browserZone)}）`, rowHeader: false, columns: [
    {key: "at", label: "期次时间", render: r => `${fmt.datetime(r.scheduled_for)}${r.manual ? "（报告窗口结束）" : "（计划）"}`},
    {key: "kind", label: "类型", render: r => r.brief_kind_label},
    {key: "to", label: "发送到", render: r => r.destination},
    {key: "state", label: "结果", render: r => ui.tag(STATE_WORDS[r.state], (r.state === "delivered" || r.state === "saved") ? "ok" : r.state === "failed" ? "danger" : null)},
    {key: "when", label: rows.every(r => r.local) ? "保存时间" : "保存或送达时间", render: r => (r.delivered_at ? fmt.datetime(r.delivered_at) : "—")},
    {key: "document", label: "正文", render: r => {
      if (!r.issue_version_ref) return "尚未生成";
      const button = h("button", {type: "button", class: "v2-btn v2-btn-secondary"}, "查看周报");
      button.addEventListener("click", () => viewBrief(r, button));
      return button;
    }},
  ], rows}) : ui.empty("还没有生成过简报。"));
  box.append(documentPanel);
  if (rows.some(r => r.state === "failed" && r.issue_version_ref && !r.local)) box.append(ui.state({tone: "warn", status: "有周报尚未送达。",
    impact: "可以查看已生成的正文，核对发送设置。", owner: "发送服务由管理员维护。",
    next: "需要补发时，把这一期的计划时间告诉管理员。"}));
  if (rows.some(r => r.state === "failed" && !r.issue_version_ref)) box.append(ui.state({tone: "warn", status: "本期周报尚未生成。",
    impact: "暂时没有可查看的正文。", owner: "生成进度由管理员核对。",
    next: "请查看任务进展或联系管理员。"}));
  const local = view.local_briefs || [];
  if (local.length) box.append(h("details", {class: "v2-more"}, h("summary", null, `保存在工作台的简报（${local.length} 期）`),
    h("ul", null, local.map(b => h("li", null, `${fmt.datetime(b.written_at)} · ${b.name}`)))));
  return box;
}
