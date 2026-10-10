/* Cockpit v2 shell (ADR-0016): navigation, URL state, data access, formatting
   and the page/widget registry every v2 module goes through.

   window.DaltonV2 = {
     registerPage(id, {title, nav, render, route, params, parent, update}),
     registerWidget(slot, render),
     api(path), fmt, state, ...helpers documented in ADR-0016
   }

   Pure helpers are exported for node checks; the browser boot only runs when a
   document with #v2-main exists. */

import {copy, UI, glossSegments, enumText} from "./copy.js";
import {actionCount} from "./pages/task_kinds.js";

export const SLOTS = Object.freeze([
  "overview.header", "overview.compare",
  "company.summary", "company.model", "company.valuation", "company.returns", "company.scenarios",
  "settings.admin_link",
]);

/* ------------------------------------------------------------------ URL -- */

/** "#/company/ACN/model?aspect=x" -> {page, segments, filters}. */
export function parseHash(hash) {
  let text = String(hash || "");
  if (text.startsWith("#")) text = text.slice(1);
  const q = text.indexOf("?");
  const path = q < 0 ? text : text.slice(0, q);
  const search = q < 0 ? "" : text.slice(q + 1);
  const segments = path.split("/").filter(Boolean).map(s => {
    try { return decodeURIComponent(s); } catch (_e) { return s; }
  });
  const filters = {};
  for (const [k, v] of new URLSearchParams(search)) if (k) filters[k] = v;
  return {page: segments[0] || "", segments: segments.slice(1), filters};
}

/** Inverse of parseHash; filters are written in sorted key order. */
export function buildHash({page, segments = [], filters = {}}) {
  const path = [page || "overview", ...segments.filter(s => s != null && s !== "")]
    .map(s => encodeURIComponent(String(s))).join("/");
  const keys = Object.keys(filters).filter(k => filters[k] != null && filters[k] !== "").sort();
  const search = new URLSearchParams(keys.map(k => [k, String(filters[k])])).toString();
  return "#/" + path + (search ? "?" + search : "");
}

/* --------------------------------------------------------------- state -- */

