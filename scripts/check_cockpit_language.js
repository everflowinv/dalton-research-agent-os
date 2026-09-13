#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "src/dalton_core/cockpit_control.html"), "utf8");
const vm = require('vm');
const gateCode = ['displayText', 'finalResearchText'].map(name =>
  html.split('\n').find(line => line.startsWith(`const ${name}=`))).join('\n');
const gateContext = { UI_TEXT: { '表达已经清楚': '表达已经清楚', 'old': '已修改' }, FINAL_RESEARCH_REQUIRED: true };
vm.createContext(gateContext);
vm.runInContext(gateCode + '\nthis.showFinal=finalResearchText;', gateContext);

const checks = {
  removedOldReviewLink: !html.includes('href="/legacy"') && !html.includes("旧版审阅"),
  exactLocalizationHook: html.includes("Object.prototype.hasOwnProperty.call(UI_TEXT,value)"),
  unchangedApprovedTextVisible: gateContext.showFinal('表达已经清楚') === '表达已经清楚',
  changedApprovedTextVisible: gateContext.showFinal('old') === '已修改',
  absentReviewWaits: gateContext.showFinal('未经审查') === '正文正在检查文字表达，完成后会显示。',
  opaqueClaimCursor: html.includes('q.set("cursor",claimCursor)') && html.includes("r.next_cursor"),
  boundedClaimPage: html.includes('q.set("limit","50")'),
  technicalDetailsCollapsed: html.includes('node("details",null,"technical")'),
  normalizedCitationLocalized: html.includes('shown=displayText(x.statement)') &&
    html.includes('technicalDetails({original_statement:x.statement})'),
  unknownEnumsClosed: html.includes('enumLabel(s,"状态暂不可读")'),
  askWaitExplained: html.includes("通常约需一分钟"),
  approvalDedupeRequiresKey: html.includes("sameActions&&item.dedupe_key"),
  budgetInputsLabelled: html.includes('input.setAttribute("aria-label",label)'),
  readableFinancialMetrics: html.includes("function formatDisplayMetric") && html.includes("1e8") && html.includes("per_share") && html.includes("toFixed(1)"),
  eventEmptyState: html.includes("目前没有可核实的新事件"),
};
const failed = Object.entries(checks).filter(([, value]) => !value).map(([key]) => key);
if (failed.length) {
  process.stderr.write(JSON.stringify({checks, failed}) + "\n");
  process.exit(1);
}
process.stdout.write(JSON.stringify({checks, failed: []}) + "\n");
