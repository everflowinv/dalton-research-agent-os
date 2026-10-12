/* Cockpit v2 · 管理员设置 (agent B).
 *
 * Everything the 2026-10-08 analyst UX review moves out of the analyst's
 * view.  Six areas: AI 服务 / 数据连接 / 运行控制 / 预算与费用 / 健康与故障 /
 * 研究项目管理.  Runs embedded in the v2 shell (window.DaltonV2.registerPage)
 * or standalone on /v2/admin (#admin-root).
 *
 * Reads reuse /v1/cockpit/* and add /v2/data/admin/<section>.  Every write
 * goes through POST /v2/data/admin/action {action, reason, payload}: the same
 * cockpit action (and writer path) the old page used, with a required reason
 * and a receipt shown on the spot.  Dangerous actions confirm and look apart.
 * Every server string is set as text, never parsed as markup.
 */
(function () {
  "use strict";
  const ADMIN_VERSION = "2026-10-09.1";
  const TABS = [
    ["ai", "AI 服务"], ["connections", "数据连接"], ["run", "运行控制"],
    ["budget", "预算与费用"], ["health", "健康与故障"], ["projects", "研究项目管理"],
  ];
  const state = { csrf: null, tab: "ai", root: null, loaded: {}, coverageRemove: new Set(),
                  receipts: [], cache: {}, flash: {}, env: null, readAt: {}, aiPurpose: null };

  // ---------- small DOM helpers -------------------------------------------------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = String(v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : (["href", "src", "action"].includes(k) ? (globalThis.DaltonPublicURL || (x => x))(String(v)) : String(v)));
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  /* append / replaceChildren that flatten arrays and drop null/false: the
     DOM's own would print "null" or "[object HTMLDivElement]". */
  function kids(list) { return list.flat(Infinity).filter(c => c != null && c !== false); }
  function put(el, ...children) { el.append(...kids(children)); return el; }
  function swap(el, ...children) { el.replaceChildren(...kids(children)); return el; }
  /* 2026-10-09 (MUSE §10.1): internal words some upstream sentences still
     carry are never shown as labels. */
  const PLAIN_WORDS = [["车道", "研究流程"], ["票据", "执行凭证"], ["工单", "任务"]];
  function plain(v) { let t = String(v); for (const [a, b] of PLAIN_WORDS) t = t.split(a).join(b); return t; }
  const txt = (v, fallback = "—") => (v == null || v === "" ? fallback : plain(v));
  function btn(label, onclick, cls = "") {
    return h("button", { type: "button", class: `adm-btn ${cls}`.trim(), onclick }, label);
  }
  function pill(kind, label) { return h("span", { class: `adm-pill ${kind}` }, label); }
  /* A technical identifier: labelled 「标识」 and collapsed (review §10 /
     MUSE P0 #8).  Never the first thing a row says. */
  function tech(label, value) {
    if (value == null || value === "") return null;
    return h("details", { class: "adm-id", dataset: { role: "tech-id" } }, h("summary", null, "标识"),
      h("span", { class: "adm-tech" }, `${label}：`, h("code", null, String(value))));
  }
  /* Technical detail that is prose (an English reason, a raw sentence). */
  function techNote(label, value) {
    if (value == null || value === "") return null;
    return h("details", { class: "adm-id", dataset: { role: "tech-note" } }, h("summary", null, label || "技术详情"),
      h("div", { class: "adm-tech" }, String(value)));
  }
  function section(title, explain, ...body) {
    return h("section", { class: "adm-sec" }, h("h2", null, title),
      explain ? h("p", { class: "adm-explain" }, explain) : null, ...body);
  }
  function empty(text) { return h("p", { class: "adm-empty" }, text); }
  function table(headers, rows) {
    const thead = h("thead", null, h("tr", null, headers.map(x => {
      const [label, num] = Array.isArray(x) ? x : [x, false];
      return h("th", { class: num ? "num" : null, scope: "col" }, label);
    })));
    const tbody = h("tbody", null, rows.map(r => h("tr", null, r.map((cell, i) => {
      const num = Array.isArray(headers[i]) && headers[i][1];
      return h("td", { class: num ? "num" : null }, cell instanceof Node ? cell : txt(cell));
    }))));
    return h("div", { class: "adm-table-wrap" }, h("table", { class: "adm-table" }, thead, tbody));
  }
  function usd(v) {
    const n = Number(v);
    if (v == null || v === "" || !isFinite(n)) return "—";
    return "US$" + n.toLocaleString("en-US", { minimumFractionDigits: n >= 100 ? 0 : 2, maximumFractionDigits: 2 });
  }
  function count(v) { const n = Number(v); return v == null || !isFinite(n) ? "—" : n.toLocaleString("en-US"); }
  function when(iso) {
    if (!iso) return "—";
    const d = new Date(iso); if (isNaN(d)) return String(iso);
    return d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function errorText(e, what) {
    const m = (e && (e.display_message || e.message)) || "";
    return `${what}没有完成：${m || "请稍后重试"}`;
  }

  // ---------- network -------------------------------------------------------------
  async function getJson(path) {
    const r = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(v.display_message || v.message || `HTTP ${r.status}`); Object.assign(e, v, { status: r.status }); throw e; }
    if (v && v.csrf_token) state.csrf = v.csrf_token;
    return v;
  }
  async function ensureCsrf() {
    if (state.csrf) return state.csrf;
    const shell = window.DaltonV2 && window.DaltonV2.state;
    if (shell && typeof shell.csrf === "string" && shell.csrf) { state.csrf = shell.csrf; return state.csrf; }
    await getJson("/v2/data/admin/actions");
    return state.csrf;
  }
  /* Actions that record the reason themselves (mirrors routes.ACTIONS). */
  const NATIVE_REASON = new Set(["model_select", "lever", "stop_line", "prioritize", "coverage_draft",
                                 "fund_template_select", "language_retry", "owner_door", "needs_human_ack", "workspace_lifecycle"]);
  /* Where the HTTP surface does not route POST /v2/data/admin/action yet (the
     v2 shell serves GETs only), the same cockpit action is sent to its own
     /v1/cockpit/<action> route -- the writer path is identical -- and the
     receipt is assembled here.  The reason travels with actions that take
     one; for the others it is shown on this page only, and the receipt says so. */
  async function directAction(action, reason, payload) {
    const body = { ...payload };
    if (NATIVE_REASON.has(action) && !body.reason) body.reason = reason;
    const r = await fetch(`/v1/cockpit/${encodeURIComponent(action)}`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Dalton-CSRF": state.csrf || "" },
      body: JSON.stringify(body),
    });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(v.display_message || v.message || `HTTP ${r.status}`); Object.assign(e, v, { status: r.status }); throw e; }
    const sync = v && v.sync;
    const envs = sync ? [...(sync.synced || []).map(x => ({ name: x.name || x.environment_id, result: "synced", note: x.note })),
      ...(sync.skipped || []).map(x => ({ name: x.name || x.environment_id, result: "unchanged", note: x.reason })),
      ...(sync.failed || []).map(x => ({ name: x.name || x.environment_id, result: "failed", note: x.reason }))] : [];
    return { status: "done", result: v, receipt: {
      action, action_label: ACTION_LABELS[action] || action, reason, at: new Date().toISOString(),
      reason_recorded_with: NATIVE_REASON.has(action) ? "action" : "page_only",
      status: v.status, version: v.version, reload_note: v.reload_note, reload_instruction: v.reload_instruction,
      message: v.message, requires_restart: v.requires_restart, receipt_outcome: v.receipt && v.receipt.outcome,
      environments: envs, sync_note: sync && sync.note } };
  }
  const ACTION_LABELS = { model_select: "调整 AI 服务选择顺序", model_budget: "调整 AI 环节调用上限", research_budget: "调整研究预算上限",
    model_allow: "允许使用一个 AI 服务", model_metadata: "补充 AI 服务信息", model_notice_ack: "标记 AI 服务变动通知为已读",
    lever: "运行控制", stop_line: "停止一项专项研究", prioritize: "调整公司跟踪频率", coverage_draft: "生成覆盖名单调整草案",
    publish: "确认草案并生效", decide: "放弃草案", fund_template_select: "指定 Excel 模板", language_retry: "重新进行语言质检",
    owner_door: "负责人放行 / 撤回一项专项研究", needs_human_ack: "待办「已知悉 / 暂不提醒」",
    workspace_lifecycle: "管理研究项目状态", workspace_create: "新建研究项目", workspace_rename: "重命名研究项目" };
  async function adminAction(action, reason, payload) {
    await ensureCsrf();
    if (state.directWrites) return directAction(action, reason, payload);
    const r = await fetch("/v2/data/admin/action", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Dalton-CSRF": state.csrf || "" },
      body: JSON.stringify({ action, reason, payload }),
    });
    if (r.status === 404 || r.status === 405) { state.directWrites = true; return directAction(action, reason, payload); }
    const v = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(v.display_message || v.message || `HTTP ${r.status}`);
      Object.assign(e, v, { status: r.status });
      if (r.status === 403 && v.error === "csrf") { state.csrf = null; e.display_message = "登录状态已变化，请刷新页面后重试"; }
      throw e;
    }
    return v;
  }
  const rid = () => (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(16).slice(2) + Date.now());

  // ---------- receipts & write forms ----------------------------------------------
  const RESULT_LABELS = { synced: "已同步", unchanged: "未改动", failed: "未同步" };
  /* What actually happened (review M12): a write the system only accepted, or
     refused, does not say 「已完成」. */
  const REFUSED = /^(refused|rejected|failed|error|denied|conflict)/;
  const ACCEPTED = /^(requested|queued|accepted|pending|running|already_requested|dry_run)/;
  function receiptHeadline(r) {
    const s = String(r.status || r.application_status || r.receipt_outcome || "").toLowerCase();
    if (REFUSED.test(s)) return ["err", "没有生效"];
    if (ACCEPTED.test(s) && r.action === "workspace_lifecycle") return ["", "操作已受理，当前结果见项目卡"];
    if (ACCEPTED.test(s)) return ["", "已受理，正在处理"];
    if (r.host_settings) return r.host_settings.status === "pending" ? ["warn", "共用设置已保存，部分项目待应用"] : ["", "共用设置已保存"];
    if ((r.environments || []).some(e => e.result === "failed")) return ["err", "部分研究项目未同步"];
    if (r.action === "model_select") return /^(saved|applied|fresh|published|updated|duplicate|unchanged|recorded)$/.test(s)
      ? ["", "配置已保存"] : ["warn", "保存结果待确认"];
    return ["", "已完成"];
  }
  function receiptNode(out) {
    const r = out.receipt || {};
    const [kind, head] = receiptHeadline({ ...r, host_settings: r.host_settings || (out.result || {}).host_settings, status: r.status || (out.result || {}).status || r.receipt_outcome || out.status });
    const box = h("div", { class: `adm-receipt${kind ? " " + kind : ""}`, role: "status", dataset: { role: "receipt" } },
      h("b", null, `${head}：${r.action_label || r.action}`), " · ", when(r.at), h("br"),
      `理由：${txt(r.reason)}`,
      r.reason_recorded_with === "page_only" ? h("div", { class: "adm-meta" }, "这项操作本身不记录理由；理由只显示在本页回执里。") : null,
      r.reload_note || r.reload_instruction || r.message ? h("div", null, txt(r.reload_note || r.reload_instruction || r.message)) : null,
      r.requires_restart ? h("div", null, "需要重启相关常驻服务后才完全生效（见「运行控制 → 重启与部署」）。") : null,
      r.receipt_outcome ? h("div", null, `回执结果：${txt(r.receipt_outcome)}`) : null,
      r.version != null ? h("div", null, `已发布为第 ${r.version} 版`) : null,
      tech("操作身份", r.actor_ref));
    if (Array.isArray(r.environments) && r.environments.length) {
      put(box, h("div", null, "各研究项目的结果："),
        h("ul", null, r.environments.map(e => h("li", null,
          `${txt(e.name, "研究项目")}：${RESULT_LABELS[e.result] || txt(e.result)}${e.note ? `（${txt(e.note)}）` : ""}`))));
    } else if (r.sync_note) put(box, h("div", null, txt(r.sync_note)));
    return box;
  }
  function failureNode(e, what) {
    return h("div", { class: "adm-receipt err", role: "alert" }, errorText(e, what),
      e && e.technical_detail ? techNote("技术详情", e.technical_detail) : null);
  }

  /* The one confirmation every consequential write goes through (MUSE P0 #9,
     §10.3, §10.6): a title, the consequence in words, and for a dangerous
     action the environment's name typed back.  Same API as the shell's shared
     component (pages/common.js confirmAction); the administrator page is a
     classic script that also runs standalone, so it carries its own.
     Resolves true only on an explicit confirm. */
  function confirmAction({ title, consequence, danger = false, typeToConfirm = null, confirmLabel = null } = {}) {
    const shared = window.DaltonV2 && typeof window.DaltonV2.confirmAction === "function" ? window.DaltonV2.confirmAction : null;
    if (shared) return Promise.resolve(shared({ title, consequence, danger, typeToConfirm }));
    return new Promise(resolve => {
      /* Inside the page root, so the dialog carries the page's colours both
         standalone and inside the shell; it is position:fixed either way. */
      const host = state.root || document.body;
      const need = typeToConfirm == null ? "" : String(typeToConfirm);
      const titleId = `adm-confirm-${Math.random().toString(16).slice(2)}`;
      const input = need ? h("input", { type: "text", class: "adm-confirm-input", autocomplete: "off", spellcheck: "false",
                                        "aria-label": `输入「${need}」以确认` }) : null;
      const ok = h("button", { type: "button", class: `adm-btn ${danger ? "danger" : "primary"}`, dataset: { role: "confirm-ok" } },
        confirmLabel || (danger ? "我已了解后果，确认执行" : "确认"));
      const cancel = h("button", { type: "button", class: "adm-btn", dataset: { role: "confirm-cancel" } }, "取消");
      const sync = () => { ok.disabled = !!need && (input.value || "").trim() !== need; };
      const dialog = h("div", { class: `adm-dialog${danger ? " danger" : ""}`, role: danger ? "alertdialog" : "dialog",
                                "aria-modal": "true", "aria-labelledby": titleId },
        h("h2", { id: titleId }, title || "确认操作"),
        consequence ? h("p", { class: "adm-dialog-consequence" }, h("b", null, "后果："), plain(consequence)) : null,
        need ? h("label", { class: "adm-field" }, h("span", null, `请输入研究项目名称「${need}」以确认`), input) : null,
        h("div", { class: "adm-dialog-actions" }, cancel, ok));
      const overlay = h("div", { class: "adm-overlay adm-root-overlay", dataset: { role: "confirm" } }, dialog);
      let done = false;
      const close = value => {
        if (done) return; done = true;
        overlay.remove();
        resolve(value);
      };
      ok.addEventListener("click", () => { sync(); if (!ok.disabled) close(true); });
      cancel.addEventListener("click", () => close(false));
      overlay.addEventListener("keydown", ev => {
        if (ev.key === "Escape") { ev.preventDefault(); close(false); }
        else if (ev.key === "Enter" && ev.target === input) { ev.preventDefault(); sync(); if (!ok.disabled) close(true); }
      });
      if (input) input.addEventListener("input", sync);
      sync();
      host.append(overlay);
      try { (input || cancel).focus(); } catch (_e) { /* no focus outside a browser */ }
    });
  }
  function envName() { return (state.env && (state.env.name || state.env.slug)) || "确认"; }

  /* One write: what it changes, a reason (a normal two-line box), a button,
     and the receipt in the same place -- also after the section reloads.
     ``danger``: the button stands apart and the confirmation asks for the
     environment's name.  ``confirm``: the consequence sentence (every write
     with one is confirmed). */
  function writeForm({ action, label, payload, danger = false, confirmText = null, confirmTitle = null,
                       after = null, defaultReason = "", placeholder = "写一句理由（会和操作一起存档）", primary = true,
                       extra = null, validate = null, key = null, typeToConfirm = null }) {
    const receiptKey = key || `${action}:${label}`;
    const reason = h("textarea", { class: "adm-reason", rows: "2", maxlength: "300",
                                   "aria-label": `${label}的理由`, placeholder });
    reason.value = defaultReason;
    const out = h("div", { dataset: { role: "receipt-slot", receiptKey } });
    const go = btn(label, null, danger ? "danger" : primary ? "primary" : "");
    go.addEventListener("click", async () => {
      const why = (reason.value || "").trim();
      swap(out, );
      if (!why) { put(out, failureNode({ display_message: "请先写一句理由" }, label)); reason.focus(); return; }
      if (validate) { const problem = validate(); if (problem) { put(out, failureNode({ display_message: problem }, label)); return; } }
      let body;
      try { body = JSON.parse(JSON.stringify(typeof payload === "function" ? payload() : payload)); }
      catch (e) { put(out, failureNode(e, label)); return; }
      if (danger || confirmText) {
        const yes = await confirmAction({ title: confirmTitle || `确认「${label}」？`,
          consequence: (typeof confirmText === "function" ? confirmText(body) : confirmText) || `将执行「${label}」。`, danger, typeToConfirm: typeToConfirm || (danger ? envName() : null) });
        if (!yes) { put(out, h("span", { class: "adm-status" }, "已取消，没有执行。")); return; }
      }
      go.disabled = true; put(out, h("span", { class: "adm-status" }, "正在提交…"));
      try {
        const result = await adminAction(action, why, body);
        swap(out, receiptNode(result));
        state.receipts.unshift(result.receipt); state.receipts = state.receipts.slice(0, 30);
        state.flash[state.tab] = { key: receiptKey, result };
        renderReceiptLog();
        if (after) await after(result);
      } catch (e) { swap(out, failureNode(e, label)); }
      finally { go.disabled = false; }
    });
    const form = h("div", { class: `adm-form${danger ? " adm-danger-form" : ""}` }, extra,
      h("label", { class: "adm-field adm-reason-field" }, h("span", null, "理由（必填）"), reason), go);
    return h("div", { class: danger ? "adm-danger-slot" : null, dataset: { role: "write" } }, form, out);
  }
  function renderReceiptLog() {
    const box = state.root && state.root.querySelector("[data-role=receipt-log]");
    if (!box) return;
    swap(box, state.receipts.length
      ? h("ul", null, state.receipts.map(r => h("li", null, `${when(r.at)} · ${txt(r.action_label)} · 理由：${txt(r.reason)}`)))
      : empty("本次打开页面后还没有操作。"));
  }

  // ---------- commands: technical detail, collapsed, never executable --------------
  /* Commands that withdraw research, revoke, restart or deploy change the
     running system whether or not the server flagged them. */
  const DANGEROUS_COMMAND = /\bwithdraw\b|--revoke|--apply|kickstart|launchctl\s+(kickstart|bootout|unload|stop)|deploy\.sh|authorize-/;
  function commandDanger(c) { return !!(c && (c.danger || DANGEROUS_COMMAND.test(c.apply || "") || DANGEROUS_COMMAND.test(c.preview || ""))); }
  function commandItem(c) {
    const danger = commandDanger(c);
    const box = h("div", { class: `adm-cmd${danger ? " danger" : ""}` });
    if (c.label) put(box, h("div", null, danger ? h("b", { class: "adm-status err" }, "会改变运行状态 · ") : null, txt(c.label)));
    for (const [kind, text] of [["只读预览", c.preview], ["会改变运行状态", c.apply]]) {
      if (!text) continue;
      const pre = h("pre", null, text);
      const status = h("span", { class: "adm-status", role: "status" });
      const copy = btn("复制命令", async () => {
        try {
          if (!navigator.clipboard) throw new Error("浏览器不允许复制");
          await navigator.clipboard.writeText(text);
          status.className = "adm-status ok"; status.textContent = "已复制";
        } catch (e) {
          status.className = "adm-status err";
          status.textContent = "复制失败：请手动选中上面的命令复制";
        }
      });
      put(box, h("div", { class: "adm-meta" }, kind), pre, h("div", null, copy, " ", status));
    }
    return box;
  }
  /* Every shell command on the page lives here (MUSE P0 #8): under a collapsed
     「技术详情（命令）」 with a 「显示命令」 toggle; a dangerous one is shown only
     after a danger confirmation.  The page copies, it never runs anything. */
  function commandsBlock(list, { title = null } = {}) {
    const commands = (list || []).filter(c => c && (c.preview || c.apply));
    if (!commands.length) return null;
    const danger = commands.some(commandDanger);
    const body = h("div", { class: "adm-cmd-body", dataset: { role: "command-body" } }, commands.map(commandItem));
    body.hidden = true;
    const toggle = btn("显示命令", null, "adm-cmd-toggle");
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", async () => {
      if (body.hidden) {
        if (danger && !state.commandsShown) {
          const yes = await confirmAction({ title: "显示会改变运行状态的命令",
            consequence: "这些命令在终端执行后会重启服务、部署新版本或撤回研究；它们只供技术管理员在运行 Dalton 的 Mac 上使用，页面不会执行。",
            danger: true, typeToConfirm: envName(), confirmLabel: "显示命令" });
          if (!yes) return;
          state.commandsShown = true;
        }
        body.hidden = false; toggle.textContent = "隐藏命令"; toggle.setAttribute("aria-expanded", "true");
      } else { body.hidden = true; toggle.textContent = "显示命令"; toggle.setAttribute("aria-expanded", "false"); }
    });
    return h("details", { class: "adm-cmds", dataset: { role: "commands" } },
      h("summary", null, `${title || "技术详情（命令）"} · ${commands.length} 条${danger ? "，含会改变运行状态的命令" : ""}`),
      h("p", { class: "adm-meta" }, "仅供技术管理员在运行 Dalton 的 Mac 终端里执行；页面本身不执行任何命令。"),
      toggle, body);
  }

  /* 「现在有什么问题 → 影响什么 → 建议动作」 first, then the controls (review §10,
     第三批).  ``items``: {problem, impact, action, level}. */
  function issuesCard(items, okLine) {
    const list = (items || []).filter(Boolean);
    return h("section", { class: `adm-sec adm-issues${list.length ? "" : " ok"}`, dataset: { role: "issues" } },
      h("h2", null, list.length ? `现在有什么问题（${list.length}）` : "现在有什么问题"),
      list.length ? h("ol", { class: "adm-issue-list" }, list.map(i => h("li", { class: `adm-issue ${i.level || "warn"}` },
        h("div", { class: "adm-issue-row" }, h("span", { class: "adm-issue-k" }, "问题"), h("span", null, plain(i.problem))),
        h("div", { class: "adm-issue-row" }, h("span", { class: "adm-issue-k" }, "影响"), h("span", null, plain(i.impact))),
        h("div", { class: "adm-issue-row" }, h("span", { class: "adm-issue-k" }, "建议动作"), h("span", null, plain(i.action))))))
        : h("p", { class: "adm-ok-line" }, okLine || "目前没有需要处理的问题。下面是这一分区的设置。"));
  }

  // ---------- page frame ---------------------------------------------------------------
  function mount(root) {
    state.root = root;
    root.classList.add("adm-root");
    root.dataset.adminVersion = ADMIN_VERSION;
    const header = h("div", { class: "adm-head", "data-role": "admin-head" }, "正在读取环境状态…");
    const tabs = h("div", { class: "adm-tabs", role: "tablist", "aria-label": "管理员设置分区" });
    const panels = h("div", { "data-role": "panels" });
    TABS.forEach(([id, label]) => {
      const b = h("button", { type: "button", role: "tab", id: `adm-tab-${id}`, "aria-controls": `adm-panel-${id}`,
                              "aria-selected": "false", tabindex: "-1", dataset: { tab: id } }, label);
      b.addEventListener("click", () => select(id, true));
      b.addEventListener("keydown", ev => {
        const i = TABS.findIndex(t => t[0] === id);
        if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
          ev.preventDefault();
          const next = TABS[(i + (ev.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length][0];
          select(next, true); root.querySelector(`#adm-tab-${next}`).focus();
        }
      });
      put(tabs, b);
      put(panels, h("div", { role: "tabpanel", id: `adm-panel-${id}`, "aria-labelledby": `adm-tab-${id}`, hidden: true }));
    });
    const log = section("本次操作回执", "这里列出本次打开页面以来所做的管理员操作；完整记录在投研工作台的操作记录里。",
      h("div", { "data-role": "receipt-log" }));
    swap(root, header, tabs, panels, log);
    renderReceiptLog();
    loadHeader();
    const initial = (location.hash || "").replace(/^#(admin\/)?/, "");
    select(TABS.some(t => t[0] === initial) ? initial : "ai", false);
    const follow = () => {
      const id = (location.hash || "").replace(/^#(admin\/)?/, "");
      if (TABS.some(t => t[0] === id) && id !== state.tab) select(id, false);
    };
    window.addEventListener("hashchange", follow);
    window.addEventListener("popstate", follow);
    return root;
  }
  function select(id, push) {
    state.tab = id;
    state.root.querySelectorAll("[role=tab]").forEach(b => {
      const on = b.dataset.tab === id; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
    });
    state.root.querySelectorAll("[role=tabpanel]").forEach(p => { p.hidden = p.id !== `adm-panel-${id}`; });
    /* M13: a real history entry per section, so 「后退」 steps back through
       them; a section read more than five minutes ago is read again. */
    if (push && !window.DaltonV2) {
      if (typeof history.pushState === "function") history.pushState(null, "", `#${id}`);
      else history.replaceState(null, "", `#${id}`);
    }
    const stale = state.readAt[id] && Date.now() - state.readAt[id] > 5 * 60 * 1000;
    if (!state.loaded[id] || stale) { state.loaded[id] = true; loadTab(id); }
  }
  function panel(id) { return state.root.querySelector(`#adm-panel-${id}`); }
  async function loadTab(id) {
    const p = panel(id);
    swap(p, empty("正在读取…"));
    try {
      await LOADERS[id](p);
      state.readAt[id] = Date.now();
      p.prepend(h("div", { class: "adm-readat", dataset: { role: "read-at" } },
        `读取于 ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`, " ",
        btn("重新读取", () => reload(id))));
      /* A write that reloads its section keeps its receipt where the write
         was (MUSE P0 #9: the receipt in place); a form that no longer exists
         puts it at the top. */
      const flash = state.flash[id];
      if (flash) {
        delete state.flash[id];
        const slot = Array.from(p.querySelectorAll("[data-role=receipt-slot]")).find(x => x.dataset.receiptKey === flash.key);
        if (slot) put(slot, receiptNode(flash.result));
        else p.prepend(h("div", { "data-role": "flash-receipt" }, receiptNode(flash.result)));
      }
    }
    catch (e) { swap(p, failureNode(e, "读取"), btn("重新读取", () => loadTab(id))); }
  }
  function reload(id) { state.loaded[id] = true; return loadTab(id); }

  async function shellBackLink() {
    const back = document.getElementById("adm-back");
    if (!back || window.DaltonV2) return;
    try {
      const r = await fetch("/v2/data/manifest", { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (r.ok) { back.setAttribute("href", (globalThis.DaltonPublicURL || (x => x))("/#/settings")); back.textContent = "← 返回研究设置"; }
    } catch (_e) { /* the old workbench stays the way back */ }
  }
  async function loadHeader() {
    shellBackLink();
    const head = state.root.querySelector("[data-role=admin-head]");
    try {
      const s = await getJson("/v2/data/admin/summary");
      const env = s.environment || {}, w = s.writer || {}, lv = s.levers || {};
      state.env = env;
      swap(head, 
        h("span", null, "研究项目：", h("b", null, txt(env.name))), tech("项目标识", env.slug || env.mode),
        h("span", null, "调度：", pill(w.tick_stale ? "err" : w.heartbeat_present ? "ok" : "muted", txt(w.state_label))),
        w.last_tick_at ? h("span", { class: "adm-meta" }, `最近一次调度 ${when(w.last_tick_at)}`) : null,
        lv.emergency ? pill("err", "紧急停机中") : lv.paused_count ? pill("warn", `已暂停 ${lv.paused_count} 项`) : pill("ok", "全部在运行"));
    } catch (e) { swap(head, failureNode(e, "读取环境状态")); }
  }

  // ---------- AI 服务 ---------------------------------------------------------------
  function chainEditor({ initial, choices, describe, ariaLabel }) {
    /* One ordered list: ↑ ↓ 移除 per item, one searchable "添加备用服务"
       select below (review X01/X02: no shuttle box, no drag requirement). */
    const picked = [...initial];
    const list = h("ol", { class: "adm-chain", "aria-label": ariaLabel });
    const render = () => {
      swap(list, ...picked.map((id, i) => {
        const d = describe(id);
        const up = btn("↑", () => { [picked[i - 1], picked[i]] = [picked[i], picked[i - 1]]; render(); });
        const down = btn("↓", () => { [picked[i + 1], picked[i]] = [picked[i], picked[i + 1]]; render(); });
        const rm = btn("移除", () => { picked.splice(i, 1); render(); });
        up.disabled = i === 0; down.disabled = i === picked.length - 1;
        up.setAttribute("aria-label", `上移 ${d.name}`); down.setAttribute("aria-label", `下移 ${d.name}`);
        rm.setAttribute("aria-label", `移除 ${d.name}`);
        return h("li", { class: d.warn ? "warn" : null }, h("span", { class: "name" }, `${i + 1}. ${d.name}`),
          d.badge ? pill(d.warn ? "warn" : "muted", d.badge) : null, up, down, rm);
      }));
      if (!picked.length) put(list, h("li", null, "顺序为空：保存前至少保留一个服务"));
      dirty.textContent = JSON.stringify(picked) === JSON.stringify(initial) ? "" : "有未保存的修改";
    };
    const dirty = h("span", { class: "adm-status err" });
    const select = h("select", { "aria-label": `${ariaLabel}：添加备用服务` },
      h("option", { value: "" }, "选择要添加的备用服务…"),
      choices.map(c => h("option", { value: c.id, disabled: c.disabled || null }, c.label)));
    const add = btn("添加备用服务", () => { const v = select.value; if (v && !picked.includes(v)) { picked.push(v); render(); } });
    render();
    return { node: h("div", null, list, h("div", { class: "adm-form" }, select, add, dirty)), picked,
             set: next => { picked.splice(0, picked.length, ...next); render(); },
             isDirty: () => JSON.stringify(picked) !== JSON.stringify(initial) };
  }
  function modelChoiceName(models, choice) {
    const name = choice.display_name || choice.model;
    const duplicate = (models.choices || []).filter(c => (c.display_name || c.model) === name).length > 1;
    const parts = [name];
    if (duplicate && choice.broker_model_ref) parts.push(`接入通道：${choice.broker_model_ref}`);
    if (choice.broker_thinking_preset != null) parts.push(`网关预设：${thinkingLabel(choice.broker_thinking_preset)}（环节设置优先）`);
    const sameVariant = (models.choices || []).filter(c => (c.display_name || c.model) === name
      && (c.broker_model_ref || "") === (choice.broker_model_ref || "")
      && (c.broker_thinking_preset ?? null) === (choice.broker_thinking_preset ?? null)).length > 1;
    if (duplicate && (choice.broker_thinking_preset == null || sameVariant)) {
      const identity = choice.profile_hash || choice.profile_version_ref;
      if (identity) parts.push(`配置标识：${String(identity).slice(-12)}`);
    }
    return parts.join(" · ");
  }
  function modelChoiceIndex(models) {
    const info = new Map();
    (models.choices || []).forEach(c => info.set(c.model, { ...c, display_name: modelChoiceName(models, c) }));
    return info;
  }
  function describeModel(models, info) {
    return id => {
      const c = info.get(id) || {};
      const cool = (models.cooldowns || {})[id];
      return { name: c.display_name || id, badge: cool ? (cool.badge || "冷却中") : null, warn: !!cool };
    };
  }
  function modelOptions(models, { verifierOnly = false } = {}) {
    return (models.choices || []).map(c => ({
      id: c.model, disabled: verifierOnly && !c.verifier_eligible,
      label: `${modelChoiceName(models, c)}${c.family_label ? " · " + c.family_label : ""}${verifierOnly ? (c.verifier_eligible ? " · 可独立核验" : " · 不可核验") : ""}${c.cooldown ? " · " + (c.cooldown.badge || "冷却中") : ""}${c.note ? "（" + c.note + "）" : ""}`,
    }));
  }
  function hostVersion(models) {
    return models.host_settings ? { expected_host_settings_hash: models.host_settings.desired_hash } : {};
  }
  function tierCard(models, card) {
    const info = modelChoiceIndex(models);
    (card.chain || []).forEach(l => { if (!info.has(l.model)) info.set(l.model, { display_name: l.display_name }); });
    const editor = chainEditor({ initial: (card.chain || []).map(l => l.model), choices: modelOptions(models, { verifierOnly: card.tier === "verifier" }),
                                 describe: describeModel(models, info), ariaLabel: `${card.label}的 AI 服务选择顺序` });
    const cov = card.capability_coverage || {};
    const separate = (models.purposes || []).filter(p => p.tier === card.tier && p.mode === "explicit" && !p.retired_by);
    const following = (models.purposes || []).filter(p => p.tier === card.tier && p.mode === "tier" && !p.retired_by && !p.configuration_error);
    return h("div", { class: "adm-row", dataset: { tier: card.tier } }, h("div", { class: "grow" },
      h("h3", null, card.label, " ", cov.status === "missing" ? pill("err", "能力缺口") : cov.status === "partial" ? pill("warn", "部分覆盖") : pill("ok", "可用")),
      h("div", { class: "adm-meta" }, `本档位的共用默认顺序${card.requires_restart ? " · 含常驻服务固定的环节，保存后需重启才完全生效" : ""} · 范围：本机全部研究项目共用`),
      h("div", { class: "adm-meta" }, following.length ? `当前使用这套默认顺序：${following.map(p => p.label).join("、")}。` : "当前没有环节使用这套默认顺序。"),
      separate.length ? h("div", { class: "adm-meta" }, `单独指定、不随档位默认更新：${separate.map(p => p.label).join("、")}。要重新跟随默认，请在「各环节设置」中选择对应环节，再使用档位默认顺序。`) : null,
      cov.note ? h("div", { class: "adm-meta" }, cov.note) : null,
      card.cooldown_note ? h("div", { class: "adm-meta" }, card.cooldown_note) : null,
      card.chain_variants_note ? h("div", { class: "adm-meta" }, card.chain_variants_note) : null,
      tech("档位标识", card.tier),
      editor.node,
      writeForm({ action: "model_select", label: `保存「${card.label}」的选择顺序（共用默认）`, danger: true,
        payload: () => ({ tier: card.tier, mode: "explicit", chain: [...editor.picked], ...hostVersion(models) }),
        validate: () => (editor.picked.length ? null : "顺序至少需要一个服务"),
        confirmTitle: `保存「${card.label}」的共用默认顺序？`,
        confirmText: body => `新顺序：${body.chain.map(id => describeModel(models, info)(id).name).join(" → ") || "（空）"}。这会更新本机全部研究项目中按该档位选择的环节，保留单独指定的环节${card.requires_restart ? "；部分环节需重启后才生效" : ""}。各项目结果见回执。`,
        after: () => reload("ai") }),
      writeForm({ action: "model_select", label: "恢复档位的系统推荐顺序（共用默认）", primary: false, danger: true,
        payload: { tier: card.tier, mode: "tier", ...hostVersion(models) },
        confirmText: `「${card.label}」的共用默认将恢复为系统推荐顺序，单独指定的环节保持不变。保存会同步到全部研究项目，本卡未保存的修改会丢弃。`, after: () => reload("ai") })));
  }
  function thinkingLabel(level) {
    return ({ off: "关闭", minimal: "最低强度", low: "低强度", medium: "中等强度", high: "高强度" })[level] || txt(level);
  }
  function stageRows(models) {
    const rows = (models.purposes || []).map(p => {
      const chain = (p.chain || []).map(l => l.display_name || l.model).join(" → ");
      const cb = p.call_budget || {}, eff = cb.effective || {};
      return [h("div", null, h("div", null, p.label), tech("环节标识", p.purpose)),
        txt(p.tier_label), txt(p.mode_label), chain || "—", thinkingLabel(p.thinking_level || "low"),
        p.last_served ? `${txt(p.last_served.display_name)} · ${when(p.last_served.at)}` : txt(p.last_served_note),
        eff.max_output_tokens != null ? count(eff.max_output_tokens) : "—",
        [p.requires_restart ? "修改需重启" : null, p.cooldown_note, p.output_contract_hold_note,
          p.last_output_contract_skip_note, p.last_served?.selection_changed_since_call ? "上次调用使用的是当时保存的配置" : null, p.pin_note].filter(Boolean).join("；") || "—"];
    });
    return table(["环节", "档位", "选择方式", "当前顺序", "思考强度", "上次实际使用", ["单次回答长度上限（token）", true], "说明"], rows);
  }
  function stageEditor(models) {
    const purposes = (models.purposes || []).filter(p => p.editable !== false && !p.retired_by);
    if (!purposes.length) return { node: empty("没有可单独调整的环节。"), canEdit: () => false, open: () => false };
    const info = modelChoiceIndex(models);
    const pick = h("select", { "aria-label": "选择要单独调整的环节" },
      purposes.map(p => h("option", { value: p.purpose }, `${p.label}（${p.tier_label || ""}）`)));
    pick.value = purposes.some(p => p.purpose === state.aiPurpose) ? state.aiPurpose : purposes[0].purpose;
    const holder = h("div");
    const render = (proposal = null) => {
      const p = purposes.find(x => x.purpose === pick.value) || purposes[0];
      state.aiPurpose = p.purpose;
      const editor = chainEditor({ initial: (p.chain || []).map(l => l.model), choices: modelOptions(models, { verifierOnly: p.tier === "verifier" }),
                                   describe: describeModel(models, info), ariaLabel: `${p.label}的 AI 服务顺序` });
      if (proposal && Array.isArray(proposal.chain)) editor.set(proposal.chain);
      const cb = p.call_budget || {}, eff = cb.effective || {};
      const sharedBudget = cb.shared_limits || cb.shared_call_cost;
      const budgetScope = sharedBudget ? "全部研究项目" : "仅当前研究项目";
      const out = h("input", { type: "number", min: "256", step: "256", value: eff.max_output_tokens ?? "",
                               "aria-label": `${p.label}的单次输出上限` });
      const thinking = h("select", { "aria-label": `${p.label}的思考强度` },
        (p.thinking_level_choices || ["low"]).map(level => h("option", {
          value: level, selected: level === (p.thinking_level || "low") || null }, thinkingLabel(level))));
      thinking.value = p.thinking_level || "low";
      swap(holder, h("div", { class: "adm-meta" }, `当前：${p.mode_label || ""}${p.requires_restart ? " · 修改需重启" : ""}`),
        h("div", null, h("h3", null, "思考强度"),
          h("p", { class: "adm-explain" }, p.tier === "verifier"
            ? "独立核验固定使用低强度。"
            : `当前使用${thinkingLabel(p.thinking_level || "low")}。调整会同步到全部研究项目，请填写理由。`),
          p.thinking_override ? h("p", { class: "adm-meta" }, `上次理由：${p.thinking_override.reason}`) : null,
          writeForm({ action: "model_select", label: "保存思考强度（全部研究项目）", primary: false,
            extra: h("label", { class: "adm-field" }, h("span", null, "思考强度"), thinking),
            payload: () => ({ purpose: p.purpose, thinking_level: thinking.value, ...hostVersion(models) }), after: () => reload("ai") })),
        editor.node,
        writeForm({ action: "model_select", label: `为「${p.label}」单独指定顺序`,
          key: `model_select:purpose:${p.purpose}`, defaultReason: proposal && proposal.reason || "",
          confirmText: body => `「${p.label}」的新顺序：${body.chain.map(id => describeModel(models, info)(id).name).join(" → ")}。保存会同步到全部研究项目。`,
          payload: () => ({ purpose: p.purpose, mode: "explicit", chain: [...editor.picked], ...hostVersion(models) }),
          validate: () => (editor.picked.length ? null : "顺序至少需要一个服务"), after: () => reload("ai") }),
        writeForm({ action: "model_select", label: "使用档位默认顺序", primary: false,
          payload: { purpose: p.purpose, mode: "tier", ...hostVersion(models) }, after: () => reload("ai") }),
        cb.editable && eff.max_output_tokens != null ? h("div", null, h("h3", null, `高级：单次回答长度上限（${budgetScope}）`),
          h("p", { class: "adm-explain" }, `${sharedBudget ? "保存会同步全部研究项目与新建项目。下一批任务采用，运行中批次完成后切换。" : "这项长度设置仅修改当前研究项目。"}当前每次调用最多输出 ${count(eff.max_output_tokens)} 个 token（输入最多 ${count(eff.max_input_tokens)} 个 token，超过 ${txt(eff.timeout_seconds)} 秒未完成即放弃）。推理模型的思考也计入输出上限，上限太小时可能只思考、不给出正文。`),
          writeForm({ action: "model_budget", label: `保存回答长度上限（${budgetScope}）`, primary: false, extra: h("label", { class: "adm-field" }, h("span", null, "单次回答长度上限（token，正整数）"), out),
            validate: () => (Number.isInteger(Number(out.value)) && Number(out.value) > 0 ? null : "请输入正整数"),
            payload: () => ({ purpose: p.purpose, kind: "call",
                              budget: sharedBudget ? { max_output_tokens: Number.parseInt(out.value, 10) }
                                : { ...(cb.overrides || {}), max_output_tokens: Number.parseInt(out.value, 10) },
                              ...(sharedBudget ? { expected_shared_policy_hash: sharedBudget.policy_hash }
                                : { expected_config_hash: cb.config_hash }) }), after: () => reload("ai") }))
          : (cb.reason ? h("p", { class: "adm-meta" }, cb.reason) : null));
    };
    pick.addEventListener("change", () => render()); render();
    const node = h("div", { dataset: { role: "purpose-editor" } }, h("label", { class: "adm-field" }, h("span", null, "环节"), pick), holder);
    return { node, canEdit: purpose => purposes.some(p => p.purpose === purpose), open: (purpose, proposal = null) => {
      if (!purposes.some(p => p.purpose === purpose)) return false;
      pick.value = purpose; render(proposal); pick.focus(); return true;
    } };
  }
  function cooldownList(models) {
    const entries = Object.entries(models.cooldowns || {});
    if (!entries.length) return empty("目前没有处于冷却期的 AI 服务。");
    const info = modelChoiceIndex(models);
    return table(["服务", "状态", "说明"], entries.map(([id, c]) => [
      h("div", null, (info.get(id) || {}).display_name || id, h("br"), tech("服务标识", id)),
      pill("warn", c.badge || "冷却中"), txt(c.note)]));
  }
  const VERIFY_STATUS = { independent: "ok", unbound: "muted", unavailable: "muted" };
  function verifierCard(v, openStage) {
    if (!v || !v.available) return empty((v && v.reason) || "暂时读不到核验独立性。");
    const box = h("div");
    put(box, h("div", { class: "adm-grid" }, (v.environments || []).map(e =>
      h("div", { class: `adm-cell${e.conflicts ? " alarm" : ""}` }, h("div", { class: "n" }, e.available ? String(e.conflicts || 0) : "—"),
        h("div", { class: "l" }, `${txt(e.name)}：${e.available ? (e.conflicts ? "个核验环节冲突" : "未发现配置冲突") : txt(e.reason, "读不到")}`)))));
    const bound = (v.rows || []).filter(r => r.bound_anywhere), idle = (v.rows || []).filter(r => !r.bound_anywhere);
    bound.forEach(row => {
      const card = h("div", { class: "adm-row", dataset: { purpose: row.draft_purpose } });
      const pairStates = (row.pairs || []).flatMap(pair => pair.environments || []);
      const allIndependent = pairStates.length > 0 && pairStates.every(e => e.status === "independent");
      const body = h("div", { class: "grow" }, h("h3", null, `${row.draft_label}（${row.draft_tier_label}）`, " ",
        row.in_conflict ? pill("err", "核验配置有问题") : allIndependent ? pill("ok", "独立") : pill("warn", "待核对")), tech("起草环节标识", row.draft_purpose));
      if (row.refusals_48h) put(body, h("div", { class: "adm-status err" }, `近 48 小时核验被拒 ${row.refusals_48h} 次`));
      (row.pairs || []).forEach(pair => {
        put(body, h("div", { class: "adm-meta" }, `由「${pair.verifier_label}」核验`));
        put(body, table(["环境", "起草顺序", "核验顺序", "状态"], (pair.environments || []).map(e => [
          txt(e.name), (e.draft_chain || []).join(" → ") || "—", (e.verifier_chain || []).join(" → ") || "—",
          h("span", null, pill(VERIFY_STATUS[e.status] || "err", txt(e.status_label)),
            (e.fallback_conflicts || []).length ? ` 回退到 ${e.fallback_conflicts.join("、")} 时会冲突` : "")])));
      });
      const ed = row.editor;
      if (ed) {
        const edit = btn(`修改「${row.draft_label}」的顺序`, () => openStage(row.draft_purpose));
        edit.disabled = !openStage.canEdit(row.draft_purpose);
        put(body, edit);
        if (edit.disabled) put(body, h("div", { class: "adm-meta" }, openStage.uneditableReason(row.draft_purpose)));
        const rec = ed.recommended || {};
        if (rec.status === "proposed") put(body,
          h("div", { class: "adm-meta" }, `推荐顺序：${(rec.names || []).join(" → ")}（${txt(rec.note, "")}）`),
          h("button", { type: "button", class: "adm-btn", disabled: !openStage.canEdit(row.draft_purpose),
            onclick: () => openStage(row.draft_purpose, { chain: rec.chain || [], reason: ed.default_reason || "" }) },
            "在统一编辑入口填入推荐顺序（不会保存）"));
      } else if (row.editor_note) put(body, h("div", { class: "adm-meta" }, row.editor_note));
      (row.pairs || []).forEach(pair => {
        const edit = btn(`修改核验顺序：${pair.verifier_label}`, () => openStage(pair.verifier_purpose));
        edit.disabled = !openStage.canEdit(pair.verifier_purpose);
        put(body, edit);
        if (edit.disabled) put(body, h("div", { class: "adm-meta" }, openStage.uneditableReason(pair.verifier_purpose)));
      });
      put(card, body); put(box, card);
    });
    if (idle.length) put(box, h("details", { class: "adm-more" }, h("summary", null, `其余 ${idle.length} 个核验环节在任何环境都还没有配置`),
      h("ul", null, idle.map(r => h("li", null, `${r.draft_label} → ${(r.pairs || []).map(p => p.verifier_label).join("、")}`)))));
    return box;
  }
  function catalogSection(models) {
    const cat = models.catalog || {};
    const box = h("div");
    if (!cat.available) { put(box, empty(cat.reason || "模型网关目录暂时读不到。")); return box; }
    put(box, h("p", { class: "adm-explain" }, cat.in_sync ? "本地 AI 服务配置与模型网关一致。" : "本地 AI 服务配置与模型网关存在差异，明细如下。"));
    if (cat.sync_note) put(box, h("p", { class: "adm-meta" }, cat.sync_note));
    const col = (title, names, note, allow) => {
      put(box, h("h3", null, `${title}（${(names || []).length}）`), note ? h("p", { class: "adm-meta" }, note) : null);
      if (!(names || []).length) { put(box, empty("没有")); return; }
      names.forEach(name => put(box, h("div", { class: "adm-row" }, h("div", { class: "grow" }, h("span", { class: "adm-model-ref" }, name)),
        allow ? writeForm({ action: "model_allow", label: "允许使用", primary: false, danger: true, payload: { model_ref: name },
          confirmTitle: `允许 Dalton 使用「${name}」？`,
          confirmText: "这会修改模型网关的授权，对本机所有研究项目生效，可能产生新的费用；重启模型网关后生效。", after: () => reload("ai") }) : null)));
    };
    col(cat.follow_provider_catalog ? "网关新增、等待自动同步" : "网关已提供、尚未获授权", cat.in_openclaw_not_allowed, cat.in_openclaw_not_allowed_note, !cat.follow_provider_catalog);
    col("已获授权、本机尚未登记", cat.allowed_not_in_dalton, cat.allowed_not_in_dalton_note, false);
    col("本机仍有登记、网关已下线", cat.dalton_not_in_openclaw, cat.dalton_not_in_openclaw_note, false);
    return box;
  }
  function metadataSection(models) {
    const choices = models.choices || [];
    if (!choices.length) return empty("还没有可用的 AI 服务。");
    const pick = h("select", { "aria-label": "要补充信息的 AI 服务" }, choices.map(c => h("option", { value: c.model }, modelChoiceName(models, c))));
    const family = h("input", { type: "text", "aria-label": "模型家族", maxlength: "128" });
    const caps = h("input", { type: "text", "aria-label": "能力（逗号分隔）", maxlength: "400" });
    const fill = () => { const c = choices.find(x => x.model === pick.value) || {};
      family.value = c.family && !String(c.family).startsWith("unclassified:") ? c.family : ""; caps.value = (c.capabilities || []).join(","); };
    pick.addEventListener("change", fill); fill();
    return h("div", null, h("p", { class: "adm-explain" }, "模型家族用于判断核验是否独立：只填写已确认的公开家族名。保存后通过写入服务记录，并在目录同步后生效。"),
      writeForm({ action: "model_metadata", label: "保存 AI 服务信息", primary: false,
        extra: [h("label", { class: "adm-field" }, h("span", null, "服务"), pick), h("label", { class: "adm-field" }, h("span", null, "模型家族"), family),
                h("label", { class: "adm-field" }, h("span", null, "能力（逗号分隔）"), caps)],
        validate: () => (family.value.trim() && caps.value.trim() ? null : "请填写模型家族和至少一项能力"),
        payload: () => { const c = choices.find(x => x.model === pick.value) || {};
          return { profile_id: c.model, profile_version_ref: c.profile_version_ref, profile_hash: c.profile_hash, family: family.value.trim(),
                   capabilities: caps.value.split(",").map(x => x.trim()).filter(Boolean) }; },
        after: () => reload("ai") }));
  }
  function aiIssues(models, verifier, notices) {
    const out = [];
    const cooling = Object.keys(models.cooldowns || {});
    if (cooling.length) {
      const info = modelChoiceIndex(models);
      out.push({ problem: `${cooling.length} 个 AI 服务连续失败，正在冷却：${cooling.map(id => (info.get(id) || {}).display_name || "未命名服务").slice(0, 4).join("、")}`,
        impact: "用到它们的环节会自动改用顺序里的下一个服务，可能更贵、更慢；顺序里没有备用服务的环节会暂停。",
        action: "看下方「冷却中的 AI 服务」；冷却持续时，在对应档位「添加备用服务」后保存。" });
    }
    (models.tier_cards || []).forEach(card => {
      const cov = card.capability_coverage || {};
      if (cov.status === "missing") out.push({ level: "err", problem: `「${card.label}」档位缺少需要的能力`,
        impact: txt(cov.note, "依赖这项能力的环节无法完成。"), action: `在「${card.label}」添加具备该能力的服务后保存。` });
    });
    const conflicts = ((verifier && verifier.environments) || []).reduce((n, e) => n + (Number(e.conflicts) || 0), 0);
    if (conflicts) out.push({ level: "err", problem: `${conflicts} 个核验环节配置有问题`,
      impact: "独立核验不能完成，相关报告不能定稿；具体原因见下方各项目的核验对照。", action: "在「核验独立性」查看冲突，跳转到统一环节编辑入口调整后保存。" });
    if (notices.length) out.push({ level: "info", problem: `${notices.length} 条 AI 服务变动通知未读`,
      impact: "通知说明了服务上线、下线或价格变化；未读不影响运行。", action: "在「AI 服务变动通知」逐条查看后标为已读。" });
    const cat = models.catalog || {};
    if (cat.available && !cat.in_sync) out.push({ level: "info", problem: "本机 AI 服务配置与模型网关存在差异",
      impact: "网关新增或下线的服务还没有反映到选择顺序里。", action: "看「模型网关目录」，需要时「允许使用」。" });
    return out;
  }
  async function loadAi(p) {
    const [models, verifier] = await Promise.all([getJson("/v1/cockpit/models"),
      getJson("/v1/cockpit/verifier_independence").catch(e => ({ available: false, reason: errorText(e, "读取核验独立性") }))]);
    if (!models.available) {
      swap(p, issuesCard([{ level: "info", problem: "本项目还没有接入 AI 服务", impact: "需要 AI 的研究步骤不会运行。",
        action: txt(models.reason, "由技术管理员为本项目接入 AI 服务。") }]), section("AI 服务", null, empty(txt(models.reason, "本项目还没有接入 AI 服务。"))));
      return;
    }
    const notices = (models.notices || []).filter(n => !n.acknowledged);
    const hadSelectedPurpose = state.aiPurpose != null;
    const stages = stageEditor(models);
    const stageDetails = h("details", { class: "adm-more", open: hadSelectedPurpose || null },
      h("summary", null, "各环节设置（高级）"), stages.node);
    const openStage = (purpose, proposal = null) => {
      if (!stages.canEdit(purpose)) return;
      stageDetails.open = true;
      stages.open(purpose, proposal);
      if (typeof stages.node.scrollIntoView === "function") stages.node.scrollIntoView({ block: "center", behavior: "smooth" });
    };
    openStage.canEdit = stages.canEdit;
    openStage.uneditableReason = purpose => {
      const row = (models.purposes || []).find(p => p.purpose === purpose);
      return row && (row.configuration_error || row.pin_note) || "该环节在当前研究项目不能单独调整。";
    };
    swap(p, issuesCard(aiIssues(models, verifier, notices)),
      models.host_settings ? h("p", { class: "adm-explain" }, models.host_settings.note) : null,
      notices.length ? section("AI 服务变动通知", "标为已读只表示看过这条通知，不代表接受当前配置。",
        notices.map(n => h("div", { class: "adm-row" }, h("div", { class: "grow" }, n.message),
          writeForm({ action: "model_notice_ack", label: "标为已读", primary: false, defaultReason: "已查看", payload: { ref: n.ref }, after: () => reload("ai") })))) : null,
      section("各档位的 AI 服务选择顺序", "按档位选择的环节使用这套共用默认：第一个不可用或冷却中时使用下一个。单独指定的环节保留原顺序，可在「各环节设置」中查看或改为使用档位默认。保存会同步到本机全部研究项目，各项目结果见回执。",
        (models.tier_cards || []).map(card => tierCard(models, card))),
      section("各环节当前使用情况", "每个调用环节的选择方式、当前顺序和上次实际使用的服务（默认收起）。",
        h("details", { class: "adm-more" }, h("summary", null, `展开 ${(models.purposes || []).length} 个环节的使用情况`), stageRows(models)),
        stageDetails),
      section("冷却中的 AI 服务", "连续失败的服务会暂时冷却，冷却期内各顺序自动跳过它。", cooldownList(models)),
      section("核验独立性", "检查各研究项目的起草与核验服务是否独立。调整顺序请使用同一个环节编辑入口。", verifierCard(verifier, openStage)),
      section("模型网关目录", "网关提供了哪些服务、哪些已获授权。「允许使用」会修改网关授权，重启网关后生效。", catalogSection(models)),
      section("补充 AI 服务信息", null, metadataSection(models)));
  }

  // ---------- 数据连接 -----------------------------------------------------------------
  const CONNECT_KIND = { connected: "ok", login_expired: "err" };
  function sourcesSection(r) {
    const rows = (r.sources || []).map(s => {
      const c = s.connector || {}, m = s.mission || {};
      const expired = c.status === "login_expired" || (s.health && s.health.status === "login_expired");
      const ready = (c.status === "connected" || s.status === "connected") && !expired && !["unconfigured", "ungranted", "unapproved"].includes(m.status);
      return [h("div", null, s.label, s.generic ? "（通用）" : "", h("br"), tech("来源标识", s.source_ref || s.slug)),
        pill(expired ? "err" : ready ? "ok" : "warn", expired ? "登录已过期" : txt(c.status_label || s.status_label, "未连接")),
        txt(m.status_label, "—"),
        (s.quotas || []).map(q => `${txt(q.operation_label || q.operation)} 每天最多 ${q.daily_limit} 次`).join("；") || "没有登记额度",
        txt(s.cadence_label, "尚未设置更新频率"),
        txt(s.health_note || s.note, "")];
    });
    return h("div", null, r.policy_note ? h("p", { class: "adm-meta" }, r.policy_note) : null,
      rows.length ? table(["数据源", "连接", "本项目权限", "额度", "更新频率", "说明"], rows) : empty("没有登记的数据源。"));
  }
  function governanceSection(g) {
    if (!g || g.available === false) return empty((g && g.reason) || "读不到接口治理记录。");
    if (!(g.items || []).length) return empty(g.note || "没有治理记录。");
    return h("div", null, h("p", { class: "adm-explain" }, g.note),
      (g.items || []).map(it => h("div", { class: "adm-row" }, h("div", { class: "grow" },
        h("div", null, txt(it.capability_id || it.id, it.file), " ", pill(it.approved ? "ok" : "warn", it.status_label)),
        h("div", { class: "adm-meta" }, `风险等级 ${txt(it.risk_class)} · 批准时间 ${when(it.approved_at)}`), tech("文件", it.file),
        commandsBlock(it.commands)))));
  }
  function holdsSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到连接状态。");
    if (!(x.items || []).length) return empty("没有因登录过期而暂停过的数据源。");
    return h("div", null, h("p", { class: "adm-explain" }, x.note), table(["数据源", "状态", "开始", "冷却到", "受阻检索/下载", "服务方提示"],
      x.items.map(i => [h("div", null, txt(i.label, "数据源"), tech("来源", i.source_ref)), pill(i.held ? "err" : "ok", txt(i.status_label)), when(i.since), i.held ? when(i.hold_until) : `已恢复 ${when(i.recovered_at)}`,
        `${txt(i.blocked_searches, "0")} / ${txt(i.blocked_acquisitions, "0")}`, txt(i.provider_message)])));
  }
  function credentialSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "本环境没有模型配置。");
    return h("div", null, h("p", { class: "adm-explain" }, x.note), (x.slots || []).length
      ? table(["使用这个凭证的 AI 服务", "本项目已声明配置", "标识"], x.slots.map(s => [(s.profiles || []).map(txt).join("、") || "（暂无服务使用）",
          pill(s.declared ? "ok" : "err", s.declared ? "已声明" : "未声明"), tech("凭证槽", s.credential_slot_ref)]))
      : empty("没有需要的凭证。"));
  }
  function plansSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到检索计划。");
    return h("div", null, h("p", { class: "adm-explain" }, x.note), (x.plans || []).map(plan => {
      if (!plan.available) return h("div", { class: "adm-row" }, h("div", { class: "grow" }, h("b", null, txt(plan.label)), h("div", { class: "adm-meta" }, txt(plan.reason))));
      const f = plan.owner_fields || {};
      const drifted = (plan.specs || []).filter(s => s.drift && s.drift !== "none").length;
      return h("div", { class: "adm-row" }, h("div", { class: "grow" },
        h("h3", null, plan.label, " ", drifted ? pill("muted", `${drifted} 项更新间隔已自定义`) : pill("ok", "使用默认间隔")),
        drifted ? h("p", { class: "adm-meta" }, "「已自定义」表示计划文件里的回看或重查间隔与系统默认不同：通常是有意调整，不是故障；只有出现「格式错误」才需要处理。") : null,
        h("div", { class: "adm-meta" }, `负责人控制：每 24 小时最多检索 ${txt((f.budget || {}).max_calls_24h)} 次 · 覆盖公司 ${count(f.company_count)} 家`),
        tech("计划", plan.plan_ref), " ", tech("文件", plan.file),
        (plan.names_unresolved || []).length ? h("div", { class: "adm-status err" }, `只有代码、没有公司名称的：${plan.names_unresolved.join("、")}`) : null,
        (plan.specs || []).length ? h("details", { class: "adm-more" }, h("summary", null, `展开 ${plan.specs.length} 项检索规格（代码维护，只读）`),
          table(["文件类型", ["回看（天）", true], ["重新检索间隔（天）", true], ["失败后重试间隔（天）", true], "与默认相比", "标识"],
          plan.specs.map(s => [txt(s.form), txt(s.lookback_days), txt(s.rediscovery_interval_days), txt(s.retry_interval_days),
            pill(s.drift === "none" ? "ok" : "muted", txt(s.drift_label)), tech("规格", s.spec_ref)]))) : empty("计划里没有规格。")));
    }));
  }
  function sourceBudgetSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到来源预算。");
    const m = x.mission_caps || {}, ae = (x.measured || {}).alphaengine, web = (x.measured || {}).web;
    const line = b => (b ? `已用 ${txt(b.spent)} / 上限 ${txt(b.cap)}` : "还没有运行记录");
    return h("div", null, h("p", { class: "adm-explain" }, x.note), table(["来源", "研究目标中的上限", "最近一轮实测"], [
      ["AlphaEngine 搜索（次 / 24 小时）", txt(m.max_alphaengine_calls_24h), line(ae)],
      ["网络检索（次）", "写在检索计划里", line(web)],
      ["阅读原文（份 / 天）", m.max_daily_document_reads == null ? "不限" : txt(m.max_daily_document_reads), "—"]]),
      x.shared_alphaengine ? h("p", { class: "adm-meta" }, `全部环境共享的 AlphaEngine 上限 ${txt(x.shared_alphaengine.cap)}，其它环境已用 ${txt(x.shared_alphaengine.spent_elsewhere)}`) : null);
  }
  function irSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到 IR 页面状态。");
    return h("div", null, h("p", { class: "adm-explain" }, x.note), table(["项目", "状态"], [
      ["声明文件", h("span", null, pill(x.present ? (x.valid === false ? "err" : "ok") : "muted", x.present ? (x.valid === false ? "格式有误" : "存在") : "缺失"), " ", tech("文件", x.file))],
      ["最近修改", when(x.modified_at)], ["声明的页面数", txt(x.page_count)],
      ["问题", txt(x.problem, "无")], ["最近一次运行", x.last_run ? `${txt(x.last_run.status)}${x.last_run.reason ? " · " + x.last_run.reason : ""}` : "没有运行记录"]]));
  }
  function feishuSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到飞书推送状态。");
    const c = x.contract || {};
    return h("div", null, h("p", { class: "adm-explain" }, x.note),
      h("div", { class: "adm-form" }, pill(x.enabled ? "ok" : "warn", x.status_label), c.recipient_set ? pill("ok", "已设置接收群聊") : pill("muted", "未设置接收群聊"),
        c.approved_at ? h("span", { class: "adm-meta" }, `批准时间 ${when(c.approved_at)}`) : null, tech("审批文件", x.contract_file)),
      c.reason ? h("p", { class: "adm-meta" }, c.reason) : null,
      Object.keys(x.counts || {}).length ? h("p", { class: "adm-meta" }, "发送回执统计：" + Object.entries(x.counts).map(([k, v]) => `${k} ${v}`).join("，")) : null,
      (x.receipts || []).length ? table(["时间", "内容", "结果"], x.receipts.map(r => [when(r.at), txt(r.artifact_label), txt(r.outcome_label)])) : empty("还没有发送回执。"),
      h("h3", null, "开通与停用"), h("p", { class: "adm-meta" }, "开通或停用飞书推送需要技术管理员在终端完成。"), commandsBlock(x.commands));
  }
  function connectionIssues(sources, conn) {
    const out = [];
    const held = (((conn.holds || {}).items) || []).filter(i => i.held);
    if (held.length) out.push({ level: "err", problem: `${held.length} 个数据源登录已过期：${held.map(i => txt(i.label, "数据源")).slice(0, 3).join("、")}`,
      impact: "依赖它们的检索和下载已暂停，相关公司的资料不会更新。", action: "在对应应用里重新登录；恢复后系统自动继续，无需在本页操作。" });
    const list = (sources && sources.sources) || [];
    const unconnected = list.filter(x => { const c = x.connector || {}; return !(c.status === "connected" || x.status === "connected"); });
    if (unconnected.length) out.push({ problem: `${unconnected.length} 个数据源尚未连接或未授权`,
      impact: "研究目标需要这些来源时，相关资料会缺失。", action: "看「数据源与连接状态」的「说明」一列：未授权的需要负责人批准，未连接的需要技术管理员接入。" });
    const pending = (((conn.governance || {}).items) || []).filter(i => !i.approved);
    if (pending.length) out.push({ problem: `${pending.length} 项数据接口等待批准`,
      impact: "批准前，这些接口不会被调用。", action: "核对「接口治理与批准」里的风险等级后，由技术管理员按技术详情批准。" });
    const missing = (((conn.credentials || {}).slots) || []).filter(x => !x.declared);
    if (missing.length) out.push({ level: "err", problem: `${missing.length} 个 AI 服务凭证没有声明`,
      impact: "使用这些凭证的 AI 服务在本项目不可用。", action: "由技术管理员在本机配置凭证；「已声明」只表示配置存在，不代表凭证有效。" });
    return out;
  }
  async function loadConnections(p) {
    const [sources, conn] = await Promise.all([
      getJson("/v1/cockpit/sources").catch(e => ({ error: e })), getJson("/v2/data/admin/connections")]);
    swap(p, issuesCard(connectionIssues(sources, conn)),
      section("数据源与连接状态", "每个数据源的真实连接状态、本项目是否启用、额度与更新频率。", sources.error ? failureNode(sources.error, "读取数据源") : sourcesSection(sources)),
      section("登录过期与冷却", null, holdsSection(conn.holds)),
      section("接口治理与批准", "数据源接口要先经负责人批准才能使用：已批准 / 已提出两类。", governanceSection(conn.governance)),
      section("AI 服务凭证", "「已声明」只表示本机配置里有这个凭证，不代表它仍然有效；有效与否看 AI 服务是否在冷却。", credentialSection(conn.credentials)),
      section("检索计划", null, plansSection(conn.discovery)),
      section("AlphaEngine / SEC / 网络检索预算", null, sourceBudgetSection(conn.source_budgets)),
      section("IR 页面监控", null, irSection(conn.ir_pages)),
      section("飞书推送", null, feishuSection(conn.feishu)));
  }

  // ---------- 运行控制 -----------------------------------------------------------------
  function hoursInput() { return h("input", { type: "number", min: "1", max: "720", step: "1", placeholder: "不填＝直到手动恢复", "aria-label": "暂停小时数（1–720 小时）" }); }
  function hoursValid(input) {
    const v = (input.value || "").trim();
    if (v === "") return null;
    const n = Number(v);
    return Number.isInteger(n) && n >= 1 && n <= 720 ? null : "暂停时长请填 1 到 720 之间的整数小时，或留空表示直到手动恢复";
  }
  function emergencyZone(r) {
    const danger = h("div", { class: "adm-danger-zone", dataset: { role: "danger-zone" } }, h("h3", null, "暂停所有研究（紧急停机）"),
      h("p", null, "生效后系统不再启动新的研究任务、不再发起新的付费 AI 调用；已经开始的子任务可能继续做完，或在恢复后接着做。已产生的研究成果不受影响。直到按「恢复全部运行」才会恢复。"));
    if (!r.emergency) put(danger, writeForm({ action: "lever", label: "紧急停机", danger: true,
      confirmTitle: "暂停所有研究？",
      confirmText: "所有研究流程立即停止接新任务，不再发起新的付费调用；已开始的子任务可能继续做完。直到有人按「恢复全部运行」才会恢复。",
      payload: () => ({ lever: "emergency_stop", request_id: rid() }), after: () => reload("run") }));
    else put(danger, h("p", null, `停机中：${txt(r.emergency.label)}（${when(r.emergency.since)} 起）`),
      writeForm({ action: "lever", label: "恢复全部运行", confirmText: "所有未单独暂停的研究流程将重新开始接任务。",
        payload: () => ({ lever: "resume_all", request_id: rid() }), after: () => reload("run") }));
    return danger;
  }
  function companiesSection(r) {
    const companies = r.companies || [];
    if (!companies.length) return empty("还没有研究目标，暂无公司。");
    return h("div", null, companies.map(c => {
      const hrs = hoursInput();
      return h("div", { class: "adm-row" }, h("div", { class: "grow" },
        h("div", null, txt(c.label), " ", c.paused ? pill("warn", "已暂停") : pill("ok", "研究中"), c.prioritized ? pill("ok", "提高跟踪频率中") : null),
        h("div", { class: "adm-meta" }, txt(c.paused_label, `覆盖等级 ${txt(c.coverage_tier, "未标")}`))),
        c.paused ? writeForm({ action: "lever", label: "恢复研究", primary: false, key: `company:${c.company_ref}:resume`,
            payload: () => ({ lever: "resume", scope_kind: "company", scope_ref: c.company_ref, request_id: rid() }), after: () => reload("run") })
          : writeForm({ action: "lever", label: "暂停研究", primary: false, key: `company:${c.company_ref}:pause`,
            extra: h("label", { class: "adm-field" }, h("span", null, "暂停多久（小时）"), hrs), validate: () => hoursValid(hrs),
            payload: () => ({ lever: "pause", scope_kind: "company", scope_ref: c.company_ref, request_id: rid(), ...(hrs.value ? { hours: Number(hrs.value) } : {}) }), after: () => reload("run") }),
        writeForm({ action: "prioritize", label: c.prioritized ? "恢复常规跟踪频率" : "提高跟踪频率", primary: false, key: `company:${c.company_ref}:prioritize`,
          payload: () => ({ company_ref: c.company_ref, mode: c.prioritized ? "normal" : "prioritize", request_id: rid() }), after: () => reload("run") }));
    }));
  }
  function lanesSection(r) {
    const lanes = r.lanes || [];
    if (!lanes.length) return empty("没有可控制的研究流程。");
    const paused = lanes.filter(l => l.paused);
    const rows = lanes.map(l => {
      const hrs = hoursInput();
      return h("div", { class: "adm-row" }, h("div", { class: "grow" }, h("div", null, txt(l.label), " ", l.paused ? pill("warn", "已暂停") : pill("ok", "运行中")),
        l.paused_label ? h("div", { class: "adm-meta" }, txt(l.paused_label)) : null, tech("流程", l.lane)),
        l.paused ? writeForm({ action: "lever", label: `恢复「${l.label}」`, primary: false,
            payload: () => ({ lever: "resume", scope_kind: "lane", scope_ref: l.lane, request_id: rid() }), after: () => reload("run") })
          : writeForm({ action: "lever", label: `暂停「${l.label}」`, primary: false, extra: h("label", { class: "adm-field" }, h("span", null, "暂停多久（小时）"), hrs),
            validate: () => hoursValid(hrs),
            payload: () => ({ lever: "pause", scope_kind: "lane", scope_ref: l.lane, hours: hrs.value ? Number(hrs.value) : null, request_id: rid() }), after: () => reload("run") }));
    });
    return h("details", { class: "adm-more" }, h("summary", null, `按研究流程暂停（高级，${lanes.length} 个流程${paused.length ? `，${paused.length} 个已暂停` : ""}）`), rows);
  }
  function linesSection(r) {
    const lines = r.lines || [];
    if (!lines.length) return empty("目前没有进行中的专项研究。");
    return h("div", null, lines.map(x => h("div", { class: "adm-row" }, h("div", { class: "grow" }, txt(x.label), h("div", { class: "adm-meta" }, `开始于 ${when(x.created_at)}`), tech("研究线", x.line_ref)),
      writeForm({ action: "stop_line", label: "停止这项专项研究", danger: true, key: `stop_line:${x.line_ref}`,
        confirmTitle: `停止「${plain(x.label)}」？`,
        confirmText: "已产生的成果保留，但这项研究不会继续；以后需要时由规划重新立项。",
        payload: () => ({ line_ref: x.line_ref, request_id: rid() }), after: () => reload("run") }))));
  }
  function leverLog(r) {
    return h("details", { class: "adm-more" }, h("summary", null, `运行控制操作记录（${(r.receipts || []).length}）`),
      (r.receipts || []).length ? table(["时间", "操作", "对象", "结果", "理由"], r.receipts.map(x => [when(x.at), txt(x.lever_label), txt(x.target),
        `${txt(x.outcome_label, "")}${x.until ? " · 到 " + when(x.until) : ""}`, txt(x.reason)])) : empty("还没有操作记录。"));
  }
  function coverageSection(r) {
    const add = h("input", { type: "text", "aria-label": "新增公司代码", placeholder: "例如 ACN, IBM" });
    const tier = h("select", { "aria-label": "覆盖等级" }, h("option", { value: "A" }, "重点（A）"), h("option", { value: "B", selected: true }, "常规（B）"), h("option", { value: "C" }, "观察（C）"));
    const removeBox = h("div", { class: "adm-filter" }, (r.companies || []).map(c => {
      const cb = h("input", { type: "checkbox", value: c.company_ref, checked: state.coverageRemove.has(c.company_ref) || null });
      cb.addEventListener("change", () => { if (cb.checked) state.coverageRemove.add(c.company_ref); else state.coverageRemove.delete(c.company_ref); });
      return h("label", null, cb, ` ${c.label}`);
    }));
    const draftBox = h("div");
    const showDraft = out => {
      const r2 = out.result || {}, d = r2.draft || {};
      const adds = d.additions || [], rms = d.removals || [], b = d.budget || {}, share = b.coverage_pool_usd_per_company || {};
      swap(draftBox, h("div", { class: "adm-sec" }, h("h3", null, "草案：确认后才生效"), h("p", null, d.summary || "名单调整草案"),
        adds.length ? h("div", null, h("b", null, "新加入："), h("ul", null, adds.map(x => h("li", null, x.resolution === "resolved" ? `${x.ticker} · ${txt(x.name, "名称未返回")} · 覆盖等级 ${x.coverage_tier}` : `${x.ticker}：${txt(x.error, "未能识别")}`)))) : null,
        rms.length ? h("div", null, h("b", null, "移除："), h("ul", null, rms.map(x => h("li", null, x.ticker || "一家公司")))) : null,
        h("p", { class: "adm-meta" }, `预算影响：每日上限不变（${usd(b.max_daily_cost_usd)}）；每家公司分到约 ${usd(share.before)} → ${usd(share.after)}。${txt(b.note, "")}`),
        (d.warnings || []).length ? h("div", { class: "adm-banner warn" }, d.warnings.join("；")) : null,
        writeForm({ action: "publish", label: "确认覆盖名单调整", confirmText: "确认后新的覆盖名单立即生效，检索计划随之调整。确认？",
          payload: { draft_id: r2.draft_id, draft_hash: r2.draft_hash, request_id: rid() },
          after: () => { state.coverageRemove.clear(); reload("run"); } }),
        writeForm({ action: "decide", label: "放弃这份草稿", primary: false, defaultReason: "不采用这份名单草案",
          payload: { kind: "draft:coverage", ref: r2.draft_id, hash: r2.draft_hash, decision: "discard", request_id: rid() },
          after: () => swap(draftBox, empty("草稿已放弃。")) })));
    };
    return h("div", null, h("p", { class: "adm-explain" }, "先生成草案（会向 SEC 核对公司身份并估算预算影响），核对后再确认；生成草案本身不会改变研究范围。"),
      h("div", { class: "adm-form" }, h("label", { class: "adm-field" }, h("span", null, "新增公司代码"), add), h("label", { class: "adm-field" }, h("span", null, "覆盖等级"), tier)),
      h("div", { class: "adm-meta" }, "要移除的公司（勾选状态在刷新数据时保留）："), removeBox,
      writeForm({ action: "coverage_draft", label: "生成名单调整草案",
        validate: () => (add.value.trim() || state.coverageRemove.size ? null : "请填写要新增的代码，或勾选要移除的公司"),
        payload: () => ({ add: add.value, remove: [...state.coverageRemove], coverage_tier: tier.value, request_id: rid() }), after: showDraft }),
      draftBox);
  }
  /* 2026-10-09: each held research task is one card -- company, question,
     why it stopped, what each choice costs -- with its doors (放行一次重新进入
     / 再买一次 / 撤回) grouped under it, sent as the owner through
     owner_door.  No internal references on the card. */
  function heldTasksSection(list) {
    if (!Array.isArray(list) || !list.length) return empty("目前没有停下等你决定的专项研究。");
    return h("div", null, list.map(t => {
      const doors = t.doors || [];
      const safe = doors.filter(d => !(d.danger || d.door === "withdraw"));
      const risky = doors.filter(d => d.danger || d.door === "withdraw");
      const doorForm = d => {
        const cap = d.needs_cost_cap ? h("input", { type: "number", min: "0.01", max: "20", step: "0.01", "aria-label": "这次愿意花的费用上限（美元，0.01–20）" }) : null;
        const withdraw = d.door === "withdraw";
        return h("div", { class: "adm-door" }, h("div", { class: "adm-meta" }, txt(d.cost_label, d.needs_cost_cap ? "要花钱：会再买一次模型调用" : "不花钱")),
          writeForm({ action: "owner_door", label: txt(d.label), danger: withdraw, key: `door:${t.ref}:${d.door}`,
            confirmTitle: withdraw ? `撤回「${txt(t.company, "")}」这项研究？` : `${txt(d.label)}？`,
            confirmText: txt(d.confirm, withdraw ? "这项研究不再继续，已有记录存档。" : "将以你的身份执行这一步。"),
            extra: cap ? h("label", { class: "adm-field" }, h("span", null, "费用上限（美元，0.01–20）"), cap) : null,
            validate: () => (cap && !(Number(cap.value) > 0 && Number(cap.value) <= 20) ? "请填写这次愿意花的费用上限（0.01 到 20 美元）" : null),
            payload: () => ({ door: d.door, admission_ref: d.admission_ref, ...(cap ? { max_cost_usd: Number(cap.value) } : {}), request_id: rid() }),
            after: () => reload("run") }));
      };
      return h("article", { class: "adm-task", dataset: { role: "held-task" } },
        h("h3", null, t.company ? `${txt(t.company)}${t.question ? " · " + plain(t.question) : ""}`
          : txt(String(t.title || "").replace(/^等你决定：/, ""), "一项专项研究")),
        h("dl", { class: "adm-facts" },
          h("dt", null, "为什么停下"), h("dd", null, txt(t.reason_label, "研究任务停下了")),
          h("dt", null, "在哪个流程"), h("dd", null, txt(t.lane_label, "专项研究")),
          h("dt", null, "费用"), h("dd", null, pill(t.cost === "paid" ? "warn" : "ok", t.cost === "paid" ? "有付费选项" : t.cost === "within_budget" ? "原定预算内" : "免费"), " ", txt(t.cost_label, "")),
          t.consequence ? h("dt", null, "不处理会怎样") : null, t.consequence ? h("dd", null, txt(t.consequence)) : null,
          t.action ? h("dt", null, "怎么处理") : null, t.action ? h("dd", null, txt(t.action)) : null),
        t.why ? h("details", { class: "adm-more" }, h("summary", null, "详细原因"), h("p", { class: "adm-meta" }, txt(t.why))) : null,
        safe.length ? h("div", { class: "adm-doors" }, safe.map(doorForm)) : null,
        risky.length ? h("div", { class: "adm-doors adm-doors-danger" }, risky.map(doorForm)) : null);
    }));
  }
  function ownerDoorSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到负责人放行任务。");
    return h("div", null, h("p", { class: "adm-explain" }, txt(x.note)), (x.items || []).length
      ? table(["操作", "状态", "受理时间", "最近更新", "说明"], x.items.map(j => [h("div", null, txt(j.operation_label), tech("任务", j.job_ref)),
          pill(j.state === "succeeded" ? "ok" : j.state === "failed" ? "err" : "warn", txt(j.state_label)), when(j.requested_at), when(j.updated_at), txt(j.error, "")]))
      : empty(x.note || "没有负责人放行任务。"));
  }
  function guidanceSection(g, pending) {
    const box = h("div");
    if (!g || g.available === false) put(box, empty((g && g.reason) || "读不到重启与部署说明。"));
    else {
      put(box, h("p", { class: "adm-explain" }, txt(g.note)));
      (g.sections || []).forEach(s => put(box, h("h3", null, txt(s.title)), h("p", { class: "adm-explain" }, txt(s.explain)), commandsBlock(s.commands)));
    }
    if ((pending || []).length) {
      put(box, h("h3", null, "待处理事项的命令行做法"),
        h("p", { class: "adm-meta" }, "这些事项在本页和「待办决策」都有按钮；命令行做法只在按钮不可用时由技术管理员使用。"));
      pending.forEach(item => put(box, h("div", { class: "adm-row" }, h("div", { class: "grow" }, h("b", null, txt(item.title)),
        commandsBlock(item.commands)))));
    }
    return box;
  }
  function runIssues(levers, runtime) {
    const out = [];
    if (!levers.error) {
      if (levers.emergency) out.push({ level: "err", problem: "所有研究已暂停（紧急停机）",
        impact: "不再启动新的研究任务、不再发起付费调用；已开始的子任务可能继续做完。",
        action: "确认停机原因已排除后，在「暂停所有研究」点「恢复全部运行」。" });
      const pausedCompanies = (levers.companies || []).filter(c => c.paused);
      if (pausedCompanies.length) out.push({ problem: `${pausedCompanies.length} 家公司的研究已暂停：${pausedCompanies.map(c => txt(c.label)).slice(0, 4).join("、")}`,
        impact: "这些公司不会有新的研究、资料更新和结论变化。", action: "到期会自动恢复；需要提前恢复，在「公司」里点「恢复研究」。" });
      const pausedLanes = (levers.lanes || []).filter(l => l.paused);
      if (pausedLanes.length) out.push({ problem: `${pausedLanes.length} 个研究流程已暂停`,
        impact: "这些流程负责的研究步骤暂时不做。", action: "在「按研究流程暂停（高级）」里恢复。" });
    }
    if (!runtime.error) {
      const held = runtime.held_tasks || [];
      if (held.length) out.push({ problem: `${held.length} 项专项研究停下等你决定`,
        impact: "这些研究不会再自己动，它们要回答的问题一直没有答案。",
        action: "在「停下等你决定的专项研究」逐项选择：放行重试（通常免费）或撤回。" });
      const failed = (((runtime.owner_door_jobs || {}).items) || []).filter(j => j.state === "failed");
      if (failed.length) out.push({ level: "err", problem: `${failed.length} 个放行任务执行失败`,
        impact: "对应的研究仍然停着。", action: "看「负责人放行任务」里的说明，可以重新发起。" });
    }
    return out;
  }
  async function loadRun(p) {
    const [levers, runtime] = await Promise.all([getJson("/v1/cockpit/levers").catch(e => ({ error: e })),
      getJson("/v2/data/admin/runtime").catch(e => ({ error: e }))]);
    swap(p, issuesCard(runIssues(levers, runtime)),
      section("停下等你决定的专项研究", "每项一张卡片：哪家公司、什么问题、为什么停下、每个选择花不花钱。按钮以你的身份执行，需要写理由，留下回执。",
        runtime.error ? failureNode(runtime.error, "读取专项研究") : heldTasksSection(runtime.held_tasks)),
      section("公司", "暂停或恢复一家公司的研究，调整跟踪频率。暂停可以设到期时间，到期自动恢复。",
        levers.error ? failureNode(levers.error, "读取运行控制") : h("div", null, levers.banner ? h("div", { class: "adm-banner err", role: "alert" }, txt(levers.banner)) : null, companiesSection(levers))),
      section("进行中的专项研究", null, levers.error ? empty("运行控制读不到。") : linesSection(levers)),
      section("覆盖名单调整（草案 → 确认）", null, levers.error ? empty("运行控制读不到，暂时不能调整名单。") : coverageSection(levers)),
      section("负责人放行任务", null, runtime.error ? failureNode(runtime.error, "读取放行任务") : ownerDoorSection(runtime.owner_door_jobs)),
      section("高级：研究流程与紧急停机", "流程级暂停只在排查问题时使用；紧急停机会暂停所有研究。",
        levers.error ? empty("运行控制读不到。") : h("div", null, lanesSection(levers), emergencyZone(levers), leverLog(levers))),
      section("重启与部署", "这些操作需要技术管理员在终端完成；页面只说明做法，不执行任何命令。", runtime.error ? failureNode(runtime.error, "读取说明") : guidanceSection(runtime.guidance, runtime.pending_commands)));
  }

  // ---------- 预算与费用 ---------------------------------------------------------------
  function budgetCapsForm(mission, poolsNote, host) {
    if (!mission) {
      const effective = host && host.material_acquisition_effective;
      return h("div", null, empty("还没有发布研究目标：发布后才可编辑研究预算。"), effective ? h("p", { class: "adm-explain" }, `材料获取当前有效上限：每天 ${count(effective.calls_per_day)} 次、${usd(effective.usd_micros_per_day / 1000000)}。${effective.explicit_override ? "本项目使用单独设置的额度。" : "新建项目也采用共用设置；尚未保存时使用系统默认。"}`) : null);
    }
    const b = mission.budget || {};
    const material = (host && host.material_acquisition) || (host && host.material_acquisition_effective && { calls_per_day: host.material_acquisition_effective.calls_per_day, usd_per_day: host.material_acquisition_effective.usd_micros_per_day / 1000000 }) || { calls_per_day: 180, usd_per_day: 6 };
    /* Review M10: every field checked here by the server's rule -- money a
       positive amount, counts whole numbers, blank only where it means 不限. */
    const fields = [["max_daily_paid_calls", "模型调用上限（次 / 天，整数）", "1", "count"],
                    ["max_daily_cost_usd", "每天总费用上限（美元 / 天，大于 0）", "0.01", "money"],
                    ["max_alphaengine_calls_24h", "AlphaEngine 搜索上限（次 / 24 小时，整数）", "1", "count"],
                    ["max_daily_document_reads", "阅读原文上限（份 / 天，整数；留空保留现有上限）", "1", "optional"]];
    const inputs = {};
    const extra = fields.map(([key, label, step]) => { inputs[key] = h("input", { type: "number", min: "0", step, value: b[key] ?? "", "aria-label": label });
      return h("label", { class: "adm-field" }, h("span", null, label), inputs[key]); });
    const materialCalls = h("input", { type: "number", min: "0", step: "1", value: material.calls_per_day, "aria-label": "材料获取上限（次 / 天）" });
    const materialUsd = h("input", { type: "number", min: "0", step: "0.01", value: material.usd_per_day, "aria-label": "材料获取上限（美元 / 天）" });
    if (host) {
      extra.push(h("label", { class: "adm-field" }, h("span", null, "材料获取上限（次 / 天，全部项目共用设置，各项目分别计数）"), materialCalls));
      extra.push(h("label", { class: "adm-field" }, h("span", null, "材料获取上限（美元 / 天，各项目分别计数）"), materialUsd));
      if (host.material_acquisition_effective && host.material_acquisition_effective.explicit_override) extra.push(h("p", { class: "adm-explain" }, `本项目使用单独设置的额度：每天 ${count(host.material_acquisition_effective.calls_per_day)} 次、${usd(host.material_acquisition_effective.usd_micros_per_day / 1000000)}。此处保存的共用额度适用于其余项目和新项目。`));
    }
    const pools = h("input", { type: "checkbox", checked: b.pools_enforcement === "off" || null });
    extra.push(h("label", { class: "adm-field" }, h("span", null, "各研究环节的模型费用额度"), h("span", null, pools, " 只统计，不按环节额度暂停调用")));
    extra.push(h("p", { class: "adm-explain" }, "勾选后，模型调用仍计入各研究环节的费用，但不因该环节额度用完而暂停。材料获取额度、每天总费用和其他次数上限继续生效。"));
    const check = () => {
      if (host && (!materialCalls.value.trim() || !materialUsd.value.trim() || !Number.isInteger(Number(materialCalls.value)) || Number(materialCalls.value) < 0 || !isFinite(Number(materialUsd.value)) || Number(materialUsd.value) < 0)) return "材料获取次数需为零或正整数，美元额度需为零或正数";
      for (const [key, label, , kind] of fields) {
        const raw = (inputs[key].value || "").trim();
        if (kind === "optional" && raw === "") continue;
        const n = Number(raw);
        if (raw === "" || !isFinite(n)) return `请填写「${label}」`;
        if (kind === "money" && !(n > 0)) return "每天总费用上限必须大于 0 美元";
        if (kind !== "money" && !(Number.isInteger(n) && n >= 0)) return `「${label}」请填 0 或正整数，不能有小数`;
      }
      return null;
    };
    return h("div", null, h("p", { class: "adm-explain" }, `每天总费用、模型调用次数、AlphaEngine 搜索次数和阅读原文份数各有上限。达到上限后，当天暂停相应调用；每日用量在 UTC 零点重新计数。次数设为 0 时，相应调用保持关闭，直到您调高上限。${poolsNote || ""}`),
      writeForm({ action: "research_budget", label: "保存研究预算上限", extra,
        confirmText: "新的上限会保存为全部研究项目和新建项目的共用设置。运行中的调用继续按原授权结算；暂停或离线项目恢复后先应用设置再接新任务。",
        validate: check,
        payload: () => { const budget = { max_daily_paid_calls: Number(inputs.max_daily_paid_calls.value), max_daily_cost_usd: Number(inputs.max_daily_cost_usd.value || 0),
          max_alphaengine_calls_24h: Number(inputs.max_alphaengine_calls_24h.value || "0"), pools_enforcement: pools.checked ? "off" : "on" };
          if (host) budget.material_acquisition = { calls_per_day: Number(materialCalls.value), usd_per_day: materialUsd.value };
          const reads = inputs.max_daily_document_reads.value.trim(); if (reads !== "") budget.max_daily_document_reads = Number(reads);
          return { mission_ref: mission.mission_ref, budget, expected_mission_hash: mission.hash, ...(host ? { expected_host_settings_hash: host.desired_hash } : {}) }; },
        after: () => reload("budget") }));
  }
  function costTables(c) {
    const box = h("div");
    const local = c.local || {};
    if (local.available === false) { put(box, empty(local.reason)); return box; }
    put(box, h("div", { class: "adm-grid" },
      h("div", { class: "adm-cell" }, h("div", { class: "n" }, usd(local.today && local.today.cost_usd)), h("div", { class: "l" }, `本项目今天 · ${count(local.today && local.today.calls)} 次调用`)),
      h("div", { class: "adm-cell" }, h("div", { class: "n" }, usd(local.week && local.week.cost_usd)), h("div", { class: "l" }, `本项目近 7 天 · ${count(local.week && local.week.calls)} 次调用`)),
      c.shared_daily && c.shared_daily.status === "ok" ? h("div", { class: "adm-cell" }, h("div", { class: "n" }, usd(c.shared_daily.cost_usd)),
        h("div", { class: "l" }, `全部研究项目今天 / 共享上限 ${usd(c.shared_daily.cost_cap_usd)}`)) : null));
    put(box, h("h3", null, "按研究项目"), table(["研究项目", ["今天费用", true], ["今天调用（次）", true], ["近 7 天费用", true], ["近 7 天调用（次）", true]],
      (c.environments || []).map(e => e.reason ? [txt(e.name), e.reason, "", "", ""] : [txt(e.name), usd(e.today.cost_usd), count(e.today.calls), usd(e.week.cost_usd), count(e.week.calls)])));
    put(box, h("h3", null, "按研究流程（本项目）"), (local.by_lane || []).length ? table(["流程", ["今天费用", true], ["今天调用（次）", true], ["近 7 天费用", true], ["近 7 天调用（次）", true]],
      local.by_lane.map(e => [h("div", null, txt(e.label), tech("流程", e.lane)), usd(e.today.cost_usd), count(e.today.calls), usd(e.week.cost_usd), count(e.week.calls)])) : empty("近 7 天没有付费调用。"));
    put(box, h("h3", null, "按预算池（本项目）"), (local.by_pool || []).length ? table(["预算池", ["今天费用", true], ["近 7 天费用", true]],
      local.by_pool.map(e => [e.label, usd(e.today.cost_usd), usd(e.week.cost_usd)])) : empty("没有分池记录。"));
    const comparison = c.thinking_comparison;
    put(box, h("h3", null, "每份产物的发布流水线（滚动 24 小时）"), publicationPipeline(c.publication_pipeline));
    put(box, h("h3", null, "按环节：思考强度调整前后"),
      h("p", { class: "adm-explain" }, txt(c.thinking_comparison_note, "")),
      Array.isArray(comparison) && comparison.length ? table(["环节", "当前思考强度", "变更时间 / 理由",
        "此前：次数 / 平均费用 / 平均耗时", "此后：次数 / 平均费用 / 平均耗时"], comparison.map(e => {
          const stats = side => `${count(side.calls)} 次 · ${usd(side.average_cost_usd)} · ${side.average_seconds == null ? "—" : `${count(side.average_seconds)} 秒`}（${(side.levels || []).join("、") || "无记录"}）`;
          return [e.label, e.current_level, `${when(e.changed_at)}${e.reason ? ` / ${e.reason}` : ""}`,
            stats(e.before), stats(e.after)];
        })) : empty(comparison && comparison.reason || "暂无可比较的思考强度费用记录。"));
    return box;
  }
  function purposeCaps(models) {
    const rows = (models.purposes || []).filter(p => p.call_budget && p.call_budget.effective);
    if (!rows.length) return empty("没有可读的环节调用上限。");
    return h("div", null, table(["环节", ["每次调用费用上限（美元）", true], ["每次回答长度上限（token）", true], ["超时放弃（秒）", true], "范围"], rows.map(p => {
      const cb = p.call_budget, e = cb.effective || {};
      return [h("div", null, txt(p.label), tech("环节", p.purpose)), e.max_cost_usd != null ? usd(e.max_cost_usd) : "—", count(e.max_output_tokens), txt(e.timeout_seconds),
        cb.shared_limits && ["max_input_tokens", "max_output_tokens", "timeout_seconds"].every(key => (cb.shared_limits.governed_fields || []).includes(key))
          ? "全部研究项目共用" : cb.shared_call_cost ? "费用共用；其它上限见环节设置" : "仅本项目"];
    })), sharedCostEditor(rows));
  }
  function sharedCostEditor(rows) {
    const shared = rows.filter(p => p.call_budget.shared_call_cost && p.call_budget.shared_call_cost.policy_hash);
    if (!shared.length) return h("p", { class: "adm-meta" }, "每次调用的费用上限目前没有绑定全部研究项目共用的策略；回答长度上限在「AI 服务 → 单独调整某个环节」里修改。");
    const pick = h("select", { "aria-label": "环节" }, shared.map(p => h("option", { value: p.purpose }, p.label)));
    const cost = h("input", { type: "number", min: "0", step: "any", "aria-label": "每次调用费用上限（美元）" });
    return h("details", { class: "adm-more" }, h("summary", null, "调整某个环节的每次调用费用上限（全部研究项目）"),
      writeForm({ action: "model_budget", label: "保存每次调用费用上限（全部研究项目）", primary: false, danger: true,
        extra: [h("label", { class: "adm-field" }, h("span", null, "环节"), pick), h("label", { class: "adm-field" }, h("span", null, "每次调用费用上限（美元，大于 0）"), cost)],
        confirmText: "这会修改本机全部研究项目共用的每次调用费用上限，下一批任务采用，运行中批次完成后切换；超过上限的调用会被拒绝。",
        validate: () => (Number(cost.value) > 0 ? null : "请输入大于 0 的金额"),
        payload: () => { const p = shared.find(x => x.purpose === pick.value);
          return { purpose: p.purpose, kind: "call", budget: { max_cost_usd: Number(cost.value) }, expected_shared_policy_hash: p.call_budget.shared_call_cost.policy_hash }; },
        after: () => reload("budget") }));
  }
  function budgetIssues(costs) {
    const out = [];
    if (!costs.mission) out.push({ level: "info", problem: "还没有发布研究目标，因此没有研究预算",
      impact: "付费调用不会开始。", action: "先在投研工作台「投研规则与设置」发布研究目标。" });
    const cap = Number(((costs.mission || {}).budget || {}).max_daily_cost_usd);
    const spent = Number((((costs.local || {}).today) || {}).cost_usd);
    if (cap > 0 && isFinite(spent) && spent >= cap * 0.8) out.push({ level: spent >= cap ? "err" : "warn",
      problem: `本项目今天已用 ${usd(spent)}，达到每天上限 ${usd(cap)} 的 ${Math.round(spent / cap * 100)}%`,
      impact: spent >= cap ? "今天不再发起新的付费调用，研究进度放慢，明天（UTC 零点）自动恢复。" : "接近上限后，今天剩下的研究可能被拦下。",
      action: "不需要处理也会在次日恢复；确实需要多花，在下方「研究预算上限」调高每天总费用上限。" });
    const sh = costs.shared_daily;
    if (sh && sh.status === "ok" && Number(sh.cost_cap_usd) > 0 && Number(sh.cost_usd) >= Number(sh.cost_cap_usd) * 0.8) out.push({
      problem: `全部研究项目今天合计 ${usd(sh.cost_usd)}，接近共享上限 ${usd(sh.cost_cap_usd)}`,
      impact: "达到共享上限后，所有研究项目当天都不再发起付费调用。", action: "看「按研究项目」哪个项目用得多；共享上限由技术管理员调整。" });
    return out;
  }
  async function loadBudget(p) {
    const [costs, models] = await Promise.all([getJson("/v2/data/admin/costs"), getJson("/v1/cockpit/models").catch(() => ({ purposes: [] }))]);
    swap(p, issuesCard(budgetIssues(costs)),
      section("研究预算上限", "全部研究项目与新建项目共用；达到上限暂停相应调用，次日按日额度恢复。",
        costs.host_settings ? h("p", { class: "adm-explain" }, costs.host_settings.note) : null,
        budgetCapsForm(costs.mission, null, costs.host_settings)),
      section("费用：今天与近 7 天", txt(costs.note, ""), costTables(costs)),
      section("各环节调用上限（技术参数）", "每次 AI 调用的费用、回答长度与超时上限；与上面的业务预算分开，一般不需要调整。",
        h("details", { class: "adm-more" }, h("summary", null, "展开各环节调用上限"), purposeCaps(models))));
  }

  // ---------- 健康与故障 ---------------------------------------------------------------
  const PARITY_KIND = { ok: "ok", info: "muted", pending: "muted", warn: "warn", gap: "err", drift: "err" };
  const PARITY_LABELS = { ok: "一致", info: "说明", pending: "待完成", warn: "警告", gap: "缺口", drift: "不一致" };
  function paritySection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "一致性检查暂时读不到。");
    const counts = x.counts || {};
    const filter = h("div", { class: "adm-filter", role: "group", "aria-label": "按状态筛选" });
    const holder = h("div");
    let current = "problems";
    const render = () => {
      const rows = (x.rows || []).filter(r => current === "all" || !["ok", "info"].includes(r.status));
      swap(holder, rows.length ? table(["分区", "状态", "详情", "处理办法"], rows.map(r => [h("div", null, txt(r.section_label), tech("检查项", r.check)),
        pill(PARITY_KIND[r.status] || "muted", txt(r.status_label)), txt(r.detail),
        h("div", null, r.fix ? txt(r.fix) : (r.fix_technical ? "由技术管理员处理（见技术详情）" : ""), techNote("技术详情", r.fix_technical))])) : empty("没有需要处理的检查项。"));
      filter.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.f === current)));
    };
    [["problems", "只看需要处理的"], ["all", "全部"]].forEach(([f, label]) => { const b = btn(label, () => { current = f; render(); }); b.dataset.f = f; put(filter, b); });
    render();
    return h("div", null, h("p", { class: "adm-explain" }, txt(x.note)),
      h("div", { class: "adm-meta" }, Object.entries(counts).map(([k, v]) => `${PARITY_LABELS[k] || txt(k)} ${v}`).join(" · ")), filter, holder);
  }
  function ledgerSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "还没有失败台账。");
    const cells = x.cells || [];
    return h("div", null, h("p", { class: "adm-explain" }, txt(x.note)),
      h("div", { class: "adm-grid", dataset: { role: "ledger-cells" } }, cells.map(c => h("div", { class: `adm-cell${(c.key === "parked" || c.key === "permission") && (x.totals || {})[c.key] ? " alarm" : ""}` },
        h("div", { class: "n" }, count((x.totals || {})[c.key])), h("div", { class: "l" }, `${txt(c.label)} · ${txt(c.hint)}`)))),
      x.released ? h("p", { class: "adm-meta", dataset: { role: "ledger-released" } },
        `另有 ${count(x.released)} 项已自动解除等待：依赖的服务此后已恢复，或超过 7 天没有再受阻。它们不计入上面四格；如果问题仍在，下次尝试会重新登记。`) : null,
      (x.lanes || []).length ? h("details", { class: "adm-more" }, h("summary", null, `按流程查看（${x.lanes.length} 个流程）`),
        table(["流程", ...cells.map(c => [c.label, true]), "最近一次"], x.lanes.map(l => [h("div", null, txt(l.lane_label), tech("流程", l.lane)),
        ...cells.map(c => count(l[c.key])), when(l.last_seen)]))) : empty("台账里没有记录。"),
      x.historical_truncated ? h("p", { class: "adm-meta" }, "历史记录较多，按流程统计只计入最近 500 条。") : null);
  }
  function writerSection(w) {
    if (!w || w.available === false) return empty((w && w.reason) || "读不到写入与调度状态。");
    const t = w.ticks || {};
    return h("div", null, table(["项目", "状态"], [
      ["调度服务", h("span", null, pill(w.tick_stale ? "err" : w.heartbeat_present ? "ok" : "muted", txt(w.state_label)), " ", tech("状态", w.service_state))],
      ["最近一次调度", `${when(w.last_tick_at)}${w.tick_age_seconds != null ? `（${Math.round(w.tick_age_seconds / 60)} 分钟前）` : ""}`],
      ["写入服务连接", w.writer_socket_present ? "可连接" : "连不上（写入服务未运行，或本项目尚未启动）"],
      ["规划器最近错误", txt(w.planner_last_error, "无")],
      ["调度统计", t.available ? `${txt(t.idle_note)}（空闲比例 ${txt(t.idle_ratio)}）` : txt(t.reason, "还没有调度台账")]]),
      t.available && (t.stalled_lanes || []).length ? table(["停滞较多的流程", ["调度次数", true], ["无进展次数", true], ["最长连续无进展", true], ["预算池用完次数", true]],
        t.stalled_lanes.map(s => [txt(s.lane_label), count(s.ticks), count(s.stalls), count(s.longest_stall_run), count(s.pool_exhausted_ticks)])) : null);
  }
  function incidentSection(list) {
    if (!Array.isArray(list)) return empty((list && list.reason) || "读不到事故记录。");
    return h("div", null, list.map(i => h("div", { class: "adm-row" }, h("div", { class: "grow" },
      h("div", null, h("b", null, txt(i.title)), " ", pill(i.source === "live" ? "err" : "muted", i.source === "live" ? "当前" : "已处理"), " ", pill("muted", txt(i.kind_label))),
      h("div", { class: "adm-meta" }, `${when(i.at)}${i.resolved_at ? " → 恢复于 " + when(i.resolved_at) : ""}`),
      i.what ? h("p", null, i.what) : null, i.impact ? h("p", { class: "adm-meta" }, `影响：${i.impact}`) : null,
      i.fix ? h("p", { class: "adm-meta" }, `修复：${i.fix}`) : null, i.watch ? h("p", { class: "adm-meta" }, `注意：${i.watch}`) : null,
      tech("记录", i.doc || i.id)))));
  }
  /* The latest round's give-ups, each its own line (2026-10-09): which
     company, which kind of report, since when, and the retry -- not a bare
     「exhausted: 1」. */
  function exhaustedLine(it) {
    const who = [txt(it.company, ""), txt(it.kind_label, "研究产物")].filter(Boolean).join(" 的");
    return h("div", { class: "adm-row", dataset: { role: "exhausted-item" } }, h("div", { class: "grow" },
      h("b", null, `${who}：中文定稿重试次数用完`), " ", pill("err", "停在草稿"),
      h("div", { class: "adm-meta" }, `最后一次失败 ${when(it.failed_at)}${it.retries ? ` · 已重试 ${it.retries} 次` : ""}`),
      h("div", { class: "adm-meta" }, it.auto_retry ? "系统会自己再试一次，暂时不需要处理。"
        : "影响：这份报告的中文版本出不来，研究页只显示原稿；其他研究不受影响。系统不会再自动重试。"),
      techNote("技术详情（失败原因与标识）", [it.reason, it.version_ref && `版本：${it.version_ref}`, it.identity_hash && `产物：${it.identity_hash}`].filter(Boolean).join("\n"))),
      it.auto_retry ? null : writeForm({ action: "language_retry", label: "重新进行语言质检", primary: true, key: `language_retry:${it.identity_hash}`,
        confirmText: "系统会丢弃上次缓存的译稿，重新起草、语言质检并独立核验（约 5 分钟内开始，花一次起草和质检的模型费用）。",
        payload: () => ({ identity_hash: it.identity_hash, product_hash: it.product_hash, request_id: rid() }), after: () => reload("health") }));
  }
  function publicationSection(x) {
    if (!x || x.available === false) return empty((x && x.reason) || "读不到定稿发布服务。");
    if (!x.present) return empty(x.note);
    const last = x.last_run && x.last_run.available !== false ? x.last_run : null;
    const items = (last && last.exhausted_items) || [];
    return h("div", null,
      publicationPipeline(x.pipeline),
      last ? h("div", { dataset: { role: "publication-last-run" } },
        h("h3", null, `最近一轮（${when(last.checked_at)}）`),
        h("p", { class: "adm-meta" }, [["pending", "等待处理"], ["parked", "等你决定"], ["exhausted", "重试次数用完"], ["completed", "本轮定稿"], ["skipped", "未进入文字定稿"]]
          .filter(([k]) => (last.counts || {})[k] != null).map(([k, l]) => `${l} ${count(last.counts[k])} 份`).join(" · ")),
        items.length ? items.map(exhaustedLine) : empty("最近一轮没有重试次数用完的报告。")) : (x.last_run ? empty(txt(x.last_run.reason)) : null),
      h("details", { class: "adm-more" }, h("summary", null, "累计统计（含已被新版本取代的旧版本）"),
        h("p", { class: "adm-explain" }, txt(x.note)), h("div", { class: "adm-grid" },
        Object.entries(x.counts || {}).map(([k, v]) => h("div", { class: "adm-cell" },
          h("div", { class: "n" }, count(v)), h("div", { class: "l" }, txt((x.labels || {})[k], k))))),
        (x.exhausted_items || []).length ? h("details", { class: "adm-more" }, h("summary", null, `较早的 ${x.exhausted_items.length} 份语言质检放弃的版本（可重新质检）`),
          x.exhausted_items.map(it => h("div", { class: "adm-row" }, h("div", { class: "grow" }, `${txt(it.kind)} · ${when(it.at)}`, tech("对象", it.subject_ref), tech("版本", it.version_ref)),
            writeForm({ action: "language_retry", label: "重新进行语言质检", primary: false, key: `language_retry_old:${it.identity_hash}`,
              payload: () => ({ identity_hash: it.identity_hash, product_hash: it.product_hash, request_id: rid() }), after: () => reload("health") })))) : null),
      h("p", { class: "adm-meta" }, `最近变化 ${when(x.last_change_at)} · 发布路由受阻 ${count(x.route_blocks)} 项${x.truncated ? " · 文件较多，只统计了前 20000 个" : ""}${x.unreadable ? ` · ${x.unreadable} 个状态文件无法读取` : ""}`));
  }
  function publicationPipeline(value) {
    if (!value || !value.present) return empty("尚无发布流水线次数记录。");
    const reasons = { new_version: "新版本", daily_retry: "每日重试", owner_retry: "管理员重试", verifier_rejected: "核验拒绝", language_review_failed: "语言复审未过", rules_upgrade: "规则升级", publication_run_24h_limit: "24 小时已运行 2 次，暂缓", publication_call_24h_limit: "24 小时已调用 6 次，暂缓", publication_same_tick_stage_duplicate: "同轮相同环节重复，已拦截" };
    return h("div", { dataset: { role: "publication-pipeline-limits" } },
      h("p", { class: "adm-explain" }, txt(value.note)),
      value.audit ? h("p", { class: "adm-meta" }, `重复调用与次数上限检查：${value.audit.passed ? "通过" : "发现异常"}${(value.audit.legacy_overages || []).length ? ` · 保留的旧回执中有 ${count(value.audit.legacy_overages.length)} 条历史超限，后续调用仍受限制` : ""}`) : null,
      table(["产物 / 重复运行榜", "运行次数", "调用次数", "费用", "提示词大小 / 各环节", "原因"], (value.artifacts || []).map(row => [
        h("div", null, txt((row.identity || {}).kind, "研究产物"), tech("版本", (row.identity || {}).version_ref), tech("原稿哈希", row.source_hash)),
        count(row.runs), count(row.calls), usd(row.cost_micros / 1000000),
        `${count(row.prompt_bytes)} 字节；${(row.stages || []).map(s => `${({ research_localization: "中文起草", research_language_check: "语言检查", research_language_revision: "语言修订", research_localization_verifier: "事实保真核验" })[s.stage] || s.stage} ${count(s.calls)} 次 / ${count(s.prompt_bytes)} 字节`).join("；")}`,
        (row.reasons || []).map(reason => reasons[reason] || reason).join("、")])));
  }
  const LANE_STATUS = { launched: "运行中", busy: "运行中", running: "运行中", idle: "空闲", waiting: "等待下次更新",
    unstarted: "尚未开始", unavailable: "不可用", unapproved: "等待批准", ungranted: "未授权", failed: "失败",
    held: "暂缓", paused: "已暂停", parked: "等待外部服务" };
  function laneStates(overview) {
    const lanes = ((overview || {}).activity || {}).lanes || [];
    if (!lanes.length) return empty("还没有研究流程的运行状态。");
    return table(["流程", "状态", "说明"], lanes.map(l => [h("div", null, txt(l.label || l.lane), tech("流程", l.lane || l.key)),
      pill(["launched", "busy", "running", "idle"].includes(l.status) ? "ok" : ["unavailable", "failed", "error"].includes(l.status) ? "err" : "warn", txt(l.status_label || LANE_STATUS[l.status] || l.status)),
      txt(l.detail_label || l.note || (typeof l.detail === "string" ? l.detail : ""), "")]));
  }
  function logSection(log) {
    const events = (log && log.events) || [];
    if (!events.length) return empty("还没有运行记录。");
    const kinds = [...new Set(events.map(e => e.kind).filter(Boolean))];
    const kindLabel = new Map(events.map(e => [e.kind, e.lane || e.kind]));
    const filter = h("div", { class: "adm-filter", role: "group", "aria-label": "按类型筛选" });
    const holder = h("div");
    let current = "all";
    const render = () => {
      const rows = events.filter(e => current === "all" || e.kind === current).slice(0, 80);
      swap(holder, table(["时间", "类型", "内容"], rows.map(e => [when(e.at), h("span", null, txt(e.lane || e.kind), tech("类型", e.kind)), `${txt(e.title, "")}${e.detail ? " · " + plain(String(e.detail).slice(0, 240)) : ""}`])));
      filter.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.f === current)));
    };
    ["all", ...kinds].forEach(k => { const b = btn(k === "all" ? `全部（${events.length}）` : kindLabel.get(k) || k, () => { current = k; render(); }); b.dataset.f = k; put(filter, b); });
    render();
    return h("div", null, filter, holder);
  }
  /* The failure ledger item by item (the old cockpit's 运维待办 panel):
     what waits on which outside service since when, what waits for a grant,
     the stopped attempts grouped by cause, and the resolved history a page
     at a time.  Read-only: GET /v1/cockpit/ops. */
  function opsDetail(r) {
    if (!r || r.available === false) return empty(txt(r && r.reason, "还没有失败台账。"));
    const line = it => h("li", null, `${txt(it.lane_label, "流程")} · ${txt(it.item_label, "任务")}：${txt(it.display_reason || it.history_note, "")}`);
    const deps = r.dependencies || [];
    const out = [h("p", { class: "adm-explain" }, txt(r.note, ""))];
    out.push(h("h3", null, `等待外部服务（${count(r.parked_items)} 件）`));
    if (!deps.length) out.push(empty("目前没有因外部服务不可用而暂缓的任务。"));
    for (const dep of deps) out.push(h("details", { class: "adm-more" },
      h("summary", null, `${txt(dep.dependency_label, "外部服务")} · ${count(dep.item_count)} 件 · 首次受阻 ${when(dep.first_seen)} · 最近尝试 ${when(dep.last_seen)} · 累计 ${count(dep.attempts)} 次`),
      h("ul", null, (dep.items || []).map(line))));
    out.push(h("h3", null, `等待授权（${count(r.permission_count)} 件）`));
    out.push((r.permission_items || []).length ? h("ul", null, r.permission_items.map(line)) : empty("没有等待授权的任务。"));
    const groups = r.terminal_groups || [];
    out.push(h("h3", null, `已停止的研究尝试（${count(r.terminal_count)} 次）`));
    out.push(groups.length ? table(["流程", "原因", ["次数", true], "最近一次"], groups.map(g => [txt(g.lane_label), txt(g.display_reason), count(g.count), when(g.last_seen)]))
      : empty("没有已停止的尝试。"));
    const hist = r.historical_items || [];
    if (hist.length || r.historical_count) {
      const list = h("ul", null, hist.map(line));
      const box = h("details", { class: "adm-more" }, h("summary", null, `已处理的历史记录（共 ${count(r.historical_count)} 条，配置或研究目标已更新，仅作记录）`), list);
      let next = r.historical_page && r.historical_page.next_offset;
      if (next != null) {
        const more = btn("再看一页", async () => {
          more.disabled = true;
          try {
            const page = await getJson(`/v1/cockpit/ops?historical_offset=${encodeURIComponent(next)}`);
            put(list, (page.historical_items || []).map(line));
            next = page.historical_page && page.historical_page.next_offset;
            if (next == null) more.remove();
          } catch (e) { put(box, failureNode(e, "读取历史记录")); }
          finally { more.disabled = false; }
        });
        put(box, more);
      }
      out.push(box);
    }
    return h("div", null, out);
  }
  function healthIssues(health, ops) {
    const out = [];
    const w = health.writer || {};
    if (w.tick_stale) out.push({ level: "err", problem: "调度超过 10 分钟没有推进",
      impact: "研究流程不会接新任务。", action: "先看「运行控制 → 负责人放行任务」有没有执行中的操作；再请技术管理员按「重启与部署」处理。" });
    for (const dep of ((ops && ops.dependencies) || []).slice(0, 3)) out.push({
      problem: `${count(dep.item_count)} 项任务在等「${txt(dep.dependency_label, "外部服务")}」恢复（首次受阻 ${when(dep.first_seen)}，最近一次 ${when(dep.last_seen)}）`,
      impact: "这些任务暂缓，不算重试次数；服务恢复后自动重试。",
      action: "多数情况下不需要处理；同一服务长时间不恢复时，按服务处理（例如重新登录、调高预算或添加备用 AI 服务）。" });
    if (ops && ops.permission_count) out.push({ problem: `${count(ops.permission_count)} 项任务在等授权`,
      impact: "授权前这些任务不会继续。", action: "看「失败台账 · 明细 → 等待授权」，批准对应的数据接口或权限。" });
    const last = ((health.publication || {}).last_run) || {};
    const exhausted = (last.exhausted_items || []).filter(it => !it.auto_retry);
    if (exhausted.length) out.push({ level: "err", problem: `${exhausted.length} 份报告的中文定稿重试次数用完：${exhausted.map(it => [txt(it.company, ""), txt(it.kind_label, "")].filter(Boolean).join(" 的")).slice(0, 3).join("、")}`,
      impact: "这些报告的中文版本出不来，研究页只显示原稿。", action: "在「定稿发布服务」逐份点「重新进行语言质检」。" });
    const parity = health.parity || {};
    const gaps = (parity.rows || []).filter(r => ["gap", "drift"].includes(r.status));
    if (gaps.length) out.push({ problem: `本项目有 ${gaps.length} 项配置与其他研究项目不一致或缺失`,
      impact: "相关研究步骤可能不运行，或与其他项目结果不同。", action: "看「环境一致性检查」每项的处理办法。" });
    return out;
  }
  async function loadHealth(p) {
    const [health, ops] = await Promise.all([getJson("/v2/data/admin/health"),
      getJson("/v1/cockpit/ops").catch(e => ({ error: e }))]);
    const lanesBox = h("div", null, empty("正在读取流程状态…")), logBox = h("div", null, empty("正在读取运行记录…"));
    swap(p, issuesCard(healthIssues(health, ops.error ? null : ops)),
      section("失败台账 · 概览", "只计仍在等待的任务：等待外部服务、等待授权、已停止、已处理（历史）。", ledgerSection(health.ledger)),
      section("失败台账 · 明细", "逐件列出仍在等待和已停止的任务。", ops.error ? failureNode(ops.error, "读取失败台账明细") : opsDetail(ops)),
      section("定稿发布服务", "研究报告的中文定稿：最近一轮的结果，以及需要你处理的报告。", publicationSection(health.publication)),
      section("写入与调度", null, writerSection(health.writer)),
      section("事故与异常", "当前异常和需要记住的历史事故。", incidentSection(health.incidents)),
      section("环境一致性检查", null, paritySection(health.parity)),
      section("研究流程状态", null, h("details", { class: "adm-more" }, h("summary", null, "展开各研究流程的运行状态"), lanesBox)),
      section("运行记录", "最近的系统运行记录，按类型筛选。", h("details", { class: "adm-more" }, h("summary", null, "展开运行记录"), logBox)));
    getJson("/v1/cockpit/overview").then(o => swap(lanesBox, laneStates(o))).catch(e => swap(lanesBox, failureNode(e, "读取流程状态")));
    getJson("/v1/cockpit/log?limit=200").then(l => swap(logBox, logSection(l))).catch(e => swap(logBox, failureNode(e, "读取运行记录")));
  }

  // ---------- 研究项目管理 -------------------------------------------------------------
  function pausedResearch(item) {
    const body = h("div", null);
    const box = h("details", null, h("summary", null, "阅读暂停项目的已有研究（只读）"), body);
    box.addEventListener("toggle", async () => {
      if (!box.open || body.childNodes.length) return;
      try {
        const data = await getJson(item.readonly_url);
        put(body, h("p", null, "研究服务已停止，以下内容来自已保存的研究版本。"),
          (data.products || []).map(product => h("article", null, h("h4", null, product.display_label || product.label || product.kind),
            (product.sections || []).map(section => h("div", null, h("b", null, section.title || ""), h("p", null, section.body || ""))))));
      } catch (error) { put(body, failureNode(error, "读取暂停项目研究")); }
    });
    return box;
  }
  function workspacesSection(w) {
    if (w.enabled === false) return empty(w.reason || "研究项目管理尚未配置。");
    const items = Array.isArray(w.items) ? w.items : [];
    const goal = h("textarea", {rows: "3", maxlength: "2000", "aria-label": "初始研究目标（可选）"});
    const name = h("input", { type: "text", maxlength: "80", "aria-label": "新项目名称" });
    let createId = rid();
    return h("div", null,
      items.length ? items.map(item => {
        const label = item.display_name || item.name || "未命名研究项目";
        const lifecycle = item.lifecycle || "active";
        const ops = [];
        const reviewed = h("input", {type: "checkbox", "aria-label": "已检查待决策项与未发布草稿"});
        const irreversible = h("input", {type: "checkbox", "aria-label": "确认删除不可恢复"});
        const typed = h("input", {type: "text", "aria-label": `输入项目名${label}确认删除`});
        const pending = item.pending_review || item.archive_integrity?.reviewed_items || [];
        if (item.slug !== "legacy" && lifecycle !== "deleted") {
          const actions = lifecycle === "active" ? ["pause", "keep"] : lifecycle === "paused" ? ["restore", "archive", "delete"] : ["restore", "delete"];
          const labels = {pause: "暂停项目", archive: "归档项目", restore: "恢复项目", delete: "永久删除项目", keep: "继续保留项目"};
          for (const action of actions) { const operationId = item.lifecycle_operation?.status === "failed" && item.lifecycle_operation.action === action ? item.lifecycle_operation.request_id : rid(); ops.push(writeForm({action: "workspace_lifecycle", label: labels[action], primary: false, danger: action !== "keep",
            typeToConfirm: label, confirmText: ({delete: "项目草稿、研究数据与全部归档将被永久删除，操作记录会保留。", pause: `暂停「${label}」后停止更新研究，已有研究和草稿会保留，可随时恢复。`, archive: `归档「${label}」后从日常项目列表隐藏，研究和草稿会打包保存，可在这里恢复。`, restore: `恢复「${label}」后继续使用原有研究和草稿，并重新启动研究服务。`, keep: `继续保留「${label}」，研究设置不变。`})[action],
            extra: action === "delete" ? h("div", null, h("p", null, `待决策项与未发布草稿：${pending.length} 项`),
              pending.map(row => h("div", null, row.title || (row.table === "unpublished_goal" ? "尚未发布的初始研究目标" : /draft/.test(row.table) ? "尚未发布的研究草稿" : "尚未处理的研究事项"), tech("记录详情", `${row.table} · ${row.row} · ${row.status}`))),
              h("label", null, "输入项目名称", typed), h("label", null, reviewed, "我已检查上述待决策项与未发布草稿"), h("label", null, irreversible, "我确认删除不可恢复")) : null,
            validate: () => action === "delete" && (typed.value !== label || !reviewed.checked || !irreversible.checked) ? "请输入项目名并确认草稿审阅与不可恢复" : null,
            payload: () => ({slug: item.slug, action, request_id: operationId, ...(action === "delete" ? {confirm_name: typed.value, review_ack: reviewed.checked, irreversible: irreversible.checked} : {})}),
            after: () => { setTimeout(() => reload("projects"), 1500); } })); }
        }
        const next = h("input", { type: "text", maxlength: "80", value: label, "aria-label": `${label}的新名称` });
        return h("div", { class: "adm-row" }, h("div", { class: "grow" }, h("div", null, label, " ", item.current ? pill("ok", "当前项目") : null),
          h("div", { class: "adm-meta" }, ({active: "运行中", paused: "已暂停 · 只读", archived: "已归档", deleted: "已删除"}[lifecycle] || item.status_label || item.status)), tech("项目标识", item.slug || item.workspace_id)),
          item.url && !item.current ? h("a", { href: item.url, class: "adm-btn" }, "进入") : null,
          item.lifecycle_operation ? h("p", {class: "adm-meta"}, `最近操作：${{pause: "暂停项目", archive: "归档项目", restore: "恢复项目", delete: "永久删除项目", keep: "继续保留项目"}[item.lifecycle_operation.action] || "项目管理"} · ${{processing: "正在执行", failed: "未完成，请重试同一次请求", complete: "已完成"}[item.lifecycle_operation.status] || "状态待核实"}`) : null,
          item.readonly_url ? pausedResearch(item) : null,
          ...ops,
          lifecycle !== "deleted" ? writeForm({ action: "workspace_rename", label: "重命名", primary: false, defaultReason: "调整项目名称", extra: h("label", { class: "adm-field" }, h("span", null, "新名称"), next),
            validate: () => (next.value.trim() && next.value.trim() !== label ? null : "请填写一个不同的新名称"),
            payload: () => ({ slug: item.slug || "", name: next.value.trim() }), after: () => reload("projects") }) : null);
      }) : empty("还没有研究项目。"),
      h("h3", null, "新建研究项目"),
      w.can_create === false ? empty(w.create_disabled_reason || "当前暂时不能新建研究项目。") :
        writeForm({ action: "workspace_create", label: "新建研究项目", extra: h("div", null, h("label", {class: "adm-field"}, "项目名称", name), h("label", {class: "adm-field"}, "初始研究目标（可选）", goal)),
          validate: () => (name.value.trim() ? null : "请填写项目名称"),
          payload: () => ({ name: name.value.trim(), request_id: createId, initial_goal: goal.value.trim() }), after: () => { createId = rid(); reload("projects"); } }));
  }
  function templatesSection(r) {
    const templates = r.templates || [];
    const opt = (selected, withNone) => h("select", { "aria-label": "模板" }, withNone ? h("option", { value: "" }, "（不指定）") : null,
      templates.map(t => h("option", { value: t.version_ref, selected: t.version_ref === selected || null }, `${t.label}${t.is_latest ? "" : "（旧版本）"}`)));
    const choose = (scopeKind, scopeRef, sel, label) => writeForm({ action: "fund_template_select", label, primary: false,
      payload: () => { const t = templates.find(x => x.version_ref === sel.value);
        return { scope_kind: scopeKind, scope_ref: scopeRef, template_version_ref: sel.value || null, template_version_hash: t ? t.version_hash : null, request_id: rid() }; },
      after: () => reload("projects") });
    const gsel = opt((r.default || {}).template_version_ref, false);
    return h("div", null,
      h("h3", null, "已登记的模板"), templates.length ? table(["模板", "来源", "登记", "工作表", "正在使用"], templates.map(t => [
        h("div", null, txt(t.label), t.is_default ? " · 本项目默认" : "", tech("模板", t.slug)), `${txt(t.origin_label)}${t.source_filename ? `（${t.source_filename}）` : ""}`,
        `${txt(t.who)} · ${when(t.created_at)}`, Object.values(t.sheet_names || {}).join(" / "), (t.used_by || []).join("、") || "无"])) : empty("还没有登记的模板。"),
      h("h3", null, "本项目默认模板"), h("div", { class: "adm-meta" }, `当前：${txt((r.default || {}).template_label)}（${(r.default || {}).chosen_by_owner ? "负责人指定" : "系统内置"}）`),
      h("div", { class: "adm-form" }, gsel), choose("default", "default", gsel, "设为本项目默认"),
      (r.industries || []).length ? h("details", { class: "adm-more" }, h("summary", null, "按行业分类指定默认模板"), r.industries.map(i => {
        const sel = opt(i.template_version_ref || "", true);
        return h("div", { class: "adm-row" }, h("div", { class: "grow" }, `行业分类「${i.label}」 · ${txt(i.template_label, "未设定，按项目默认")}`), sel,
          choose("industry", i.value, sel, "保存该行业的模板"));
      })) : null,
      (r.receipts || []).length ? h("details", { class: "adm-more" }, h("summary", null, `模板操作记录（${r.receipts.length}）`),
        table(["时间", "操作", "范围", "模板", "理由"], r.receipts.map(x => [when(x.at), txt(x.action_label), `${txt(x.scope_label, "")} ${txt(x.target, "")}`, txt(x.template_label), txt(x.reason)]))) : null,
      h("p", { class: "adm-meta" }, "上传新模板在投研工作台的「投研规则与设置 → 财务模型导出底稿模板」：", h("a", { href: "/#/settings?focus=templates" }, "上传财务模型模板"), "。登记后在这里指定使用范围，单家公司的模板可在公司页导出 Excel 时指定。"));
  }
  async function loadProjects(p) {
    const [w, t] = await Promise.all([getJson("/v1/cockpit/workspaces").catch(e => ({ enabled: false, reason: errorText(e, "读取研究项目") })),
      getJson("/v1/cockpit/fund-templates").catch(e => ({ error: e }))]);
    const issues = [];
    if (w.enabled === false) issues.push({ level: "info", problem: "研究项目管理尚未配置", impact: "不能在这里新建或重命名研究项目。", action: txt(w.reason, "由技术管理员配置项目管理服务。") });
    if (!t.error && !(t.templates || []).length) issues.push({ level: "info", problem: "还没有登记 Excel 模板", impact: "导出财务模型时使用系统内置模板。", action: "在「研究设置 → 报告模板」上传后，回到这里指定使用范围。" });
    swap(p, issuesCard(issues),
      section("研究项目", "每个研究项目的资料和结论单独保存。新建与重命名会通过本机的项目管理服务完成。", workspacesSection(w)),
      section("Excel 模板登记", null, t.error ? failureNode(t.error, "读取模板") : templatesSection(t)));
  }

  const LOADERS = { ai: loadAi, connections: loadConnections, run: loadRun, budget: loadBudget, health: loadHealth, projects: loadProjects };

  // ---------- embedding ------------------------------------------------------------------
  const api = { mount, version: ADMIN_VERSION, tabs: TABS.map(t => t[0]), select: id => state.root && select(id, false), _state: state };
  window.DaltonAdmin = api;
  const shell = window.DaltonV2;
  let embedded = false;
  if (shell && typeof shell.registerPage === "function") {
    const page = Object.assign(el => mount(el), { id: "admin", title: "管理员设置", render: el => mount(el), mount: el => mount(el) });
    try { shell.registerPage("admin", page); embedded = true; }
    catch (_e) { try { shell.registerPage(page); embedded = true; } catch (_e2) { embedded = false; } }
  }
  const boot = () => { const root = document.getElementById("admin-root"); if (root && !state.root) mount(root); };
  if (!embedded || document.getElementById("admin-root")) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  }
})();