/** The URL-backed state: environment, page, company, tab, filters. */
export function createState(registry) {
  let current = {environment: null, page: "overview", company: null, tab: null, filters: {}};
  const listeners = new Set();
  const paramsOf = page => (registry.pages.get(page)?.params) || [];
  const api = {
    get() { return {...current, filters: {...current.filters}}; },
    /** Resolve a patch against the current state.  Changing the page clears
        company/tab/filters not named; changing the company clears tab and
        filters not named. */
    resolve(patch = {}) {
      const next = {...current, filters: {...current.filters}};
      if (patch.page !== undefined && patch.page !== current.page) {
        next.page = patch.page; next.company = null; next.tab = null; next.filters = {};
      }
      if (patch.company !== undefined && patch.company !== current.company) {
        next.company = patch.company; next.tab = null; next.filters = {};
      }
      if (patch.tab !== undefined) next.tab = patch.tab;
      if (patch.filters !== undefined) next.filters = {...patch.filters};
      if (patch.environment !== undefined) next.environment = patch.environment;
      return next;
    },
    toHash(s = current) {
      const def = registry.pages.get(s.page);
      const route = def?.route || s.page;
      const segments = paramsOf(s.page).map(name => s[name]);
      while (segments.length && (segments[segments.length - 1] == null)) segments.pop();
      return buildHash({page: route, segments, filters: s.filters});
    },
    fromHash(hash) {
      const parsed = parseHash(hash);
      let id = null;
      for (const [pid, def] of registry.pages) if ((def.route || pid) === parsed.page) { id = pid; break; }
      if (!id) id = registry.defaultPage || "overview";
      const out = {environment: current.environment, page: id, company: null, tab: null, filters: parsed.filters};
      paramsOf(id).forEach((name, i) => { out[name] = parsed.segments[i] ?? null; });
      return out;
    },
    href(patch) { return api.toHash(api.resolve(patch)); },
    _commit(next) {
      const prev = current; current = next;
      for (const fn of listeners) { try { fn(api.get(), prev); } catch (e) { console.warn(e); } }
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    // Filled in by the browser boot; harmless no-ops elsewhere.
    set(patch, options) { api._commit(api.resolve(patch)); return options; },
    setFilter(key, value, options = {replace: true}) {
      const filters = {...current.filters};
      if (value == null || value === "") delete filters[key]; else filters[key] = String(value);
      return api.set({filters}, options);
    },
  };
  return api;
}

/* ------------------------------------------------------------ registry -- */

export function createRegistry() {
  const pages = new Map();
  const widgets = new Map();
  const reg = {
    pages, widgets, defaultPage: "overview", onChange: null,
    registerPage(id, def) {
      if (typeof id !== "string" || !/^[a-z][a-z0-9_-]*$/.test(id)) throw new Error("page id must be a lowercase word");
      if (!def || typeof def.render !== "function") throw new Error(`page ${id} needs render()`);
      const nav = typeof def.nav === "string" ? {label: def.nav, order: 100} : (def.nav || null);
      pages.set(id, {id, title: def.title || id, nav, render: def.render, update: def.update || null,
                     route: def.route || id, params: def.params || [], parent: def.parent || null});
      if (reg.onChange) reg.onChange("page", id);
      return id;
    },
    registerWidget(slot, render) {
      if (typeof slot !== "string" || !slot.includes(".")) throw new Error("widget slot is <page>.<name>");
      if (typeof render !== "function") throw new Error("widget render must be a function");
      if (!widgets.has(slot)) widgets.set(slot, []);
      widgets.get(slot).push(render);
      if (reg.onChange) reg.onChange("widget", slot);
      return slot;
    },
    navPages() {
      return [...pages.values()].filter(p => p.nav).sort((a, b) => (a.nav.order ?? 100) - (b.nav.order ?? 100));
    },
  };
  return reg;
}

/* ---------------------------------------------------------- formatting -- */
/* Ports of numeric_display.format_display_number / format_typed_value
   (tests/test_cockpit_v2_shell.py checks the two agree). */

function toNumber(value) {
  if (value == null || value === "" || typeof value === "boolean") return null;
  const n = Number(String(value).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
}
/** Decimal ROUND_HALF_UP to `places`, as a plain string. */
export function roundHalfUp(n, places) {
  const [int, frac = ""] = Math.abs(n).toFixed(Math.min(places + 6, 20)).split(".");
  let digits = (int + frac.slice(0, places)).split("").map(Number);
  if (Number(frac[places] || 0) >= 5) {
    let i = digits.length - 1;
    while (i >= 0) { if (digits[i] === 9) { digits[i] = 0; i--; } else { digits[i]++; break; } }
    if (i < 0) digits.unshift(1);
  }
  let text = digits.join("");
  if (places > 0) text = text.slice(0, text.length - places).padStart(1, "0") + "." + text.slice(text.length - places);
  text = text.replace(/^0+(?=\d)/, "");
  const zero = /^[0.]+$/.test(text);
  return (n < 0 && !zero ? "-" : "") + text;
}
function adjusted(n) { return n === 0 ? 0 : Math.floor(Math.log10(Math.abs(n)) + 1e-12); }
function scaledAmount(n, divisor, label) {
  const value = n / divisor;
  const places = Math.max(0, Math.min(2, 3 - adjusted(value)));
  return `${roundHalfUp(value, places)} ${label}`;
}
function displayNumber(n, kind) {
  if (kind === "amount_usd") {
    const m = Math.abs(n);
    if (m >= 1e8) return scaledAmount(n, 1e8, "亿美元");
    if (m >= 1e4) return scaledAmount(n, 1e4, "万美元");
    return `${roundHalfUp(n, 2)} 美元`;
  }
  if (kind === "percent") return `${roundHalfUp(n, 1)}%`;
  if (kind === "eps") return `${roundHalfUp(n, 2)} 美元/股`;
  if (kind === "arpu") return `${roundHalfUp(n, 2)} 美元/用户`;
  throw new Error("unsupported display number kind");
}
const SCALE = {one: 1, base: 1, unit: 1, thousand: 1e3, million: 1e6, billion: 1e9};
const SCALE_LABEL = {thousand: "千", million: "百万", billion: "十亿"};

function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (_e) { return "UTC"; }
}
function parts(date, zone) {
  const f = new Intl.DateTimeFormat("en-CA", {timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
                                             hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
  const o = {};
  for (const p of f.formatToParts(date)) o[p.type] = p.value;
  return o;
}

export const fmt = {
  zone: browserZone(),
  number(value, digits = 0) {
    const n = toNumber(value); if (n == null) return "—";
    return Number(roundHalfUp(n, digits)).toLocaleString("zh-CN", {minimumFractionDigits: digits, maximumFractionDigits: digits});
  },
  /** format_display_number(kind="amount_usd") */
  money(value) { const n = toNumber(value); return n == null ? "—" : displayNumber(n, "amount_usd"); },
  /** A small USD amount (costs, prices): US$ with two decimals. */
  usd(value) { const n = toNumber(value); return n == null ? "—" : `US$${fmt.number(n, 2)}`; },
  percent(value) { const n = toNumber(value); return n == null ? "—" : displayNumber(n, "percent"); },
  /** Signed percent, sign always shown: "+7.2%" / "-21.7%". */
  change(value) { const n = toNumber(value); if (n == null) return "—"; const t = displayNumber(n, "percent"); return n > 0 ? "+" + t : t; },
  /** Percentage points: "+6.2 个百分点". */
  pp(value) { const n = toNumber(value); if (n == null) return "—"; const t = roundHalfUp(n, 1); return `${n > 0 ? "+" : ""}${t} 个百分点`; },
  eps(value) { const n = toNumber(value); return n == null ? "—" : displayNumber(n, "eps"); },
  ratio(value) { const n = toNumber(value); return n == null ? "—" : `${roundHalfUp(n, 1)} 倍`; },
  /** A fraction (0.1046) shown as a percent (10.5%). */
  fraction(value) { const n = toNumber(value); return n == null ? "—" : displayNumber(n * 100, "percent"); },
  probability(value) { const n = toNumber(value); return n == null ? "—" : `${roundHalfUp(n * 100, 0)}%`; },
  /** format_typed_value */
  typed(value, {unit = "", scale = null, currency = null, metric = ""} = {}) {
    const n = toNumber(value);
    if (n == null) return value == null || value === "" ? "—" : String(value);
    const scaleName = String(scale || "one").toLowerCase();
    const unitName = String(unit || "").toLowerCase().replace(/-/g, "_");
    if (!(scaleName in SCALE)) return [value, currency, unit, scale].filter(Boolean).join(" ");
    const dollar = String(currency || "").toUpperCase() === "USD" || ["usd", "usd_per_share", "usd_per_user"].includes(unitName);
    if (dollar && (["per_share", "currency_per_share", "usd_per_share"].includes(unitName)
                   || ["eps", "earnings_per_share", "diluted_eps"].includes(String(metric).toLowerCase())))
      return displayNumber(n, "eps");
    if (dollar && (["per_user", "currency_per_user", "usd_per_user"].includes(unitName) || String(metric).toLowerCase() === "arpu"))
      return displayNumber(n, "arpu");
    if (["percent", "percentage", "%"].includes(unitName)) return displayNumber(n, "percent");
    if (dollar && ["usd", "currency", "monetary", "dollars", ""].includes(unitName)) return displayNumber(n * SCALE[scaleName], "amount_usd");
    return [value, currency, unit, ["one", "base"].includes(scaleName) ? "" : scale].filter(Boolean).join(" ");
  },
  /** Table header for a stated unit: "金额：百万美元". */
  unitHeader(scale, currency) {
    const cur = String(currency || "").toUpperCase() === "USD" ? "美元" : (currency || "");
    return `金额：${SCALE_LABEL[String(scale || "").toLowerCase()] || ""}${cur}`;
  },
  /** A calendar date.  A date-only value is printed as given (no shift). */
  date(value) {
    if (!value) return "—";
    const text = String(value);
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    const d = new Date(text); if (Number.isNaN(d.getTime())) return text;
    const p = parts(d, fmt.zone); return `${p.year}-${p.month}-${p.day}`;
  },
  /** Date and time in the reader's own time zone (shown once in the status bar). */
  datetime(value) {
    if (!value) return "—";
    const d = new Date(String(value)); if (Number.isNaN(d.getTime())) return String(value);
    const p = parts(d, fmt.zone); return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  },
  ago(value, now = Date.now()) {
    const d = new Date(String(value || "")); if (Number.isNaN(d.getTime())) return "时间未知";
    const s = Math.max(0, Math.round((now - d.getTime()) / 1000));
    if (s < 60) return "刚刚";
    if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
    return `${Math.round(s / 86400)} 天前`;
  },
};

/* ------------------------------------------------------ service pill -- */

/** What the top-bar service pill says, from GET /v1/cockpit/log (pure).
    「暂停」 only when the scheduler has not ticked for 15 minutes or the
    service is stopping; a fresh tick with lanes in error is 「正常，但有 N 项
    后台任务异常」 linking to the administrator's 健康与故障 (2026-10-10). */
export const SERVICE_STALE_MS = 15 * 60 * 1000;
export function serviceStatus(log, nowMs = Date.now(), healthHref = null) {
  const value = log || {};
  const tick = value.last_tick_at || null;
  const state = value.service_state;
  if (state === "awaiting_mission")
    return {kind: "awaiting_mission", text: UI.awaitingMission, tone: "", title: UI.awaitingMissionHelp, href: null};
  const at = tick ? new Date(tick).getTime() : NaN;
  const fresh = Number.isFinite(at) && nowMs - at < SERVICE_STALE_MS;
  const lanes = Array.isArray(value.lanes_in_error) ? value.lanes_in_error : [];
  const ago = tick ? fmt.ago(tick, nowMs) : "";
  if (fresh && state === "starting")
    return {kind: "starting", text: UI.serviceStarting, tone: "warn", title: UI.serviceStartingHelp, href: null};
  if (fresh && ["running", "degraded"].includes(state)) {
    if (state === "degraded" || lanes.length) {
      const n = Math.max(1, lanes.length);
      const names = lanes.map(l => (l && (l.label || l.key)) || "").filter(Boolean).join("、") || "后台调度";
      return {kind: "degraded", text: UI.serviceDegraded(n, !!healthHref), tone: "warn",
              title: UI.serviceDegradedHelp(ago, names), href: healthHref || null};
    }
    return {kind: "running", text: UI.serviceOnlineSince(ago), tone: "ok", title: UI.serviceOnlineHelp, href: null};
  }
  return {kind: "paused", text: UI.serviceOffline, tone: "warn",
          title: UI.serviceOfflineHelp(tick ? `（最近一次运行：${ago}）` : ""), href: null};
}

/* ------------------------------------------------------- UI text keys -- */
/* Reviewed translations are keyed by the first 16 hex digits of SHA-256 over
   the exact UTF-8 string -- the same key /v1/cockpit/ui-texts and the old
   cockpit use, so every reviewed translation applies unchanged here. */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
const ENCODER = new TextEncoder();
export function uiTextKey(text) {
  const bytes = ENCODER.encode(text), n = bytes.length, total = ((n + 72) >> 6) << 6;
  const m = new Uint8Array(total); m.set(bytes); m[n] = 0x80;
  const dv = new DataView(m.buffer);
  dv.setUint32(total - 8, Math.floor(n / 0x20000000)); dv.setUint32(total - 4, (n * 8) >>> 0);
  const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const w = new Uint32Array(64);
  const r = (x, k) => (x >>> k) | (x << (32 - k));
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15], b = w[i - 2];
      w[i] = (w[i - 16] + (r(a, 7) ^ r(a, 18) ^ (a >>> 3)) + w[i - 7] + (r(b, 17) ^ r(b, 19) ^ (b >>> 10))) >>> 0;
    }
    let [A, B, C, D, E, F, G, H] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (H + (r(E, 6) ^ r(E, 11) ^ r(E, 25)) + ((E & F) ^ (~E & G)) + K[i] + w[i]) >>> 0;
      const t2 = ((r(A, 2) ^ r(A, 13) ^ r(A, 22)) + ((A & B) ^ (A & C) ^ (B & C))) >>> 0;
      H = G; G = F; F = E; E = (D + t1) >>> 0; D = C; C = B; B = A; A = (t1 + t2) >>> 0;
    }
    h[0] = (h[0] + A) >>> 0; h[1] = (h[1] + B) >>> 0; h[2] = (h[2] + C) >>> 0; h[3] = (h[3] + D) >>> 0;
    h[4] = (h[4] + E) >>> 0; h[5] = (h[5] + F) >>> 0; h[6] = (h[6] + G) >>> 0; h[7] = (h[7] + H) >>> 0;
  }
  return h[0].toString(16).padStart(8, "0") + h[1].toString(16).padStart(8, "0");
}

