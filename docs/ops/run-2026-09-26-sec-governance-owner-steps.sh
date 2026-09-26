#!/bin/zsh
# ws-7d SEC company-facts 车道：签入 research_plan_auto_start，并归还被治理前置条件烧掉的次数（2026-09-26）。
# 用法：zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-sec-governance-owner-steps.sh
# 合并进 main 之后改用：zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-sec-governance-owner-steps.sh
#
# 1) 先 dry-run，确认结果是 would-publish policy-5，再以 human:owner 身份通过 ws-7d 的 writer 发布
#    policy-5 → constitution v5 → mission v5（只新增 research_plan_auto_start，其他字段不变）。
# 2) 等 10 分钟，让发布前已经启动的那几次运行结算完毕。
# 3) 归还 "lane precondition failed" 烧掉的次数：先 dry-run 列出条数，再 --apply。
# 这个脚本不部署、不重启任何服务，也不访问 SEC。

set -u
REPO=${0:A:h:h:h}
PY=$HOME/Projects/dalton-research-agent-os/.venv/bin/python
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
C=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/config/service.json
REASON="ws-7d policy-4 lacked research_plan_auto_start; the SEC lane refused every run at its governance precondition before fetching anything (signed in policy-5)"

echo "== 1/4 ws-7d：签入 research_plan_auto_start（dry-run）"
PLAN=$($PY "$REPO/scripts/sign_research_plan_auto_start.py" --state-dir "$W") || { echo "!! dry-run 失败" >&2; exit 1; }
echo "$PLAN"
STATUS=$(print -r -- "$PLAN" | $PY -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("next_policy"), d.get("lane_precondition_after"))')
echo "   -> $STATUS"
if [[ "$STATUS" == "already-signed"* ]]; then
  echo "   已经签过，跳过发布。"
elif [[ "$STATUS" != "would-publish policy-5 ok" ]]; then
  echo "!! 预期是 'would-publish policy-5 ok'，实际是 '$STATUS'。停止执行，请把上面的输出发给我。" >&2
  exit 1
else
  echo "== 2/4 ws-7d：发布 policy-5 → constitution → mission"
  ok=0
  for i in 1 2 3; do
    $PY "$REPO/scripts/sign_research_plan_auto_start.py" --state-dir "$W" --apply --actor human:owner && { ok=1; break; }
    echo "  ...第 $i 次失败，20 秒后重试" >&2
    sleep 20
  done
  [[ $ok == 1 ]] || { echo "!! 发布失败，停止执行" >&2; exit 1; }
  $PY "$REPO/scripts/sign_research_plan_auto_start.py" --state-dir "$W" | $PY -c 'import json,sys; d=json.load(sys.stdin); print("   核验:", d["status"], d["active_policy"], "lane precondition:", d["lane_precondition_now"])'
  echo "   等 10 分钟，让发布前已启动的运行结算完毕……"
  sleep 600
fi

echo "== 3/4 ws-7d：归还次数（dry-run）"
$PY "$REPO/scripts/void_sec_dispatch_attempts.py" --config "$C" --match "lane precondition failed" --reason "$REASON" --voided-by human:owner | $PY -c 'import json,sys; d=json.load(sys.stdin); print("   将归还", d["attempts_to_void"], "次，涉及", d["accessions_affected"], "份 filing", d["by_ticker"]); print("   失败原因:", d["by_run_error"])'

echo "== 4/4 ws-7d：归还次数（apply）"
$PY "$REPO/scripts/void_sec_dispatch_attempts.py" --config "$C" --match "lane precondition failed" --reason "$REASON" --voided-by human:owner --apply | $PY -c 'import json,sys; d=json.load(sys.stdin); print("   已归还", d["applied"], "次；归还后各 filing 剩余计数:", d["attempts_after"])'

echo "== 完成"
