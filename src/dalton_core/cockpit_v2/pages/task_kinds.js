/* What each kind of "needs a person" item asks of the analyst (D01-D06). */

import {copy, LABELS} from "../copy.js";

const G = LABELS.task_group, U = LABELS.urgency;

/* T02 (2026-10-09): research decisions apart from materials and reports
   (中文定稿 is report upkeep, not a research decision), administrator issues
   and what the system is already doing. */
export const GROUPS = [
  {id: "decision", title: G.decision},
  {id: "material", title: G.material},
  {id: "service", title: G.service},
  {id: "processing", title: G.processing},
];

/* The priority in words (T02 / 第 13 节), by the server's urgency number. */
export const URGENCY_WORDS = {1: U["1"], 2: U["2"], 3: U["3"], 4: U["4"], 5: U["5"]};
export function urgencyLabel(it) {
  if (it && it.kind === "no_active_mission") return U.not_started;   // O09: not started is not blocked
  return URGENCY_WORDS[it?.urgency] || copy(String(it?.urgency_label || "")) || U["4"];
}

const missingList = d => (d.items || []).map(x => `${x.label}${(x.missing_periods || []).length ? `（缺 ${x.missing_periods.join("、")}）` : ""}`).join("；");

export const KINDS = {
  no_active_mission: {group: "decision", entry: "goal", suggest: () => "写下要研究的行业、公司范围和问题，确认后开始研究。",
                      impact: () => "设定研究目标后即可开始研究。"},
  gate_decision: {group: "decision", entry: "approval", suggest: () => "初步立项审核：审阅初筛结论，决定是否启动深度财务模型构建。"},
  memo_decision: {group: "decision", entry: "approval", suggest: () => "投资备忘录签发审核：审阅备忘录，决定是否形成正式观点。"},
  scenario_decision: {group: "decision", entry: "approval", suggest: () => "宏观与经营情景假设待确认：审阅情景与概率后确认。"},
  reopen_proposal: {group: "decision", entry: "approval", suggest: () => "决定是否重新打开这一研究阶段。"},
  governance_record: {group: "decision", entry: "admin", suggest: () => "这项决定需要管理员核对记录后处理。"},
  controlled_recovery: {group: "decision", entry: "recovery",
                        suggest: d => d.admission_ref ? `这项专项课题已停下（${copy(d.reason_label || "需要恢复")}）。决定是恢复专项课题研究，还是暂缓当前专项课题。` : "以下专项课题已停下，请逐项决定。"},
  data_foundation_stalled: {group: "material", entry: "material",
                            suggest: d => `补齐资料：${missingList(d) || "见说明"}。补传资料或调整检索方式目前由管理员处理。`},
  fetch_failure_capped: {group: "material", entry: "material", suggest: () => "确认这份资料是否仍需要；需要的话，请管理员更换来源或人工取回原文。"},
  material_budget_exceeded: {group: "decision", entry: "material",
                            suggest: d => `决定是否追加材料获取额度${d.approve_usd ? ` ${d.approve_usd} 美元` : ""}${d.approve_calls ? `（约 ${d.approve_calls} 次检索）` : ""}；也可以保留现有额度。`},
  source_not_connected: {group: "material", entry: "admin", suggest: () => "本项目需要的资料来源尚未连接，请管理员连接。"},
  earnings_release_unread: {group: "processing", entry: "company", suggest: () => "无需操作：申报数据入库后会自动补上。"},
  model_coverage_gap: {group: "processing", entry: "company", suggest: () => "查看模型缺少哪些条件；无需审批，模型发布后提醒自动消失。"},
  gate_auto_returned: {group: "processing", entry: "company", suggest: () => "无需操作，仅供知悉。"},
  publication_route_blocked: {group: "service", entry: "admin",
                              suggest: d => d.chain_now_independent ? "AI 服务选择已调整，等待下一次每日自动重试，无需操作。" : "AI 服务设置需要调整，请交给管理员处理。",
                              impact: d => d.products ? `${d.products} 份研究报告暂时只有草稿，看不到定稿中文。` : null},
  model_cooldown: {group: "service", entry: "admin", suggest: () => "一项 AI 服务暂时不可用，请管理员确认备用服务。"},
  provider_rate_limited: {group: "service", entry: "admin", suggest: () => "AI 服务调用受限，请管理员处理。"},
  raw_spool_capacity: {group: "service", entry: "admin", suggest: () => "资料存储空间快用完，请管理员整理空间。"},
  connector_login_expired: {group: "service", entry: "admin", suggest: () => "资料来源的登录已过期，请管理员重新登录。"},
  publication_parked: {group: "material", entry: "admin",
                       suggest: d => d.group === "ui_text_batches"
                         ? `${d.count || "多"} 批中文译稿可以一次重新生成，也可以逐批处理或标记已阅知悉。`
                         : "中文定稿校对暂未完成：可以在这里重新生成中文定稿，或标记已阅知悉。",
                       impact: d => (d.group === "ui_text_batches" ? "这些短句在研究页上暂时显示英文；其他研究不受影响。" : null)},
};
const DEFAULT_KIND = {group: "service", entry: "admin", suggest: () => "请交给管理员处理。"};
export const kindOf = it => KINDS[it.kind] || (it.urgency >= 5 || it.actionable === false ? {group: "processing", entry: "company", suggest: () => "无需操作。"} : DEFAULT_KIND);

/** Impact first, then urgency, then newest (section 7.2: 影响与紧急程度排序). */
export function sortTasks(items) {
  const weight = {decision: 0, material: 1, service: 2, processing: 3};
  return items.slice().sort((a, b) => (a.urgency ?? 9) - (b.urgency ?? 9)
    || weight[kindOf(a).group] - weight[kindOf(b).group]
    || String(b.at || "").localeCompare(String(a.at || "")));
}


/** Drop an aggregate recovery card whose tasks are listed one by one (D05). */
export function foldTasks(items) {
  const lanes = new Set(items.filter(x => x.kind === "controlled_recovery" && x.detail?.admission_ref).map(x => x.detail.lane));
  return items.filter(x => !(x.kind === "controlled_recovery" && !x.detail?.admission_ref && lanes.has(x.detail?.lane)));
}

/** Technical upkeep remains visible, but is never an analyst decision. */
const BACKGROUND_ROLES = new Set(["administrator", "background", "optional_feedback"]);
export function approvalRequired(it) {
  return it.decision_required !== false && !BACKGROUND_ROLES.has(it.responsibility);
}
export function analystAction(it) {
  if (it.actionable === false || !approvalRequired(it)) return false;
  if (it.decision_required === true) return true;
  const k = kindOf(it);
  return k.group === "decision" || (k.group === "material" && k.entry !== "admin");
}

/** What the 待我处理 badge counts: actual analyst actions and approvals. */
export function actionCount(items, approvals = []) {
  return foldTasks(items || []).filter(analystAction).length + (approvals || []).filter(approvalRequired).length;
}
