#!/bin/zsh
# 工作环境治理基线补签（2026-09-27）：让 ws-7d 的策略与 legacy policy-18 的运行基线一致，并检查 ws-e399。
# 用法：zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-27-workspace-parity-owner-steps.sh
#
# 前提：先部署包含 feat/workspace-parity（及 main 的 70ef64ee）的版本。本脚本第 0 步会核对当前 release 里有没有
#   CoverageMissionAuthority.carry_forward_awaiting_reviews。没有就停止，原因有两个：
#   - 补签会让 ws-7d 的 mission 升到 v6；
#   - 旧版本不会把挂在旧版本上的待处理 review 迁过来，抽取会继续停摆（现在 ws-7d 已有 47 条挂在 v2/v4 上）。
#
# 做什么：
#   1) 只读跑 parity 检查，看补签前的状态。
#   2) ws-7d：先 dry-run，确认结果是 would-publish policy-6，再用 human:owner 身份通过 ws-7d 的 writer 发布
#      policy-6 → constitution v6 → mission v6。只新增下面两条，其他字段不变：
#        research-auto-commit:sec-public-company-facts-growth-annual:v1（10-K 第四季度配对）
#        research-auto-commit:mission-verified-figure:v1（已复核的文档数字）
#      research_plan_auto_start 在 policy-5 已经签过，这里只做核对。
#   3) ws-e399：还没有发布研究任务，不需要补签。新版创建流程会在首个任务发布时签入整套基线。
#      如果它在旧版本上已经发布了任务，这一步会按 ws-7d 的方式补签。
#   4) 再只读跑一次 parity 检查。
#
# 不做什么：不部署，不重启服务，不访问 SEC 或任何外部服务。
# independence_predicates（producer/verifier 模型家族不同）在 legacy policy-14 和 ws-7d policy-2 被预算编辑顺带删掉了。
#   要不要恢复由你决定，本脚本不动它。

set -u
REPO=${0:A:h:h:h}
PY=$HOME/Projects/dalton-research-agent-os/.venv/bin/python
MANAGER=$HOME/.dalton/manager.json
W7D=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
WE399=/Volumes/EveSSD/Dalton/workspaces/ws-e399ececd5aa7a3762b1a0a4/state/dalton-core
ACTOR=human:owner

echo "== 0/4 核对当前 release 已包含 open review 迁移"
RELEASE_PY=$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_path"] + "/bin/python")' "$MANAGER") || { echo "!! 读不到 $MANAGER" >&2; exit 1; }
if ! "$RELEASE_PY" -c 'from dalton_core.coverage_mission import CoverageMissionAuthority as C; import sys; sys.exit(0 if hasattr(C, "carry_forward_awaiting_reviews") else 1)'; then
  echo "!! 当前 release（$RELEASE_PY）还没有 carry_forward_awaiting_reviews。请先部署包含 70ef64ee（main）和 feat/workspace-parity 的版本，再运行本脚本。" >&2
  exit 1
fi
echo "   ok"

echo "== 1/4 补签前：只读 parity 检查"
$PY "$REPO/scripts/check_workspace_parity.py" --no-host | grep -E "^==|   mission|GAP" || true

sign_env() {
  local NAME=$1 STATE=$2
  local HAS_MISSION
  HAS_MISSION=$($PY -c 'import sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}/core.sqlite?mode=ro", uri=True); print(c.execute("SELECT COUNT(*) FROM coverage_mission_pointer").fetchone()[0])' "$STATE") || { echo "!! $NAME：读不到 Core" >&2; return 1; }
  if [[ "$HAS_MISSION" == "0" ]]; then
    echo "   $NAME 还没有发布研究任务，无需补签（首个任务发布时由创建流程签入基线）。"
    return 0
  fi
  echo "   -- $NAME：auto-commit 规则（dry-run）"
  local PLAN STATUS
  PLAN=$($PY "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE") || { echo "!! $NAME dry-run 失败" >&2; return 1; }
  STATUS=$(print -r -- "$PLAN" | $PY -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("next_policy",""), ",".join(d.get("rules_to_add") or []))')
  echo "      -> $STATUS"
  if [[ "$STATUS" == "already-signed"* ]]; then
    echo "      已经签过，跳过。"
  elif [[ "$STATUS" != "would-publish "* ]]; then
    echo "!! $NAME：预期 would-publish，实际是 '$STATUS'。停止执行，请把上面的输出发给我。" >&2
    return 1
  else
    local ok=0
    for i in 1 2 3; do
      $PY "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE" --apply --actor "$ACTOR" >/dev/null && { ok=1; break; }
      echo "      ...第 $i 次失败，20 秒后重试" >&2
      sleep 20
    done
    [[ $ok == 1 ]] || { echo "!! $NAME 发布失败，停止执行" >&2; return 1; }
    $PY "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE" | $PY -c 'import json,sys; d=json.load(sys.stdin); print("      核验:", d["status"], d["active_policy"])'
  fi
  echo "   -- $NAME：research_plan_auto_start（dry-run）"
  PLAN=$($PY "$REPO/scripts/sign_research_plan_auto_start.py" --state-dir "$STATE") || { echo "!! $NAME dry-run 失败" >&2; return 1; }
  STATUS=$(print -r -- "$PLAN" | $PY -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("next_policy",""), d.get("lane_precondition_after", d.get("lane_precondition_now","")))')
  echo "      -> $STATUS"
  if [[ "$STATUS" == "already-signed"* ]]; then
    echo "      已经签过，跳过。"
  elif [[ "$STATUS" == "would-publish "*" ok" ]]; then
    $PY "$REPO/scripts/sign_research_plan_auto_start.py" --state-dir "$STATE" --apply --actor "$ACTOR" >/dev/null || { echo "!! $NAME 发布失败" >&2; return 1; }
    echo "      已发布。"
  else
    echo "!! $NAME：预期 already-signed 或 would-publish … ok，实际是 '$STATUS'。停止执行。" >&2
    return 1
  fi
}

echo "== 2/4 ws-7d 补签"
sign_env ws-7d "$W7D" || exit 1

echo "== 3/4 ws-e399 核对"
sign_env ws-e399 "$WE399" || exit 1

echo "== 4/4 补签后：只读 parity 检查（抽取 tick 会在下一轮自动迁移挂在旧版本上的 review）"
$PY "$REPO/scripts/check_workspace_parity.py" --no-host | grep -E "^==|   mission|GAP" || true
echo "== 完成"