export function collectStrings(value, out = new Set(), depth = 0) {
  if (out.size >= 20000 || depth > 40) return out;
  if (typeof value === "string") { if (value && value.length <= 20000) out.add(value); }
  else if (Array.isArray(value)) { for (const x of value) collectStrings(x, out, depth + 1); }
  else if (value && typeof value === "object") {
    for (const k of Object.keys(value)) if (k !== "csrf_token" && k !== "content_base64") collectStrings(value[k], out, depth + 1);
  }
  return out;
}

/* ------------------------------------------------------------- data ---- */

export function createApi({fetchImpl, now = () => Date.now(), ttlMs = 30000} = {}) {
  const cache = new Map();
  const TEXT = new Map();
  const resolved = new Set();
  const listeners = new Set();
  let csrf = "", revision;
  const doFetch = (...args) => (fetchImpl || globalThis.fetch)(...args);

  function failure(status, body, fallback) {
    const message = status === 403 ? "登录状态已变化，请刷新页面后再试。"
      : (body && body.display_message) || fallback
      || (status >= 500 ? "服务暂时无法读取，请稍后重试。" : "请求没有成功，请刷新后重试。");
    const err = new Error(message);
    err.status = status; err.display_message = message; err.body = body;
    return err;
  }

  async function resolveTexts(payload) {
    if (!payload || typeof payload !== "object") return;
    if (Object.prototype.hasOwnProperty.call(payload, "text_localization_revision")
        && payload.text_localization_revision !== revision) {
      revision = payload.text_localization_revision; resolved.clear();
    }
    const byKey = new Map();
    for (const text of collectStrings(payload)) {
      const key = uiTextKey(text);
      if (resolved.has(key)) continue;
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(text);
    }
    const keys = [...byKey.keys()];
    const deadline = now() + 15000;
    for (let i = 0; i < keys.length && now() < deadline; i += 800) {
      const batch = keys.slice(i, i + 800), chunks = [];
      for (let j = 0; j < batch.length; j += 200) chunks.push(batch.slice(j, j + 200));
      const answers = await Promise.all(chunks.map(chunk =>
        doFetch(`/v1/cockpit/ui-texts?keys=${chunk.join(",")}`, {headers: {Accept: "application/json"}, cache: "no-store"})
          .then(r => (r.ok ? r.json() : null)).catch(() => null).then(r => [chunk, r])));
      for (const [chunk, r] of answers) {
        if (!r || typeof r !== "object") continue;
        const texts = r.texts && typeof r.texts === "object" ? r.texts : {};
        const later = new Set(Array.isArray(r.deferred) ? r.deferred : []);
        for (const key of chunk) {
          if (later.has(key)) continue;
          resolved.add(key);
          if (typeof texts[key] === "string") for (const t of byKey.get(key)) TEXT.set(t, texts[key]);
        }
      }
    }
  }

  async function get(path, {fresh = false, timeoutMs = 120000} = {}) {
    const hit = cache.get(path);
    if (!fresh && hit && now() - hit.at < ttlMs) return hit.promise;
    const promise = (async () => {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
      let response;
      try {
        response = await doFetch(path, {headers: {Accept: "application/json"}, cache: "no-store", signal: controller?.signal});
      } catch (_e) {
        throw failure(0, null, "网络连接中断或读取超时，请稍后重试。");
      } finally { if (timer) clearTimeout(timer); }
      const body = await response.json().catch(() => null);
      if (!response.ok) throw failure(response.status, body);
      if (body && body.csrf_token) csrf = body.csrf_token;
      if (path.startsWith("/v1/") && !path.startsWith("/v1/cockpit/ui-texts")) await resolveTexts(body).catch(() => {});
      for (const fn of listeners) { try { fn(path, body); } catch (e) { console.warn(e); } }
      return body;
    })();
    cache.set(path, {at: now(), promise});
    promise.catch(() => { if (cache.get(path)?.promise === promise) cache.delete(path); });
    return promise;
  }

  async function post(path, value) {
    if (!csrf) await get("/v2/data/manifest", {fresh: true}).catch(() => {});
    let response;
    try {
      response = await doFetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Dalton-CSRF": csrf},
                                      body: JSON.stringify(value)});
    } catch (_e) {
      throw failure(0, null, "网络连接中断，没有确认是否已提交；请刷新查看结果后再决定是否重试。");
    }
    const body = await response.json().catch(() => null);
    if (!response.ok) throw failure(response.status, body);
    cache.clear();
    if (path.startsWith("/v1/")) await resolveTexts(body).catch(() => {});
    return body;
  }

  /* Poll a job until it is done.  `signal` (an AbortSignal) stops the
     waiting on this page only: the job itself keeps running on the server,
     which has no cancel, and its result lands in the history (MUSE P0 #1). */
  async function job(id, onTick, {intervalMs = 1800, maxMs = 600000, signal = null} = {}) {
    const started = now();
    const stopped = () => {
      const err = failure(0, null, "已转入后台继续分析。结果生成后会自动归档到历史记录，不会重复处理。");
      err.cancelled = true;
      return err;
    };
    for (;;) {
      if (signal?.aborted) throw stopped();
      await new Promise(r => {
        const t = setTimeout(r, intervalMs);
        signal?.addEventListener?.("abort", () => { clearTimeout(t); r(); }, {once: true});
      });
      if (signal?.aborted) throw stopped();
      const j = await get(`/v1/cockpit/job?id=${encodeURIComponent(id)}`, {fresh: true});
      if (onTick) onTick(j);
      if (j.status === "done") return j.result;
      if (j.status === "failed") throw failure(200, {...j, display_message: j.display_message || "处理没有完成。"});
      if (now() - started > maxMs) throw failure(0, null, "仍在处理中。结果完成后会出现在历史记录里。");
    }
  }

  const api = (path, options) => get(path, options);
  Object.assign(api, {
    get, post, job,
    text: s => (typeof s === "string" && TEXT.has(s) ? TEXT.get(s) : s),
    onResponse(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    clear() { cache.clear(); },
    _texts: TEXT,
  });
  return api;
}

