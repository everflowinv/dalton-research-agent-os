#!/bin/zsh
# 核验车道独立运行 + recheck 同义去重 + 核验 v3（分支 fix/support-lane-independent，2026-09-27）：owner 步骤。
#
# 用法（默认只读，不写任何东西）：
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-support-lane-owner-steps.sh            # = status
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-support-lane-owner-steps.sh status
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-support-lane-owner-steps.sh replay
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-support-lane-owner-steps.sh second-opinion off
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-support-lane-owner-steps.sh second-opinion on
#
#   status          只读：ws-7d 抽取车道的 latest.json、各 mission 版本下滞留的 awaiting review、
#                   按 v3 规则会被复核的已退役 claim 数，以及两个环境的核验设置文件。
#   replay          只读：(1) recheck 同义去重回放（legacy）；(2) v3 双家族核验回放（本地 mock，不调模型）。
#   second-opinion  写设置文件：在两个环境的 claim-support-verification.json 里写
#                   "second_opinion_on_rejection": true/false（保留其它键），下一轮抽取即生效，不用重启。
#                   默认就是 true；当前核验链只有 google-gemini-3 一个能做核验的家族，
#                   所以实际不会发第二次调用（运行摘要会写 unavailable 原因），等你在 cockpit 模型页
#                   给 claim_support_verifier / claim_support_backfill 加入另一家族的模型后自动生效。
#
# 撤回 legacy 上 6 条 recheck 同义重复 claim 另有脚本（默认 dry-run）：
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/withdraw-recheck-near-duplicates-2026-09-27.sh

WT=~/Projects/dalton-research-agent-os
BR=${BR:-$HOME/Projects/dalton-wt-supportlane}
[[ -d $BR/src ]] || BR=$WT
PY=$WT/.venv/bin/python
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
CMD=${1:-status}

status() {
  echo "== ws-7d 抽取车道 latest.json"
  cat "$W/extractions/latest.json"; echo
  echo "== ws-7d 各版本 awaiting review（只读）"
  python3 - "$W" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}/core.sqlite?mode=ro", uri=True)
cur = c.execute("SELECT mission_version_id FROM coverage_mission_pointer").fetchone()[0]
print("当前版本:", cur)
for v, src, n in c.execute(
        "SELECT r.mission_version_ref, r.source_ref, COUNT(*) FROM coverage_mission_document_reviews r "
        "JOIN coverage_mission_versions v ON v.mission_version_id=r.mission_version_ref "
        "WHERE r.state='awaiting_human_extraction' "
        "AND NOT EXISTS (SELECT 1 FROM coverage_mission_discovered_documents n "
        "  JOIN coverage_mission_versions nv ON nv.mission_version_id=n.mission_version_ref "
        "  WHERE nv.mission_ref=v.mission_ref AND nv.version_number>v.version_number "
        "  AND n.document_ref=r.document_ref) GROUP BY 1, 2"):
    print(f"  {'当前' if v == cur else '滞留'} {v.rsplit(':', 1)[-1]} {src}: {n}")
PY
  for S in $L $W; do
    echo "== $S"
    echo "-- claim-support-verification.json: $(cat $S/claim-support-verification.json 2>/dev/null || echo '(无，全部默认)')"
    echo "-- 按当前分支规则（v3）会被复核的已退役 claim（只读）"
    PYTHONPATH=$BR/src $PY -m dalton_core.claim_reinstatement_cli support-rereview --state-dir "$S" --show 0 \
      | python3 -c 'import sys,json; d=json.load(sys.stdin); print({k: v for k, v in d.items() if not isinstance(v, list)})'
  done
}

replay() {
  echo "== 1/2 recheck 同义去重回放（legacy，只读）"
  PYTHONPATH=$BR/src $PY $BR/scripts/replay_recheck_near_duplicates.py \
    --state-dir "$L" --since 2026-09-26 --out /tmp/recheck-near-duplicates-legacy.json
  echo
  echo "== 2/2 v3 双家族核验回放（本地 mock，不调模型，只读）"
  PYTHONPATH=$BR/src $PY $BR/scripts/replay_claim_support_v2.py \
    --env legacy="$L" --env ws7d="$W" --since 2026-09-26 \
    --labels $BR/scripts/replay_claim_support_v2_labels.json \
    --extra-labels $BR/scripts/replay_claim_support_v3_labels.json \
    --second-opinion --out /tmp/replay-claim-support-v3.json \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); d.pop("prompts", None); print(json.dumps(d, ensure_ascii=False, indent=1))'
  echo "明细：/tmp/replay-claim-support-v3.json（注意：mock 的第二意见就是人工标注，不能代表真实模型）"
}

second_opinion() {  # $1 = on | off
  local value
  case $1 in
    on) value=true ;;
    off) value=false ;;
    *) echo "second-opinion 需要 on 或 off" >&2; exit 2 ;;
  esac
  for S in $L $W; do
    python3 - "$S/claim-support-verification.json" "$value" <<'PY'
import json, os, sys
path, value = sys.argv[1], sys.argv[2] == "true"
data = json.load(open(path)) if os.path.exists(path) else {}
data["second_opinion_on_rejection"] = value
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(path, data)
PY
  done
}

case $CMD in
  status) status ;;
  replay) replay ;;
  second-opinion) second_opinion "$2" ;;
  *) echo "未知命令：$CMD（可用：status | replay | second-opinion on|off）" >&2; exit 2 ;;
esac
