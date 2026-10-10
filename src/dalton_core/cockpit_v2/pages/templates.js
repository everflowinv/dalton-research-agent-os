/* 研究设置 → 报告模板: the fund Excel templates (review N03, T09-T17).
   Lists what is registered and which template each scope uses; uploads a new
   workbook through the existing large-file channel
   POST /v1/cockpit/fund_template_register (base64 in JSON; the server reads
   only the layout, never numbers, formulas or names).  The sheet names are
   read from the chosen file in the browser and offered as choices (T14), the
   template code is generated from the name (T12; editable under 高级).
   Choosing which template a company, industry or the project uses stays where
   it is: the company page's 导出财务模型 and the administrator's 研究项目管理. */

import {LABELS} from "../copy.js";
import {autoReason, said} from "./common.js";
const SLUG = /^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/;
export const REASON_MAX = 600;
const ROLES = [
  {key: "valuation", label: "估值表", hint: /valuation|估值|dcf/i},
  {key: "financials", label: "财务表", hint: /financial|财务|报表|statement/i},
  {key: "driver", label: "关键假设与营运驱动表", hint: /driver|驱动|assumption|假设/i},
];

/** A template code from the name or the file name (pure). */
export function slugFor(name, filename, today = new Date()) {
  const clean = text => String(text || "").toLowerCase().replace(/\.xlsx$/i, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/g, "");
  for (const candidate of [clean(name), clean(filename)]) if (candidate.length >= 3 && SLUG.test(candidate)) return candidate;
  // S15: a name with no Latin letters (「TMT 组模型」 aside) gets the date and
  // a short tail from the name itself, so two uploads on one day differ.
  const d = today.toISOString().slice(0, 10).replace(/-/g, "");
  let tail = 0;
  for (const ch of String(name || filename || "")) tail = (tail * 31 + ch.codePointAt(0)) % 46656;
  return `template-${d}-${tail.toString(36).padStart(3, "0")}`;
}
export function validSlug(slug) { return SLUG.test(String(slug || "")); }

/** Which sheet plays which role, by name (pure). */
export function guessSheets(names) {
  const out = {};
  const used = new Set();
  for (const role of ROLES) {
    const hit = names.find(n => role.hint.test(n) && !used.has(n));
    if (hit) { out[role.key] = hit; used.add(hit); }
  }
  return out;
}

const u16 = (b, o) => b[o] | (b[o + 1] << 8);
const u32 = (b, o) => (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;

async function inflate(bytes) {
  if (typeof DecompressionStream !== "function") throw new Error("no inflate");
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** The worksheet names of an .xlsx (a zip with xl/workbook.xml), in order. */
export async function sheetNames(bytes) {
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) if (u32(bytes, i) === 0x06054b50) { end = i; break; }
  if (end < 0) throw new Error("not a zip");
  const count = u16(bytes, end + 10);
  let p = u32(bytes, end + 16);
  for (let n = 0; n < count && p + 46 <= bytes.length; n++) {
    if (u32(bytes, p) !== 0x02014b50) break;
    const method = u16(bytes, p + 10), size = u32(bytes, p + 20);
    const nameLen = u16(bytes, p + 28), extraLen = u16(bytes, p + 30), commentLen = u16(bytes, p + 32);
    const local = u32(bytes, p + 42);
    const name = new TextDecoder().decode(bytes.subarray(p + 46, p + 46 + nameLen));
    if (name === "xl/workbook.xml") {
      const start = local + 30 + u16(bytes, local + 26) + u16(bytes, local + 28);
      const raw = bytes.subarray(start, start + size);
      const xml = new TextDecoder().decode(method === 0 ? raw : method === 8 ? await inflate(raw) : new Uint8Array());
      const entity = {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'"};
      return [...xml.matchAll(/<(?:\w+:)?sheet\b[^>]*?\bname="([^"]*)"/g)]
        .map(m => m[1].replace(/&(amp|lt|gt|quot|apos);/g, (_x, k) => entity[k]));
    }
    p += 46 + nameLen + extraLen + commentLen;
  }
  throw new Error("no workbook");
}

/** Check the upload form (pure). */
export function checkUpload({file, name, description, sheets, slug, reason}, maxMb) {
  const errors = {};
  if (!file) errors.file = "请选择要上传的 .xlsx 文件";
  else if (!/\.xlsx$/i.test(file.name || "")) errors.file = "只支持 .xlsx 文件（不支持 .xls 或带宏的 .xlsm）";
  else if (maxMb && file.size > maxMb * 1048576) errors.file = `文件超过 ${maxMb} MB 上限`;
  if (!String(name || "").trim()) errors.name = "请给模板起一个名字";
  else if (String(name).trim().length > 80) errors.name = "模板名称最多 80 个字";
  if (String(description || "").trim().length > 600) errors.description = "说明最多 600 个字";
  for (const role of ROLES) if (!String(sheets?.[role.key] || "").trim()) errors[`sheet-${role.key}`] = `请选择${role.label}`;
  const picked = ROLES.map(r => String(sheets?.[r.key] || "").trim()).filter(Boolean);
  if (new Set(picked).size !== picked.length) errors["sheet-driver"] ||= "三张表需要是不同的工作表";
  if (!validSlug(slug)) errors.slug = "系统生成的模板代号不能用，请在「高级」里改成小写字母、数字和连字符（3–48 个字符）";
  // 2026-10-09: the reason is optional (registering changes no export); an
  // empty one is recorded as what the analyst did.
  if (String(reason || "").trim().length > REASON_MAX) errors.reason = `说明最多 ${REASON_MAX} 个字`;
  return errors;
}

function base64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 32768) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 32768));
  return btoa(bin);
}