export function requestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return Math.random().toString(16).slice(2) + Date.now().toString(16);
}

/* -------------------------------------------------------------- DOM ---- */

export function makeDom(doc) {
  /** h("p", {class: "x", on: {click}}, "text", child...) */
  function h(tag, attrs, ...children) {
    const el = doc.createElement(tag);
    if (attrs && (typeof attrs !== "object" || attrs.nodeType || Array.isArray(attrs))) { children.unshift(attrs); attrs = null; }
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else if (k === "hidden") el.hidden = true;
      else if (k === "value") el.value = v;
      else if (k === "disabled") el.disabled = true;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(typeof c === "object" ? c : doc.createTextNode(String(c)));
    }
    return el;
  }
  return {h, append};
}

export function pausedResearchDialog(doc, h, data, trigger = doc.activeElement) {
  const close = h("button", {type: "button", class: "v2-btn v2-btn-secondary", autofocus: true}, "关闭只读视图");
  const articles = (data.products || []).map(product => h("article", null,
    h("h3", null, product.display_label || product.label || product.kind || "研究成果"),
    (product.sections || []).map(section => h("section", null, h("h4", null, section.title || ""), h("p", null, section.body || "")))));
  const body = h("div", {class: "v2-dialog-body"}, h("p", null, "研究服务已停止。这里仅显示保存的研究正文。"), articles);
  const dialog = h("dialog", {class: "v2-dialog", "aria-label": `${data.name}的已发表研究`},
    h("h2", null, `${data.name}（已暂停 · 只读）`), body, h("div", {class: "v2-actions"}, close));
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { dialog.remove(); if (trigger?.isConnected) trigger.focus(); }, {once: true});
  doc.body.append(dialog); dialog.showModal();
  return dialog;
}

let dialogSerial = 0;

/** The shared confirmation dialog (also for the administrator and chart
    pages: `confirmDialog(document, {...})`).  A native modal <dialog> where
    the browser has one (focus stays inside, Escape cancels), an inline
    dialog otherwise; focus returns to the control that opened it. */
export function confirmDialog(doc, {title, body = [], confirmLabel = "确认", cancelLabel = "取消", tone = "primary",
                                    reason = null, extra = null, validate = null, typeToConfirm = null} = {}, parts = null) {
  const h = parts?.h || makeDom(doc).h;
  const ui = parts?.ui || createUi(doc, h);
  return new Promise(resolve => {
    const opener = doc.activeElement;
    const id = `v2-dlg-${++dialogSerial}`;
    const field = reason ? ui.field({id: `${id}-reason`, label: reason.label || (reason.required ? "理由" : "补充说明（选填）"),
      required: Boolean(reason.required), maxLength: reason.max || 600, help: reason.help || null, attrs: {rows: "2"}}) : null;
    const reasonBox = field ? (reason.required ? field.wrap
      : h("details", {class: "v2-more"}, h("summary", null, "补充说明（选填）"), field.wrap)) : null;
    const error = h("div", {class: "v2-field-error", role: "alert"});
    // A high-impact action (administrator) asks for a word to be typed.
    const need = typeToConfirm == null ? "" : String(typeToConfirm);
    const typed = need ? ui.field({id: `${id}-type`, label: `输入「${need}」以确认`, control: "input", attrs: {autocomplete: "off", spellcheck: "false"}}) : null;
    const ok = ui.button(confirmLabel, null, {variant: tone === "danger" ? "danger" : "primary"});
    const cancel = ui.button(cancelLabel, null, {variant: "quiet"});
    const paragraphs = [body].flat().filter(Boolean).map(x => (typeof x === "string" ? h("p", null, x) : x));
    const dlg = h("dialog", {class: "v2-dialog", id, "aria-labelledby": `${id}-title`, "aria-describedby": `${id}-body`},
      h("h2", {id: `${id}-title`}, title || "请确认"), h("div", {id: `${id}-body`, class: "v2-dialog-body"}, paragraphs),
      extra, typed ? typed.wrap : null, reasonBox, error, h("div", {class: "v2-actions"}, ok, cancel));
    let done = false;
    const finish = value => {
      if (done) return;
      done = true;
      try { dlg.close?.(); } catch (_e) { /* already closed */ }
      dlg.remove();
      if (opener && opener.focus && (!doc.contains || doc.contains(opener))) opener.focus();
      resolve(value);
    };
    ok.addEventListener("click", () => {
      if (field && !field.check()) { field.input.focus(); return; }
      if (typed && String(typed.input.value || "").trim() !== need) { error.textContent = `请输入「${need}」以确认。`; typed.input.focus(); return; }
      const message = validate ? validate() : "";
      if (message) { error.textContent = message; return; }
      finish({confirmed: true, reason: field ? String(field.input.value || "").trim() : ""});
    });
    cancel.addEventListener("click", () => finish({confirmed: false, reason: ""}));
    dlg.addEventListener("cancel", e => { e.preventDefault(); finish({confirmed: false, reason: ""}); });
    dlg.addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); finish({confirmed: false, reason: ""}); } });
    doc.body.append(dlg);
    if (typeof dlg.showModal === "function") { try { dlg.showModal(); } catch (_e) { dlg.setAttribute("open", ""); } }
    else dlg.setAttribute("open", "");
    // A dangerous action starts on 「取消」; a required reason starts in its field.
    (typed ? typed.input : field && reason.required ? field.input : tone === "danger" ? cancel : ok).focus?.();
  });
}

