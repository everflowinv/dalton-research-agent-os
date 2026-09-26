#!/bin/zsh
# 支持核验（claim-support）误判修复：owner 需要执行的步骤（2026-09-26，分支 fix/support-verifier-accuracy）。
#
# 用法（默认只读，不写任何东西）：
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh status
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh pause-backfill
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh reinstate
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh reinstate apply
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-support-verifier-owner-steps.sh resume-backfill
#
#   status           只读：两个环境的退役/恢复计数，以及修复部署后自动复核会重问哪些 claim。
#   pause-backfill   建议现在就做：线上 backfill 仍按旧规则（v1）每 10 分钟左右撤回一批 claim。
#                    这一步在两个环境的 claim-support-verification.json 里写
#                    "backfill_batches_per_run": 0（保留文件里其它键），下一轮抽取即生效，不用重启。
#   reinstate        人工恢复经人工核对确认误撤的 claim。不加 apply 只做 dry-run；
#                    加 apply 则以 human:owner 身份经 writer 的 reinstate_claim_retirement 入口写入。
#                    默认只恢复 4 条确定误撤的；边界 3 条要恢复的话，设置 BORDERLINE=1。
#   resume-backfill  修复部署后再执行：把 backfill_batches_per_run 恢复为 1（默认值）。
#
# 修复部署后，backfill 会自动用 v2 规则把所有以 citation_support_rejected 退役的 claim 各重问一次，
# 支持的自动恢复（原因 citation_support_upheld_under_current_rule）。所以下面的人工恢复是可选的：
# 如果要立刻恢复就执行，否则等部署后自动完成。

PY=~/.dalton/runtime/releases/41fe150af11aeabd7ef467366b5f6388113642761a961621a47bbd0409503bab/venv/bin/python
WT=~/Projects/dalton-research-agent-os
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
WS_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json
CMD=${1:-status}
MODE=${2:-dry-run}

# writer 刚重启或积压时可能报 "writer service is unavailable"；最多重试 3 次，每次间隔 20 秒。
try() {
  for i in 1 2 3; do
    "$@" && return 0
    echo "  ...第 $i 次失败，20 秒后重试" >&2
    sleep 20
  done
  echo "  !! 放弃：$*" >&2
  return 1
}

# 4 条确定误撤（ws-7d）：v1 引文被 1200 字截断或原文逐字支持，v2 整句后明确支持且说的是该公司。
SURE_W=(
  claim-version:c4653527923f44181831480d05eeefc569b53733c63b5a1f52c2130f83ef5c5c  # META: Big Tech 增速领先，可为 2027 更多 capex 提供空间
  claim-version:50647925cae223d104fac0e3d2bee20a7b5cd531482119b8fa1f17110ed929bd  # AMZN: AWS + Trainium 带动情绪回暖
  claim-version:32c2febfe7ce04f8ca2edb2d9689a67b3e398edf5df3874bf736514f624f3002  # META: AI 改善推荐与广告定向，Muse Spark
  claim-version:385cdbb7e940ff0e4b1b72f43a2b22b77b511fa57246ed2108c2d7ef7989b3f5  # AMZN: MSFT/AMZN capex ROI 列为 WFE 上修依据
)
# 3 条边界（需要你判断）：
BORDER_L=(
  claim-version:09d5ff0339017cfdcd56dbde38605068b6b9a4a3e2ef36e2a45440022f216707  # ACN: India IT 对 ACN read-through（支持成立，主体存疑）
)
BORDER_W=(
  claim-version:8fcb6e600d0dd6a0fb01d139a132e87b930a733ae9bb0263289e9f098a782d4a  # META: Muse 发布后市场问资金来源（fc567ead 批次）
  claim-version:ad41d5e456fbe9a02ff983dc136cee626c039c37c84a1bd876d8f379efe85a79  # META: AI 投入的资金来源疑问（fc567ead 批次）
)
REASON="v1 支持核验误判：所引原文整句支持此结论（2026-09-26 人工核对）"

settings() {  # $1 = 状态目录, $2 = backfill_batches_per_run
  $PY - "$1" "$2" <<'EOF'
import json, sys
from pathlib import Path
path = Path(sys.argv[1]) / "claim-support-verification.json"
wire = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
wire["backfill_batches_per_run"] = int(sys.argv[2])
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(wire, indent=1) + "\n", encoding="utf-8")
tmp.replace(path)
print(f"  {path}: {wire}")
EOF
}

reinstate_one() {  # $1 = 状态目录, $2 = claim version ref
  if [[ $MODE == apply ]]; then
    try $PY -m dalton_core.claim_reinstatement_cli reinstate --state-dir "$1" \
      --claim-version-ref "$2" --reason "$REASON" --apply --actor human:owner
  else
    $PY -m dalton_core.claim_reinstatement_cli reinstate --state-dir "$1" \
      --claim-version-ref "$2" --reason "$REASON"
  fi
}

case $CMD in
  status)
    for S in $L $W; do
      echo "== $S"
      $PY -m dalton_core.claim_reinstatement_cli status --state-dir "$S"
      echo "-- 修复部署后 backfill 会重问的 claim（只读，用分支代码）"
      PYTHONPATH=$WT/src $PY -m dalton_core.claim_reinstatement_cli support-rereview \
        --state-dir "$S" --show 5
    done
    ;;
  pause-backfill)
    echo "== 暂停 backfill（两个环境）"
    settings $L 0
    settings $W 0
    ;;
  resume-backfill)
    echo "== 恢复 backfill（两个环境，每轮 1 批）"
    settings $L 1
    settings $W 1
    ;;
  reinstate)
    echo "== ws-7d：4 条确定误撤（$MODE）"
    export DALTON_WORKSPACE_MANIFEST=$WS_MANIFEST
    for r in $SURE_W; do reinstate_one $W $r; done
    if [[ ${BORDERLINE:-0} == 1 ]]; then
      echo "== ws-7d：2 条边界（$MODE）"
      for r in $BORDER_W; do reinstate_one $W $r; done
    fi
    unset DALTON_WORKSPACE_MANIFEST
    if [[ ${BORDERLINE:-0} == 1 ]]; then
      echo "== legacy：1 条边界（$MODE）"
      for r in $BORDER_L; do reinstate_one $L $r; done
    fi
    ;;
  *)
    echo "未知命令：$CMD（可用：status | pause-backfill | resume-backfill | reinstate [apply]）" >&2
    exit 2
    ;;
esac
echo "== 完成"