/** The 报告模板 section body; `view` is GET /v1/cockpit/fund-templates. */
export function templatesPanel(ctx, view, {companyKey = ref => ref} = {}) {
  const {h} = ctx;
  const box = h("div");
  const listHolder = h("div", {id: "templates-list"});
  drawList(ctx, listHolder, view, companyKey);
  // After a registration the list is read again; the form keeps its receipt.
  const onRegistered = async () => {
    try { drawList(ctx, listHolder, await ctx.api("/v1/cockpit/fund-templates", {fresh: true}), companyKey); } catch (_e) { /* the old list stays */ }
  };
  box.append(listHolder, uploadForm(ctx, view, onRegistered));
  return box;
}

function drawList(ctx, holder, view, companyKey) {
  const {h, ui, fmt, say} = ctx;
  const templates = view.templates || [];
  const dflt = view.default || {};
  const companies = view.companies || [];
  holder.replaceChildren(ui.kv([
    ["本项目默认模板", `${say(dflt.template_label || "系统内置模板")}${dflt.chosen_by_owner ? "（你指定）" : "（系统内置）"}`],
    ["模板继承规则", "标的自定义模板 优先于 行业专用模板 优先于 本项目默认模板 优先于 系统内置模板"],
  ]),
  templates.length ? h("details", {class: "v2-more"}, h("summary", null, `已登记的模板（${templates.length}）`),
    ui.table({caption: "登记后的模板在公司页「导出财务分析底稿」里可以选用", columns: [
      {key: "label", label: "模板", render: t => h("span", null, say(t.label), t.is_default ? h("span", null, " ", ui.tag("本项目默认", "ok")) : null, t.is_latest ? null : h("span", null, " ", ui.tag("旧版本")))},
      {key: "origin", label: "来源", render: t => `${say(t.origin_label || "")}${t.source_filename ? `（${t.source_filename}）` : ""}`},
      {key: "at", label: "登记时间", render: t => fmt.date(t.created_at)},
      {key: "used", label: "正在使用的公司", render: t => ((t.used_by || []).length ? t.used_by.map(say).join("、") : "无")},
    ], rows: templates})) : null,
  companies.length ? h("details", {class: "v2-more"}, h("summary", null, "各公司现在使用的模板"),
    ui.table({caption: "要为某家公司改用其他模板，请到该公司页的「导出财务分析底稿」", columns: [
      {key: "c", label: "公司", render: c => h("a", {href: ctx.href({page: "company", company: companyKey(c.company_ref), tab: "model"})}, say(c.label))},
      {key: "t", label: "模板", render: c => say(c.template_label)},
      {key: "b", label: "依据", render: c => say(c.basis_label || "—")},
    ], rows: companies})) : null);
}