/** The building blocks every page uses (tables, fields, alerts ...). */
export function createUi(doc, h) {
  const ui = {
    h,
    section(id, title, ...children) {
      return h("section", {class: "v2-section", id, "aria-labelledby": `${id}-title`},
               h("h2", {id: `${id}-title`, tabindex: "-1"}, title), ...children);
    },
    empty(text) { return h("p", {class: "v2-empty"}, text); },
    note(text) { return h("p", {class: "v2-note"}, text); },
    tag(text, tone) { return h("span", {class: "v2-tag", "data-tone": tone || null}, text); },
    alert(text, tone = "warn", ...children) { return h("div", {class: "v2-alert", "data-tone": tone, role: tone === "danger" ? "alert" : null}, text, ...children); },
    link(text, href, attrs = {}) { return h("a", {href, ...attrs}, text); },
    button(text, onClick, {variant = "primary", ...attrs} = {}) {
      const cls = {primary: "v2-btn", secondary: "v2-btn v2-btn-secondary", quiet: "v2-btn v2-btn-quiet", danger: "v2-btn v2-btn-danger"}[variant] || "v2-btn";
      return h("button", {type: "button", class: cls, on: onClick ? {click: onClick} : null, ...attrs}, text);
    },
    kv(pairs) {
      const dl = h("dl", {class: "v2-kv"});
      for (const [k, v] of pairs) if (v != null && v !== "") dl.append(h("dt", null, k), h("dd", null, v));
      return dl;
    },
    /** A data table: columns [{key, label, num, render(row)}]; first column is a row header. */
    table({caption, columns, rows, rowHeader = true, sort = null}) {
      const thead = h("thead", null, h("tr", null, columns.map(c => {
        const th = h("th", {scope: "col", class: c.num ? "v2-num" : null}, c.header ? c.header() : c.label);
        if (sort && sort.key === c.key) th.setAttribute("aria-sort", sort.dir === "asc" ? "ascending" : "descending");
        return th;
      })));
      const tbody = h("tbody", null, rows.map(row => h("tr", null, columns.map((c, i) => {
        const value = c.render ? c.render(row) : row[c.key];
        const tag = rowHeader && i === 0 ? "th" : "td";
        return h(tag, {scope: tag === "th" ? "row" : null, class: c.num ? "v2-num" : null}, value ?? "—");
      }))));
      return h("div", {class: "v2-table-wrap", role: "region", "aria-label": caption || "数据表", tabindex: "0"},
               h("table", {class: "v2-table"}, caption ? h("caption", null, caption) : null, thead, tbody));
    },
    /** Persistent label, help text and inline error for one input (G10). */
    field({id, label, help, control = "textarea", required = false, maxLength, validate, attrs = {}}) {
      const input = h(control, {id, name: id, "aria-describedby": `${id}-help ${id}-error`, maxlength: maxLength, required: required || null, ...attrs});
      const error = h("div", {class: "v2-field-error", id: `${id}-error`, "aria-live": "polite"});
      const wrap = h("div", {class: "v2-field"}, h("label", {for: id}, label, required ? h("span", {class: "v2-note"}, "（必填）") : null),
                     help ? h("div", {class: "v2-help", id: `${id}-help`}, help) : h("span", {id: `${id}-help`, hidden: true}),
                     input, error);
      const setError = message => {
        error.textContent = message || "";
        wrap.setAttribute("data-invalid", message ? "true" : "false");
        if (message) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
      };
      const check = () => {
        const value = String(input.value || "").trim();
        let message = "";
        if (required && !value) message = `请填写「${label}」。`;
        else if (maxLength && value.length > maxLength) message = `「${label}」最多 ${maxLength} 个字，现在 ${value.length} 个。`;
        else if (validate) message = validate(value) || "";
        setError(message);
        return !message;
      };
      input.addEventListener("input", () => { if (wrap.getAttribute("data-invalid") === "true") check(); });
      return {wrap, input, setError, check};
    },
    receipt(text, tone) { return h("div", {class: "v2-receipt", role: "status", "data-tone": tone || null}, text); },
    /** Technical information an administrator may need (codes, refs):
        always collapsed, never the first thing an analyst reads (G10). */
    technical(...children) {
      return h("details", {class: "v2-more v2-tech"}, h("summary", null, UI.technical), ...children);
    },
    /** One empty / error / waiting state for every page (G12): 当前状态 → 影响
        → 谁处理 → 我能做什么, with the action as a button. */
    state({status, impact, owner, next, actions = [], tone = "info", technical = null, heading = null} = {}) {
      const rows = [["当前状态", status], ["影响", impact], ["谁处理", owner], ["我能做什么", next]].filter(([, v]) => v);
      const buttons = actions.filter(Boolean).map(a => (a.href
        ? h("a", {class: a.variant === "secondary" ? "v2-btn v2-btn-secondary" : "v2-btn", href: a.href, "aria-label": a.ariaLabel || null}, a.label)
        : ui.button(a.label, a.onClick, {variant: a.variant || "primary", "aria-label": a.ariaLabel || null})));
      return h("div", {class: "v2-state", "data-tone": tone, role: tone === "danger" ? "alert" : "status"},
        heading ? h("p", {class: "v2-state-head"}, heading) : null,
        h("dl", {class: "v2-state-grid"}, rows.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
        buttons.length ? h("div", {class: "v2-actions"}, buttons) : null,
        technical ? ui.technical(technical) : null);
    },
    errorBox(err, retry) {
      const raw = (err && err.display_message) || "暂时无法读取。";
      // A message with no Chinese in it is a system string: shown only under
      // the technical details (G10 / financial 5.2).
      const english = !/[㐀-鿿]/.test(raw);
      const status = english ? "暂时无法读取（服务返回了一条系统说明）" : raw;
      const offline = !err || !err.status || err.status >= 500;
      return ui.state({tone: "danger", status,
        impact: "这一部分暂时看不到；页面其他内容不受影响。",
        owner: err && err.status === 403 ? "需要你刷新页面重新登录。" : offline ? "系统会自动恢复；持续出现请联系管理员。" : "需要你调整后再试。",
        next: retry ? "点「重试」重新读取。" : UI.retryLater,
        actions: retry ? [{label: "重试", onClick: retry}] : [],
        technical: english || (err && err.status) ? [english ? h("p", {lang: "en"}, raw) : null, err && err.status ? h("p", null, `HTTP ${err.status}`) : null] : null});
    },
    /** A closed-vocabulary value: its label, or 「状态未知」 with the code under
        the technical details (G10). */
    enumValue(labels, value) {
      const t = enumText(labels, value);
      if (t.known) return h("span", null, t.text);
      return h("span", {class: "v2-enum-unknown"}, t.text, " ", ui.technical(h("p", null, `代码：${t.code}`)));
    },
    /** A confirmation dialog with the consequence spelled out (MUSE P0 #2,
        T10).  Resolves {confirmed, reason}; `reason` {required, label, help,
        max} adds a reason field -- collapsed and optional unless required. */
    confirm(options) { return confirmDialog(doc, options, {h, ui}); },
    /** Text with finance terms glossed on first use in this page. */
    glossed(text, seen) {
      const span = h("span");
      for (const part of glossSegments(text, seen)) {
        span.append(part.term ? h("abbr", {title: part.gloss}, part.text, h("span", {class: "v2-visually-hidden"}, `（${part.gloss}）`)) : doc.createTextNode(part.text));
      }
      return span;
    },
    paragraphs(text, seen) {
      return String(text || "").split(/\n{2,}|\n/).map(p => p.trim()).filter(Boolean).map(p => h("p", null, seen ? ui.glossed(p, seen) : p));
    },
    download(payload) {
      const bytes = Uint8Array.from(atob(payload.content_base64), c => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], {type: payload.media_type || "application/octet-stream"}));
      const a = h("a", {href: url, download: payload.filename || "dalton-download"});
      doc.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    },
  };
  return ui;
}

/* ------------------------------------------------------------ browser -- */

const SCROLL_KEY = "dalton.v2.scroll";
const DRAFT_PREFIX = "dalton.v2.draft";

function store() {
  try { return globalThis.sessionStorage || null; } catch (_e) { return null; }
}

export function createDrafts(getEnvironment, storage = store()) {
  const key = object => `${DRAFT_PREFIX}:${getEnvironment() || "unknown"}:${object}`;
  const drafts = {
    get(object) { try { return storage?.getItem(key(object)) ?? null; } catch (_e) { return null; } },
    set(object, value) {
      try { if (value == null || value === "") storage?.removeItem(key(object)); else storage?.setItem(key(object), value); } catch (_e) { /* full */ }
    },
    clear(object) { drafts.set(object, null); },
    getJson(object) { try { return JSON.parse(drafts.get(object) || "null"); } catch (_e) { return null; } },
    setJson(object, value) { drafts.set(object, value == null ? null : JSON.stringify(value)); },
    /** Keep a field's text across re-renders, refreshes and page changes. */
    bind(input, object) {
      const held = drafts.get(object);
      if (held != null && !input.value) input.value = held;
      input.addEventListener("input", () => drafts.set(object, input.value));
      return input;
    },
  };
  return drafts;
}

