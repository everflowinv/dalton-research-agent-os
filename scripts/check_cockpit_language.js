#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "src/dalton_core/cockpit_control.html"), "utf8");

const checks = {
  removedOldReviewLink: !html.includes('href="/legacy"') && !html.includes("旧版审阅"),
  exactLocalizationHook: html.includes("Object.prototype.hasOwnProperty.call(UI_TEXT,value)"),
  opaqueClaimCursor: html.includes('q.set("cursor",claimCursor)') && html.includes("r.next_cursor"),
  boundedClaimPage: html.includes('q.set("limit","50")'),
  technicalDetailsCollapsed: html.includes('node("details",null,"technical")'),
  rawCitationPreserved: html.includes('rawNode("div",x.statement)'),
  unknownEnumsClosed: html.includes('enumLabel(s,"状态暂不可读")'),
  askWaitExplained: html.includes("通常约需一分钟"),
  approvalDedupeRequiresKey: html.includes("sameActions&&item.dedupe_key"),
  budgetInputsLabelled: html.includes('input.setAttribute("aria-label",label)'),
  eventEmptyState: html.includes("目前没有可核实的新事件"),
};
const failed = Object.entries(checks).filter(([, value]) => !value).map(([key]) => key);
if (failed.length) {
  process.stderr.write(JSON.stringify({checks, failed}) + "\n");
  process.exit(1);
}
process.stdout.write(JSON.stringify({checks, failed: []}) + "\n");