function uploadForm(ctx, view, onRegistered) {
  const {h, ui, say} = ctx;
  const maxMb = view.limits?.max_upload_mb || 16;
  const form = h("form", {class: "v2-panel", id: "template-upload", novalidate: true, "aria-labelledby": "template-upload-title"});
  const fileInput = h("input", {type: "file", id: "tpl-file", accept: ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "aria-describedby": "tpl-file-help tpl-file-error"});
  const fileError = h("div", {class: "v2-field-error", id: "tpl-file-error", "aria-live": "polite"});
  const fileWrap = h("div", {class: "v2-field"}, h("label", {for: "tpl-file"}, "模板文件（.xlsx）", h("span", {class: "v2-note"}, "（必填）")),
    h("div", {class: "v2-help", id: "tpl-file-help"}, `上限 ${maxMb} MB。会上传整个文件，但只把版式（表名、行列结构和格式）用作模板；原有的数值和公式不会作为研究数据导入。`), fileInput, fileError);
  const name = ui.field({id: "tpl-name", label: "模板名称", required: true, maxLength: 80, control: "input", help: "例如：TMT 组模型模板"});
  const desc = ui.field({id: "tpl-desc", label: "说明（选填）", maxLength: 600, control: "input"});
  ctx.drafts.bind(name.input, "template:name"); ctx.drafts.bind(desc.input, "template:desc");
  // Sheets: choices read from the file; typed names when the file cannot be read here.
  const sheetBox = h("fieldset", {class: "v2-fieldset", id: "tpl-sheets"}, h("legend", null, "工作簿中对应的三张表"),
    ui.note("选择文件后，这里会列出工作簿里的表名供选择。"));
  const sheetControls = {};
  const sheetErrors = {};
  const drawSheets = (names, chosen = true) => {
    const guess = guessSheets(names || []);
    const fields = ROLES.map(role => {
      const id = `tpl-sheet-${role.key}`;
      const control = names && names.length
        ? h("select", {id, "aria-describedby": `${id}-error`}, h("option", {value: ""}, "请选择"),
            names.map(n => h("option", {value: n, selected: guess[role.key] === n || null}, n)))
        : h("input", {type: "text", id, maxlength: "31", "aria-describedby": `${id}-error`, placeholder: `${role.label}的表名`});
      sheetControls[role.key] = control;
      sheetErrors[role.key] = h("div", {class: "v2-field-error", id: `${id}-error`, "aria-live": "polite"});
      return h("div", {class: "v2-field"}, h("label", {for: id}, role.label), control, sheetErrors[role.key]);
    });
    sheetBox.replaceChildren(h("legend", null, "工作簿中对应的三张表"),
      names && names.length ? ui.note(`已从文件里读到 ${names.length} 张表；系统按表名猜了一个对应关系，请核对。`)
        : ui.note(chosen ? "这个浏览器没能从文件里读出表名，请照工作簿里的表名填写。" : "选择文件后，这里会列出工作簿里的表名供选择；也可以直接照工作簿里的表名填写。"),
      h("div", {class: "v2-grid"}, fields));
  };
  const inds = (view.industries || []).map(i => {
    const input = h("input", {type: "checkbox", value: i.value, id: `tpl-ind-${i.value}`});
    return h("label", {class: "v2-check", for: input.id}, input, ` ${say(i.label)}`);
  });
  const indBox = inds.length ? h("fieldset", {class: "v2-fieldset"}, h("legend", null, "建议用于哪些行业（选填）"),
    ui.note("只作为说明，不会自动应用；要真正用于某个行业，请管理员在「研究项目管理」里指定。"), h("div", {class: "v2-check-row"}, inds)) : null;
  // 高级: the template code, generated unless the analyst picks an existing one.
  const slugSelect = ui.field({id: "tpl-slug-mode", label: "登记为", control: "select",
    help: "选已有模板时，这次上传成为它的新版本；原版本保留。"});
  const slugsSeen = [...new Map((view.templates || []).filter(t => t.origin !== "builtin").map(t => [t.slug, t])).values()];
  slugSelect.input.append(h("option", {value: ""}, "新模板（自动生成代号）"),
    ...slugsSeen.map(t => h("option", {value: t.slug}, `「${say(t.display_name || t.label)}」的新版本`)));
  const slug = ui.field({id: "tpl-slug", label: "模板代号", control: "input", maxLength: 48,
    help: "系统按文件名自动生成，一般不用改；只能用小写字母、数字和连字符。"});
  const advanced = h("details", {class: "v2-more"}, h("summary", null, "高级：登记为新版本，或修改系统生成的代号"), slugSelect.wrap, slug.wrap);
  const autoSlug = () => { if (!slugSelect.input.value && !slug.input.dataset?.edited) slug.input.value = slugFor(name.input.value, fileInput.files?.[0]?.name); };
  slugSelect.input.addEventListener("change", () => { if (slugSelect.input.value) slug.input.value = slugSelect.input.value; else autoSlug(); });
  slug.input.addEventListener("input", () => { if (slug.input.dataset) slug.input.dataset.edited = "1"; });
  name.input.addEventListener("input", autoSlug);
  const reason = ui.field({id: "tpl-reason", label: "说明（选填）", maxLength: REASON_MAX, control: "input",
    help: "可以写一句为什么登记这份模板；会和登记记录一起存档。"});
  ctx.drafts.bind(reason.input, "template:reason");
  const status = h("div", {role: "status", "aria-live": "polite"});
  const submit = h("button", {type: "submit", class: "v2-btn"}, "上传并登记");
  fileInput.addEventListener("change", async () => {
    fileError.textContent = "";
    const file = fileInput.files?.[0];
    autoSlug();
    if (!file) { drawSheets(null, false); return; }
    try { drawSheets(await sheetNames(new Uint8Array(await file.arrayBuffer()))); }
    catch (_e) { drawSheets(null); }
  });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const file = fileInput.files?.[0] || null;
    const sheets = Object.fromEntries(ROLES.map(r => [r.key, String(sheetControls[r.key]?.value || "").trim()]));
    const errors = checkUpload({file, name: name.input.value, description: desc.input.value, sheets, slug: slug.input.value, reason: reason.input.value}, maxMb);
    fileError.textContent = errors.file || "";
    name.setError(errors.name); desc.setError(errors.description); slug.setError(errors.slug); reason.setError(errors.reason);
    for (const r of ROLES) if (sheetErrors[r.key]) sheetErrors[r.key].textContent = errors[`sheet-${r.key}`] || "";
    if (errors.slug) advanced.open = true;
    if (Object.keys(errors).length) {
      status.replaceChildren(ui.receipt("有几项没有填好，已在旁边标出。", "danger"));
      const first = errors.file ? fileInput : errors.name ? name.input : ROLES.some(r => errors[`sheet-${r.key}`]) ? sheetControls[ROLES.find(r => errors[`sheet-${r.key}`]).key] || fileInput
        : errors.slug ? slug.input : errors.reason ? reason.input : desc.input;
      first?.focus?.();
      return;
    }
    submit.disabled = true;
    status.replaceChildren(ui.receipt("正在读取版式并登记，大文件可能需要一分钟…"));
    const requestId = ctx.drafts.get("template:request") || ctx.requestId();
    ctx.drafts.set("template:request", requestId);
    try {
      const out = await ctx.api.post("/v1/cockpit/fund_template_register", {
        filename: file.name, content_base64: base64(new Uint8Array(await file.arrayBuffer())),
        template_slug: slug.input.value.trim(), display_name: name.input.value.trim(), description: desc.input.value.trim(),
        sheets, industries: inds.map(l => l.querySelector("input")).filter(i => i.checked).map(i => i.value),
        reason: reason.input.value.trim() || autoReason("研究设置页", "登记报告模板", name.input.value.trim()), request_id: requestId});
      for (const k of ["template:name", "template:desc", "template:reason", "template:request"]) ctx.drafts.clear(k);
      status.replaceChildren(ui.receipt(`${say(out?.message || "已登记").replace(/[。.!！]+$/, "")}。要让某家公司使用它，请在该公司页「${LABELS.page_title.model_export}」里指定；项目或行业默认由管理员在「研究项目管理」里指定。`));
      ctx.announce && ctx.announce("模板已登记");
      onRegistered && onRegistered(out);
    } catch (err) {
      if (err.status !== 0) ctx.drafts.clear("template:request");
      status.replaceChildren(ui.receipt(`没有登记：${said(err)}。`, "danger"));
    } finally { submit.disabled = false; }
  });
  drawSheets(null, false);
  autoSlug();
  form.append(h("h3", {id: "template-upload-title", tabindex: "-1"}, "上传新模板"),
    ui.note("登记本身不会改变任何公司的导出；指定给公司、行业或本项目之后才会使用。"),
    fileWrap, h("div", {class: "v2-grid"}, name.wrap, desc.wrap), sheetBox, indBox,
    h("details", {class: "v2-more"}, h("summary", null, "补充说明（选填）"), reason.wrap), advanced,
    h("div", {class: "v2-actions"}, submit), status);
  return form;
}