/* Pages build optional parts as `cond ? node : null`; on this page
   append/prepend/replaceChildren skip null, undefined and false instead of
   printing "null".  Only the v2 document is affected. */
export function tolerateEmptyChildren(proto) {
  for (const name of ["append", "prepend", "replaceChildren"]) {
    const native = proto[name];
    if (typeof native !== "function" || native.v2Tolerant) continue;
    const wrapped = function (...nodes) { return native.apply(this, nodes.filter(n => n != null && n !== false)); };
    wrapped.v2Tolerant = true;
    proto[name] = wrapped;
  }
}

function boot() {
  const doc = document;
  tolerateEmptyChildren(Element.prototype);
  if (typeof DocumentFragment === "function") tolerateEmptyChildren(DocumentFragment.prototype);
  const {h} = makeDom(doc);
  const registry = createRegistry();
  const state = createState(registry);
  const api = createApi();
  const drafts = createDrafts(() => state.get().environment);
  const main = doc.getElementById("v2-main");
  const live = doc.getElementById("v2-live");
  let manifest = {pages: [], modules: [], shell_modules: []};
  let renderToken = 0, currentKey = null, pendingPush = null, booted = false, lastRendered = null;
  const badges = new Map();

  const say = s => copy(api.text(s));
  const announce = text => { if (live) { live.textContent = ""; setTimeout(() => { live.textContent = text; }, 30); } };

  /* scroll positions per URL (G04): first visit -> top; back/forward -> restore. */
  const scrolls = (() => { try { return JSON.parse(store()?.getItem(SCROLL_KEY) || "{}"); } catch (_e) { return {}; } })();
  let holdScroll = false;  // while a page is replaced, the clamped scroll is not a reading position
  const saveScroll = () => {
    if (!currentKey || holdScroll) return;
    scrolls[currentKey] = window.scrollY;
    try { store()?.setItem(SCROLL_KEY, JSON.stringify(scrolls)); } catch (_e) { /* ignore */ }
  };
  let scrollTimer = null;
  window.addEventListener("scroll", () => { clearTimeout(scrollTimer); scrollTimer = setTimeout(saveScroll, 150); }, {passive: true});

  const ui = createUi(doc, h);

  /* side panel for sources and originals (G11, G08).  Beside the page on a
     wide screen it is a non-modal split: the page stays usable.  Where it
     overlays the page (<= 1280px) it is a modal dialog: the page behind is
     inert, Tab stays inside, Escape closes; at <= 768px it fills the screen.
     The close button says where it goes back to; focus returns to the
     control that opened it. */
  const side = (() => {
    const panel = doc.getElementById("v2-side"), body = doc.getElementById("v2-side-body");
    const title = doc.getElementById("v2-side-title"), close = doc.getElementById("v2-side-close");
    const behind = () => [doc.querySelector(".v2-rail"), doc.querySelector(".v2-top"), main].filter(Boolean);
    const overlay = () => Boolean(window.matchMedia && window.matchMedia("(max-width:1280px)").matches);
    let opener = null, onClose = null, modal = false;
    const setModal = on => {
      modal = on;
      if (on) { panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); }
      else { panel.removeAttribute("role"); panel.removeAttribute("aria-modal"); }
      for (const el of behind()) { el.inert = on; if (on) el.setAttribute("aria-hidden", "true"); else el.removeAttribute("aria-hidden"); }
    };
    const api_ = {
      /* onClose runs once when the panel closes (button, Escape, or a page
         change), so a page can drop the panel's hash parameter. */
      open({heading = "事实底稿与引用出处", render, returnLabel = null, onClose: closing = null} = {}) {
        if (panel.hidden || !doc.contains(opener)) opener = doc.activeElement;
        onClose = closing;
        const where = registry.pages.get(state.get().page)?.title || "";
        title.textContent = heading;
        close.textContent = returnLabel || UI.closePanel;
        close.setAttribute("aria-label", returnLabel || (where ? `${UI.closePanel}，${UI.backTo(where)}` : UI.closePanel));
        body.replaceChildren();
        if (render) render(body);
        panel.hidden = false;
        setModal(overlay());
        title.focus();
      },
      close(silent = false) {
        if (panel.hidden) return;
        panel.hidden = true; body.replaceChildren();
        setModal(false);
        if (opener && doc.contains(opener)) opener.focus();
        opener = null;
        const after = onClose; onClose = null;
        if (after && !silent) { try { after(); } catch (e) { console.warn(e); } }
      },
      body: () => body,
      isOpen: () => !panel.hidden,
      isModal: () => modal,
    };
    close.addEventListener("click", () => api_.close());
    // Loading a source replaces the clicked control; the browser can move
    // focus to body. Keep modal keyboard controls working during that swap.
    doc.addEventListener("keydown", e => {
      if (panel.hidden || e.defaultPrevented || e.target?.closest?.("dialog[open]")) return;
      if (!modal && !panel.contains(doc.activeElement)) return;
      if (e.key === "Escape") { e.preventDefault(); api_.close(); return; }
      if (e.key !== "Tab" || !modal) return;
      const items = [...panel.querySelectorAll("a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex='-1'])")]
        .filter(el => !el.closest("[hidden]"));
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (!panel.contains(doc.activeElement)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
      else if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    // A window resized across the breakpoint switches between split and overlay.
    window.matchMedia?.("(max-width:1280px)")?.addEventListener?.("change", () => { if (!panel.hidden) setModal(overlay()); });
    return api_;
  })();

  /* widgets: slot -> renderers registered by other modules, else fallback */
  async function slot(name, container, data, fallback, ctx) {
    const renderers = registry.widgets.get(name) || [];
    for (const render of renderers) {
      try {
        const done = await render(container, data, ctx);
        if (done !== false) { container.setAttribute("data-slot", name); return true; }
      } catch (e) { console.warn(`widget ${name} failed`, e); container.replaceChildren(); }
    }
    if (fallback) await fallback(container);
    container.setAttribute("data-slot-fallback", name);
    return false;
  }

  /* navigation: a glyph tile beside each label, as on the first cockpit */
  const NAV_ICONS = {overview: "◎", companies: "▦", ask: "？", tasks: "✓", settings: "⚙"};
  function renderNav() {
    const list = doc.getElementById("v2-nav-list");
    const s = state.get();
    const active = registry.pages.get(s.page);
    const activeNav = active?.parent || s.page;
    list.replaceChildren(...registry.navPages().map(p => {
      const count = badges.get(p.id);
      const a = h("a", {href: state.toHash({page: p.id, company: null, tab: null, filters: {}}),
                        "aria-current": p.id === activeNav ? "page" : null,
                        "aria-label": count ? `${p.nav.label}，${UI.badge(count)}` : null, title: p.nav.label},
                  h("span", {class: "v2-nav-ic", "aria-hidden": "true"}, NAV_ICONS[p.id] || "•"),
                  h("span", {class: "v2-nav-label"}, p.nav.label),
                  count ? h("span", {class: "v2-badge", "aria-hidden": "true", title: UI.badge(count)}, String(count)) : null);
      return h("li", null, a);
    }));
  }

  function navigate(patch, {replace = false} = {}) {
    const next = state.resolve(patch);
    const hash = state.toHash(next);
    if (replace) {
      history.replaceState(history.state, "", hash);
      route("replace");
    } else if (hash === location.hash) {
      route("push");
    } else {
      pendingPush = hash;
      location.hash = hash;
    }
  }
  state.set = (patch, options = {}) => navigate(patch, options);

  async function route(kind) {
    if (kind !== "replace") { clearTimeout(scrollTimer); saveScroll(); }
    const next = state.fromHash(location.hash);
    next.environment = state.get().environment;
    const prev = state.get();
    state._commit(next);
    const key = location.hash;
    currentKey = key;
    renderNav();
    const def = registry.pages.get(next.page);
    if (!def) return;
    doc.title = `${def.title} · ${UI.product}`;
    if (side.isOpen() && (prev.page !== next.page || prev.company !== next.company)) side.close(true);
    const ctx = makeCtx(def, next, prev, kind);
    // The same page and object: let the page update in place (tab/filter).
    if (lastRendered && lastRendered.page === next.page && lastRendered.company === next.company && def.update) {
      const handled = await def.update(ctx);
      if (handled !== false) {
        if (kind === "pop" && scrolls[key] != null && !next.tab) window.scrollTo(0, scrolls[key]);
        return;
      }
    }
    const token = ++renderToken;
    const restoreTo = kind === "pop" ? scrolls[key] : null;
    const section = h("div", {class: "v2-page", "data-page": next.page});
    holdScroll = true;
    main.replaceChildren(section);
    lastRendered = {page: next.page, company: next.company};
    const busy = h("p", {class: "v2-loading v2-page-busy"}, "正在读取…");
    main.setAttribute("aria-busy", "true");
    section.append(busy);
    // Keep the busy line last while the page appends its sections.
    const keepLast = typeof MutationObserver === "function"
      ? new MutationObserver(() => { if (busy.parentNode === section && section.lastChild !== busy) section.append(busy); }) : null;
    keepLast?.observe(section, {childList: true});
    try {
      await def.render(section, ctx);
    } catch (e) {
      console.warn(e);
      if (token === renderToken) section.append(ui.errorBox(e, () => route("replace")));
    } finally {
      keepLast?.disconnect();
      busy.remove();
      if (token === renderToken) { main.removeAttribute("aria-busy"); section.setAttribute("data-ready", "true"); }
    }
    if (token !== renderToken) return;
    // G06: the top bar says when this page was read, not when the data is from.
    setPill("updated", UI.updated(fmt.datetime(new Date().toISOString())), "", `按你的浏览器时区显示（${fmt.zone}）；每项数据自己的截至时间写在数据旁`);
    renderedSignature = latestSignature;
    showFresh(false);
    if (restoreTo != null) window.scrollTo(0, restoreTo);
    else if (kind !== "replace" && !next.tab) window.scrollTo(0, 0);
    setTimeout(() => { if (token === renderToken) holdScroll = false; }, 200);
    if (kind !== "replace") {
      const heading = section.querySelector("h1");
      if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({preventScroll: true}); }
      announce(`已打开${def.title}`);
    }
  }

  function makeCtx(def, s, prev, kind) {
    const token = renderToken + 1;
    const seen = new Set();
    const ctx = {
      state: s, prev, kind, def, api, fmt, ui, h, say, copy, drafts, side, seen, manifest,
      alive: () => renderToken <= token,
      slot: (name, container, data, fallback) => slot(name, container, data, fallback, ctx),
      navigate, href: patch => state.href(patch),
      setFilter: (k, v) => state.setFilter(k, v, {replace: true}),
      setBadge, announce, requestId, refresh: () => refreshPage(),
      adminHref: params => adminHref(params),
    };
    return ctx;
  }

  function adminHref(params = {}) {
    // The administrator page lives at /admin; /v2/admin stays as an alias.
    const pages = manifest.pages || [];
    if (!pages.includes("/admin") && !pages.includes("/v2/admin")) return null;
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== "")).toString();
    return "/admin" + (q ? "?" + q : "");
  }

  function setBadge(pageId, count) {
    if (count) badges.set(pageId, count); else badges.delete(pageId);
    renderNav();
    // The pill and the badge count the same things (T06: counts follow actions).
    if (pageId === "tasks") setPill("blocked", UI.blocked(count || 0), count ? "warn" : "ok", "需要你审阅与决策的事项；点开逐项处理。");
  }

  /* 刷新研究数据 (G07): read everything again and redraw this page, keeping
     what was typed, which parts were open and where the reader was. */
  function snapshot() {
    const values = {};
    for (const el of main.querySelectorAll("input[id],textarea[id],select[id]")) {
      if (el.type === "file" || el.type === "hidden") continue;
      values[el.id] = el.type === "checkbox" || el.type === "radio" ? {checked: el.checked} : {value: el.value};
    }
    const keyOf = d => d.id || d.querySelector("summary")?.textContent?.trim() || null;
    const open = [...main.querySelectorAll("details")].filter(d => d.open).map(keyOf).filter(Boolean);
    return {values, open, y: window.scrollY, focus: doc.activeElement?.id || null};
  }
  function restore(snap) {
    for (const [id, v] of Object.entries(snap.values)) {
      const el = doc.getElementById(id);
      if (!el || !main.contains(el)) continue;
      if ("checked" in v) { if (el.checked !== v.checked) { el.checked = v.checked; el.dispatchEvent(new Event("change", {bubbles: true})); } }
      else if (el.value !== v.value) { el.value = v.value; el.dispatchEvent(new Event("input", {bubbles: true})); }
    }
    const wanted = new Set(snap.open);
    for (const d of main.querySelectorAll("details")) {
      const key = d.id || d.querySelector("summary")?.textContent?.trim();
      if (key && wanted.has(key)) d.open = true;
    }
    window.scrollTo(0, snap.y);
    if (snap.focus) doc.getElementById(snap.focus)?.focus({preventScroll: true});
  }
  let refreshing = false;
  async function refreshPage() {
    if (refreshing) return;
    refreshing = true;
    const button = doc.getElementById("v2-refresh");
    if (button) { button.disabled = true; button.textContent = UI.refreshing; }
    const snap = snapshot();
    try {
      api.clear();
      lastRendered = null;
      await route("replace");
      restore(snap);
      loadStatus();
      announce(UI.refreshed);
    } finally {
      refreshing = false;
      if (button) { button.disabled = false; button.textContent = UI.refresh; }
    }
  }
  /* 有新内容: the newest research event changed since this page was drawn. */
  let latestSignature = null, renderedSignature = null;
  function showFresh(on) {
    const el = doc.getElementById("v2-fresh");
    if (!el) return;
    if (!on) { el.hidden = true; el.replaceChildren(); return; }
    el.hidden = false;
    el.replaceChildren(h("span", null, `${UI.fresh}。`),
      ui.button(UI.freshAction, () => refreshPage(), {variant: "secondary", "aria-label": `${UI.freshAction}（保留已输入的内容和阅读位置）`}));
  }

  /* in-app links push history; back/forward are "pop" */
  doc.addEventListener("click", e => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest("a[href^='#/']");
    if (!a) return;
    e.preventDefault();
    const hash = a.getAttribute("href");
    if (hash === location.hash) { route("push"); return; }
    pendingPush = hash; location.hash = hash;
  });
  window.addEventListener("hashchange", () => {
    const kind = pendingPush === location.hash ? "push" : "pop";
    pendingPush = null;
    route(kind);
  });
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  /* top bar: project switcher and the three separate status pills (G02) */
  const pill = name => doc.querySelector(`[data-status="${name}"]`);
  function setPill(name, text, tone, title) {
    const p = pill(name); if (!p) return;
    p.textContent = text; p.setAttribute("data-tone", tone || "");
    if (title) p.setAttribute("title", title); else p.removeAttribute("title");
  }
  async function loadProjects() {
    const select = doc.getElementById("v2-project-select");
    try {
      const w = await api("/v1/cockpit/workspaces");
      const items = (w && w.enabled !== false && Array.isArray(w.items)) ? w.items : [];
      const current = items.find(x => x.current) || null;
      state._commit({...state.get(), environment: current ? (current.slug || current.workspace_id) : (w.current_workspace_id || "default")});
      // MUSE §1: one project needs no switcher; an unavailable one says why;
      // switching keeps the page (not the company, which may not exist there).
      if (items.length <= 1 && !w.can_create && !items.some(item => item.lifecycle === "paused")) {
        const name = (items[0] && items[0].name) || "项目名称未提供";
        select.replaceWith(h("span", {class: "v2-project-name", id: "v2-project-select"}, name));
        doc.querySelector('label[for="v2-project-select"]')?.removeAttribute("for");
        const hint = doc.getElementById("v2-project-hint");
        if (hint) hint.textContent = "在投研规则与设置中查看当前研究目标";
        return;
      }
      select.replaceChildren(...items.filter(x => !["archived", "deleted"].includes(x.lifecycle)).map(x => h("option", {value: x.lifecycle === "paused" ? `paused:${x.slug}` : x.url || "", selected: x.current || null,
        disabled: (!x.url && x.lifecycle !== "paused") || null},
        (x.name || "未命名项目") + (x.slug === "legacy" ? "（主环境）" : x.current ? "（当前）" : x.lifecycle === "paused" ? "（已暂停 · 只读）" : x.status && x.status !== "running" ? "（暂不可用）" : ""))),
        w.can_create ? h("option", {value: "__create__"}, "新建研究项目…") : null);
      select.addEventListener("change", () => {
        const target = select.value;
        if (!target) return;
        if (target === "__create__") { location.hash = "#/settings?focus=new_project"; return; }
        if (target.startsWith("paused:")) {
          api(`/v1/cockpit/workspace_research?slug=${encodeURIComponent(target.slice(7))}`, {fresh: true}).then(data => {
            select.value = current?.url || "";
            pausedResearchDialog(doc, h, data, select);
          }).catch(error => announce(error.display_message || "只读研究暂时无法读取"));
          return;
        }
        const base = target.replace(/\/+$/, "");
        const page = state.get().page;
        const keep = ["overview", "companies", "ask", "tasks", "settings"].includes(page) ? page : "overview";
        location.href = `${base}/#/${keep}`;
      });
    } catch (_e) {
      select.replaceChildren(h("option", null, "研究项目列表暂时无法读取"));
      select.disabled = true;
      state._commit({...state.get(), environment: state.get().environment || "default"});
    }
  }
  /* The service pill says what it means for the reader and what to do
     (MUSE P0 #4): running / paused / not started / unknown. */
  async function loadStatus() {
    try {
      const log = await api("/v1/cockpit/log?limit=1", {fresh: true});
      const admin = adminHref();
      const shown = serviceStatus(log, Date.now(), admin ? `${admin}#health` : null);
      setPill("service", shown.text, shown.tone, shown.title);
      const p = pill("service");
      if (p && shown.href) p.replaceChildren(h("a", {href: shown.href, class: "v2-pill-link"}, shown.text));
      const newest = (Array.isArray(log.events) && log.events[0]) ? String(log.events[0].id || log.events[0].at || "") : "";
      if (newest) {
        latestSignature = newest;
        if (renderedSignature == null) renderedSignature = newest;
        else if (renderedSignature !== newest) showFresh(true);
      }
    } catch (_e) { setPill("service", UI.serviceUnknown, "warn", "已有研究可以照常查看；稍后会再读取一次。"); }
    try {
      const [n, a] = await Promise.all([api("/v1/cockpit/needs-human", {fresh: true}),
                                        api("/v1/cockpit/approvals", {fresh: true}).catch(() => ({items: []}))]);
      const items = Array.isArray(n.items) ? n.items : [];
      // The pill and the badge count what asks for an action: every item
      // outside 「系统正在处理」 plus every decision waiting on the approvals list (D01).
      setBadge("tasks", actionCount(items, Array.isArray(a.items) ? a.items : []));
    } catch (_e) { setPill("blocked", UI.blockedUnknown, "warn", `稍后会再读取一次；也可以打开「${UI.nav.tasks}」查看上次结果。`); }
  }

  /* 刷新研究数据 and the collapsible navigation (G04). */
  doc.getElementById("v2-refresh")?.addEventListener("click", () => refreshPage());
  const NAV_KEY = "dalton.v2.nav-collapsed";
  const app = doc.querySelector(".v2-app"), toggle = doc.getElementById("v2-rail-toggle");
  const setCollapsed = on => {
    app?.classList.toggle("v2-nav-collapsed", on);
    if (toggle) {
      toggle.setAttribute("aria-expanded", on ? "false" : "true");
      const text = toggle.querySelector(".v2-rail-toggle-text");
      if (text) text.textContent = on ? "展开导航" : "收起导航";
      toggle.setAttribute("aria-label", on ? "展开导航" : "收起导航");
      const ic = toggle.querySelector(".v2-nav-ic");
      if (ic) ic.textContent = on ? "⇥" : "⇤";
    }
    try { globalThis.localStorage?.setItem(NAV_KEY, on ? "1" : "0"); } catch (_e) { /* private mode */ }
  };
  try { setCollapsed(globalThis.localStorage?.getItem(NAV_KEY) === "1"); } catch (_e) { setCollapsed(false); }
  toggle?.addEventListener("click", () => setCollapsed(!app?.classList.contains("v2-nav-collapsed")));

  /* the public object */
  const pub = {
    version: "cockpit-v2-shell-0.1",
    registerPage: (id, def) => registry.registerPage(id, def),
    registerWidget: (slotName, render) => registry.registerWidget(slotName, render),
    api, fmt, state, copy, say, ui, h, side, drafts, slots: SLOTS, requestId,
    navigate, setBadge, announce, adminHref, refresh: () => refreshPage(), confirm: options => ui.confirm(options),
    /* The administrator page's shape: {title, consequence, danger,
       typeToConfirm, confirmLabel} -> true only on an explicit confirm. */
    confirmAction: ({title, consequence, danger = false, typeToConfirm = null, confirmLabel = null} = {}) =>
      ui.confirm({title, body: consequence ? [consequence] : [], tone: danger ? "danger" : "primary", typeToConfirm,
                  confirmLabel: confirmLabel || (danger ? "我已了解后果，确认执行" : "确认")}).then(r => Boolean(r && r.confirmed)),
    get manifest() { return manifest; },
    ready: null,
  };
  registry.onChange = kind => { if (booted && kind === "page") renderNav(); };
  window.DaltonV2 = pub;

  pub.ready = (async () => {
    try { manifest = await api("/v2/data/manifest"); } catch (_e) { manifest = {shell_modules: [], modules: [], pages: []}; }
    await loadProjects();
    for (const href of manifest.styles || []) doc.head.append(h("link", {rel: "stylesheet", href}));
    const urls = [...(manifest.shell_modules || []), ...(manifest.modules || [])];
    for (const url of urls) {
      try { await import(url); } catch (e) { console.warn(`v2 module ${url} failed to load`, e); }
    }
    booted = true;
    if (!location.hash || location.hash === "#" || location.hash === "#/") history.replaceState(null, "", "#/overview");
    await route("push");
    loadStatus();
    setInterval(loadStatus, 120000);
  })();
}

if (typeof document !== "undefined" && document.getElementById && document.getElementById("v2-main")) boot();
