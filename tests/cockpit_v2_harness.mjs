// Node harness for tests/test_cockpit_v2_shell.py: loads the v2 shell, copy and
// page modules with a small fake DOM and prints JSON results.
//   node tests/cockpit_v2_harness.mjs <command> '<json input>'
import {pathToFileURL} from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../src/dalton_core/cockpit_v2");
const load = rel => import(pathToFileURL(path.join(ROOT, rel)).href);
const [command, rawInput] = process.argv.slice(2);
const input = rawInput ? JSON.parse(rawInput) : {};

/* ------------------------------------------------------------ fake DOM -- */
class Node_ {
  constructor(doc) { this.ownerDocument = doc; this.parentNode = null; this.childNodes = []; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  _adopt(n) { if (n.parentNode) n.parentNode._drop(n); n.parentNode = this; return n; }
  _drop(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; }
  append(...nodes) { for (const n of nodes) { const x = typeof n === "object" ? n : this.ownerDocument.createTextNode(String(n)); this.childNodes.push(this._adopt(x)); } }
  prepend(...nodes) { for (const n of nodes.reverse()) { const x = typeof n === "object" ? n : this.ownerDocument.createTextNode(String(n)); this.childNodes.unshift(this._adopt(x)); } }
  replaceChildren(...nodes) { for (const c of [...this.childNodes]) this._drop(c); this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode._drop(this); }
  // The chart modules (charts/*.mjs) build with appendChild/removeChild.
  appendChild(n) { this.append(n); return n; }
  removeChild(n) { this._drop(n); return n; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  get textContent() { return this.childNodes.map(c => c.textContent).join(""); }
  set textContent(v) { this.replaceChildren(); if (v !== "" && v != null) this.append(String(v)); }
}
class Text_ extends Node_ {
  constructor(doc, text) { super(doc); this.data = text; this.nodeType = 3; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}
class Element_ extends Node_ {
  constructor(doc, tag) { super(doc); this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attributes = new Map(); this.listeners = {}; this.style = {}; this.hidden = false; this.disabled = false; this._value = ""; this._valueSet = false; this.checked = false; this.dataset = {}; this.open = false; }
  // A <select> reads as its selected option (or the first), like a browser.
  get value() {
    if (this.tagName === "SELECT" && !this._valueSet) {
      const options = this.querySelectorAll("option");
      const chosen = options.find(o => o.hasAttribute("selected")) || options[0];
      return chosen ? chosen.value : "";
    }
    return this._value;
  }
  set value(v) { this._value = v == null ? "" : String(v); this._valueSet = true; }
  setAttribute(k, v) { this.attributes.set(k, String(v)); if (k === "id") this.id = String(v); if (k === "class") this.className = String(v); }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  removeAttribute(k) { this.attributes.delete(k); }
  hasAttribute(k) { return this.attributes.has(k); }
  set className(v) { this.attributes.set("class", v); }
  get className() { return this.attributes.get("class") || ""; }
  set id(v) { this.attributes.set("id", v); }
  get id() { return this.attributes.get("id") || ""; }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  // Events bubble to the ancestors, as input/change/submit do in a browser.
  dispatch(ev, extra = {}) {
    const event = {type: ev, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra};
    for (let x = this; x; x = x.parentNode) for (const fn of (x.listeners && x.listeners[ev]) || []) fn({...event, currentTarget: x});
  }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
  click() { this.dispatch("click"); }
  get children() { return this.childNodes.filter(c => c.nodeType === 1); }
  _all() { const out = []; const walk = n => { for (const c of n.children) { out.push(c); walk(c); } }; walk(this); return out; }
  querySelectorAll(sel) { return this._all().filter(e => matches(e, sel, this)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let x = this; x && x.nodeType === 1; x = x.parentNode) if (matches(x, sel)) return x; return null; }
}
function simple(e, part) {
  const re = /([a-zA-Z0-9]+)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([\^]?=)['"]?([^'"\]]*)['"]?)?\]/g;
  let m, ok = true;
  while ((m = re.exec(part))) {
    if (m[1]) ok = ok && e.tagName === m[1].toUpperCase();
    else if (m[2]) ok = ok && e.id === m[2];
    else if (m[3]) ok = ok && e.className.split(/\s+/).includes(m[3]);
    else if (m[4]) {
      const v = e.getAttribute(m[4]);
      ok = ok && v != null && (!m[5] || (m[5] === "=" ? v === m[6] : v.startsWith(m[6])));
    }
  }
  return ok;
}
function matches(e, sel, scope) {
  return sel.split(",").some(one => {
    const parts = one.trim().split(/\s+/);
    if (!simple(e, parts[parts.length - 1])) return false;
    let x = e.parentNode;
    for (let i = parts.length - 2; i >= 0; i--) {
      while (x && x.nodeType === 1 && !simple(x, parts[i])) x = x.parentNode;
      if (!x || x.nodeType !== 1) return false;
      x = x.parentNode;
    }
    return !scope || true;
  });
}
class Document_ {
  constructor() { this.body = new Element_(this, "body"); this.activeElement = this.body; }
  createElement(tag) { return new Element_(this, tag); }
  createElementNS(_ns, tag) { return new Element_(this, tag); }
  createTextNode(t) { return new Text_(this, t); }
  getElementById(id) { return this.body.querySelector(`#${id}`); }
  querySelector(s) { return this.body.querySelector(s); }
  querySelectorAll(s) { return this.body.querySelectorAll(s); }
  contains(n) { return this.body.contains(n); }
}

// Exit once written: a page may leave a timer running (a job still waited on).
const out = value => process.stdout.write(JSON.stringify(value), () => process.exit(0));

/* ------------------------------------------------------------ commands -- */
if (command === "pure") {
  const shell = await load("shell.js");
  const copyMod = await load("copy.js");
  const reg = shell.createRegistry();
  reg.registerPage("overview", {render() {}, nav: {label: "研究总览", order: 10}});
  reg.registerPage("company", {render() {}, params: ["company", "tab"], parent: "companies"});
  const state = shell.createState(reg);
  const resolved = [
    state.href({page: "company", company: "ACN"}),
    (state._commit(state.fromHash("#/company/ACN/model?aspect=demand_drivers")), state.href({tab: "events"})),
    state.href({company: "MSFT"}),
    state.href({page: "overview"}),
    state.href({filters: {sort: "ret", dir: "asc"}}),
  ];
  const errors = [];
  for (const bad of [["Bad Id", {render() {}}], ["ok", {}]]) { try { reg.registerPage(...bad); } catch (e) { errors.push(e.message); } }
  try { reg.registerWidget("nodot", () => {}); } catch (e) { errors.push(e.message); }
  out({
    parse: (input.hashes || []).map(hh => shell.parseHash(hh)),
    roundtrip: (input.hashes || []).map(hh => shell.buildHash(shell.parseHash(hh))),
    resolved, errors,
    fromHash: state.fromHash("#/company/ACN/model?aspect=x"),
    unknown: state.fromHash("#/nowhere"),
    keys: (input.texts || []).map(t => shell.uiTextKey(t)),
    fmt: (input.fmt || []).map(([name, args]) => shell.fmt[name](...args)),
    copy: (input.copy || []).map(t => copyMod.copy(t)),
    copyTwice: (input.copy || []).map(t => copyMod.copy(copyMod.copy(t)) === copyMod.copy(t)),
    table: copyMod.COPY_TABLE.map(r => ({current: r.current, suggested: r.suggested, note: r.note, mode: r.mode, rules: r.rules.length})),
    gloss: copyMod.glossSegments(input.gloss || "", new Set()),
    debate: copyMod.splitDebateTitle(input.debate || ""),
    slots: shell.SLOTS,
  });
} else if (command === "pages") {
  const doc = new Document_();
  globalThis.document = doc;
  const shell = await load("shell.js");
  const copyMod = await load("copy.js");
  // The browser boot makes append/replaceChildren skip null; so does this DOM.
  shell.tolerateEmptyChildren(Node_.prototype);
  const {h} = shell.makeDom(doc);
  const ui = shell.createUi(doc, h);
  const registry = shell.createRegistry();
  const state = shell.createState(registry);
  const fixtures = input.fixtures || {};
  const posted = [];
  const fetched = [];
  const api = async path => {
    fetched.push(path);
    const key = Object.keys(fixtures).filter(k => path.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (!key) { const e = new Error("没有这个读取"); e.display_message = "暂时无法读取。"; e.status = 400; throw e; }
    return JSON.parse(JSON.stringify(fixtures[key]));
  };
  const replies = input.postReplies || {};
  api.post = async (p, body) => {
    posted.push([p, body]);
    const reply = replies[p];
    const answer = Array.isArray(reply) ? reply.shift() : reply;
    if (answer && answer.error) { const e = new Error(answer.error); e.display_message = answer.error; e.status = answer.status ?? 400; throw e; }
    return answer ? JSON.parse(JSON.stringify(answer)) : {};
  };
  const jobs = [];
  api.job = async (id, onTick, opts = {}) => {
    jobs.push(id);
    for (const t of input.jobTicks || []) if (onTick) onTick(t);
    if (input.jobHang) return new Promise((_, reject) => opts.signal?.addEventListener("abort", () =>
      reject(Object.assign(new Error("stopped"), {cancelled: true, display_message: "已停止等待。"}))));
    if (input.jobError) { const e = new Error(input.jobError); e.display_message = input.jobError; e.body = input.jobErrorBody || null; throw e; }
    return JSON.parse(JSON.stringify(input.jobResult || {}));
  };
  api.clear = () => {};
  api.text = s => s;
  const store = new Map();
  const drafts = shell.createDrafts(() => "test-env", {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k)});
  globalThis.window = {DaltonV2: {registerPage: (id, def) => registry.registerPage(id, def),
                                  registerWidget: (s, r) => registry.registerWidget(s, r), copy: copyMod.copy}};
  for (const [k, v] of Object.entries(input.drafts || {})) store.set(`dalton.v2.draft:test-env:${k}`, typeof v === "string" ? v : JSON.stringify(v));
  for (const f of ["overview", "companies", "ask", "tasks", "settings"]) await load(`pages/${f}.js`);
  const widgetsUsed = [];
  for (const w of input.widgets || (input.widget ? [input.widget] : []))
    registry.registerWidget(w, (el, data) => { widgetsUsed.push([w, ...Object.keys(data)]); el.append(h("p", null, `WIDGET:${w}`)); });
  const results = {};
  const navigations = [];
  const sides = {};
  const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 0)); };
  const find = (root, step) => {
    if (step.id) return doc.getElementById(step.id) || sides[step.page]?.body()?.querySelector(`#${step.id}`);
    const pool = step.dialog ? doc.body.querySelectorAll(`dialog ${step.tag || "button"}`) : [...root.querySelectorAll(step.tag || "button"), ...((sides[step.page]?.body()?.querySelectorAll(step.tag || "button")) || []),
                  ...doc.body.querySelectorAll(`dialog ${step.tag || "button"}`)];
    const hits = pool.filter(b => (step.text == null || b.textContent.trim() === step.text) && (step.attr == null || b.getAttribute(step.attr[0]) === step.attr[1]));
    return hits[step.index || 0] || null;
  };
  for (const [name, hash] of Object.entries(input.render || {})) {
    const s = state.fromHash(hash);
    state._commit(s);
    const def = registry.pages.get(s.page);
    const root = h("div", {id: "page-root"});
    doc.body.replaceChildren(root);
    const ctx = {
      state: s, prev: s, kind: "push", def, api, fmt: shell.fmt, ui, h, say: t => copyMod.copy(api.text(t)), copy: copyMod.copy,
      drafts, side: (() => {
        let el = null, closing = null;
        const sideApi = {
          open: o => { el = h("div", {id: "side-body"}); closing = o.onClose || null; o.render && o.render(el); results[`${name}:side`] = el.textContent; results[`${name}:sideHeading`] = o.heading; },
          body: () => el, isOpen: () => Boolean(el),
          close: silent => { el = null; const c = closing; closing = null; if (c && !silent) c(); },
        };
        sides[name] = sideApi;
        return sideApi;
      })(),
      seen: new Set(), manifest: {pages: []}, alive: () => true,
      slot: async (slotName, el, data, fallback) => {
        const list = registry.widgets.get(slotName) || [];
        for (const r of list) { if ((await r(el, data, ctx)) !== false) return true; }
        if (fallback) await fallback(el);
        el.setAttribute("data-slot-fallback", slotName);
        return false;
      },
      navigate(patch, options) { navigations.push([name, patch, options || null]); }, href: patch => state.href(patch), setFilter() {}, setBadge() {}, announce() {},
      requestId: shell.requestId, adminHref: () => (input.admin ? "/v2/admin" : null), refresh() { navigations.push([name, "refresh", null]); },
    };
    await def.render(root, ctx);
    await settle();
    if (input.event && input.event.page === name) root.dispatch(input.event.type, {detail: input.event.detail});
    const stepLog = [];
    for (const step of (input.steps || []).filter(x => x.page === name)) {
      if (step.op === "wait") { await settle(); stepLog.push("wait"); continue; }
      const el = find(root, step);
      if (!el) { stepLog.push(`missing:${step.id || step.text}`); continue; }
      if (step.op === "set") { el.value = step.value; el.dispatch("input"); el.dispatch("change"); }
      else if (step.op === "check") { el.checked = step.checked !== false; el.dispatch("change"); }
      else if (step.op === "file") { el.files = [{name: step.name, size: step.size ?? (step.bytes_b64 ? Buffer.from(step.bytes_b64, "base64").length : 10),
                                               arrayBuffer: async () => { const b = Buffer.from(step.bytes_b64 || "", "base64"); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); }}];
                                     el.dispatch("change"); }
      else if (step.op === "click") el.dispatch("click");
      else if (step.op === "submit") el.dispatch("submit");
      else if (step.op === "close") sides[name]?.close();
      await settle();
      stepLog.push(`${step.op}:${step.id || step.text}`);
    }
    if (stepLog.length) {
      const side = sides[name]?.body();
      results[`${name}:after`] = {
        text: root.textContent, side: side ? side.textContent : null, steps: stepLog,
        dialog: doc.body.querySelectorAll("dialog").map(d => d.textContent).join("\n") || null,
        states: root.querySelectorAll("[data-state]").map(e => e.getAttribute("data-state")),
        focused: doc.activeElement?.id || doc.activeElement?.textContent?.slice(0, 40) || null,
        invalid: [...root.querySelectorAll("[aria-invalid]"), ...(side ? side.querySelectorAll("[aria-invalid]") : [])].map(e => e.id),
        drafts: Object.fromEntries([...store.entries()].map(([k, v]) => [k.replace("dalton.v2.draft:test-env:", ""), v])),
        pressed: [...root.querySelectorAll("button"), ...(side ? side.querySelectorAll("button") : [])].filter(b => b.getAttribute("aria-pressed") === "true").map(b => b.textContent),
      };
    }
    results[name] = {
      text: root.textContent,
      h1: root.querySelector("h1")?.textContent || null,
      h2: root.querySelectorAll("h2").map(x => x.textContent),
      documentLists: root.querySelectorAll("article ul").map(x => x.children.map(li => li.textContent)),
      sections: root.querySelectorAll("section").map(x => x.id),
      toc: root.querySelectorAll(".v2-toc a").map(a => [a.textContent, a.getAttribute("href")]),
      buttons: root.querySelectorAll("button").map(b => b.textContent),
      links: root.querySelectorAll("a").map(a => [a.textContent, a.getAttribute("href")]),
      fallbacks: root.querySelectorAll("[data-slot-fallback]").map(x => x.getAttribute("data-slot-fallback")),
      labels: root.querySelectorAll("label").map(l => [l.textContent, l.getAttribute("for")]),
      nullText: /\bnull\b|undefined|\[object Object\]|NaN/.test(root.textContent),
    };
  }
  out({
    pages: [...registry.pages.values()].map(p => ({id: p.id, nav: p.nav, route: p.route, params: p.params, parent: p.parent})),
    nav: registry.navPages().map(p => p.nav.label),
    results, posted, widgetsUsed, navigations, fetched, jobs, drafts: Object.fromEntries([...store.entries()].map(([k, v]) => [k.replace("dalton.v2.draft:test-env:", ""), v])),
  });
} else if (command === "call") {
  // {module, calls: [[name, ...args]]} -> results of the module's pure exports
  globalThis.window = {DaltonV2: {registerPage() {}, registerWidget() {}}};
  const mod = await load(input.module);
  const results = [];
  for (const [name, ...args] of input.calls || []) {
    const fixed = args.map(a => (a && a.__bytes_b64 ? new Uint8Array(Buffer.from(a.__bytes_b64, "base64")) : a));
    try { results.push({ok: await mod[name](...fixed)}); } catch (e) { results.push({error: String(e.message || e)}); }
  }
  out(results);
} else {
  process.stderr.write(`unknown command ${command}\n`);
  process.exit(2);
}
