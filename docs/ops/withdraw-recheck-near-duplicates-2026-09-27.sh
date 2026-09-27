#!/bin/zsh
# 撤回 legacy 上 recheck 已入账的“同义改写重复” claim（2026-09-27，分支 fix/support-lane-independent）。
#
# 背景：recheck 以前只按“完全相同的文本 + 引用绑定”判重。部署后它入账的 claim 里有 6 条
# 是同一文档、同一段引用（span 重叠）里已有 live claim 的同义改写（相似度 0.884–0.946）。
# 修复后 recheck 不再提交这类重复；已经入账的 6 条需要人工撤回（human_judgment 退役，可再恢复）。
#
# 用法：
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/withdraw-recheck-near-duplicates-2026-09-27.sh
#       默认只读：先回放判重规则（mode=ro），再对下面 6 条做 dry-run，打印将撤回的内容和 selection_sha256。
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/withdraw-recheck-near-duplicates-2026-09-27.sh apply
#       真正撤回：以 human:owner 身份经 writer 的 retire_claim_by_hand 入口写入（writer 必须在运行）。
#       ACTOR=human:<名字> 可以改执行人。
#
# 每条都是“后来入账的那条”；它重复的那条（更早的 live claim）保留不动。

WT=~/Projects/dalton-research-agent-os
BR=${BR:-$HOME/Projects/dalton-wt-supportlane}
PY=$WT/.venv/bin/python
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
ACTOR=${ACTOR:-human:owner}
MODE=${1:-dry-run}
REASON="同一文档同一段引用里已有内容相同的 live claim（recheck 同义改写重复，2026-09-27 判重规则回放）"

REFS=(
  claim-version:66406236ee7b6c9df00788dfafda799ad840e582bbb64e7c8a1054f121626dba  # CTSH 通信与媒体需求疲软（0.946）
  claim-version:8d528695e8e5c888f489d3ca61de4e8e737efb475961fce17dd98e314067f46b  # EPAM TD Cowen 下行风险（0.91）
  claim-version:81768be92c3efbd9d901c075bacf0fffb7b19dffbde7b81093c05df15a8e615b  # DXC 项目制服务市场困难（0.92）
  claim-version:bdb03c3d2aceb4699c3bf60bc565d56c7e885ecd77151146361ddcc1aeff95ea  # IBM RBC 主要竞争对手（0.894）
  claim-version:2ca92271e87eb5987be558e575ae29d81069329630e0508f86db0282bd29300b  # IBM Red Hat/Confluent/Hashicorp（0.896）
  claim-version:819d73da249c8c1c33066118079b9b6410abe43dd507319338e54f4519f2db78  # IBM Z 大型机耐久性（0.884）
)

ARGS=()
for R in $REFS; do ARGS+=(--claim-version-ref "$R"); done

if [[ $MODE == dry-run ]]; then
  echo "== 1/2 回放判重规则（只读，分支代码 $BR）"
  if [[ -d $BR/src ]]; then
    PYTHONPATH=$BR/src $PY $BR/scripts/replay_recheck_near_duplicates.py \
      --state-dir "$L" --since 2026-09-26 --out /tmp/recheck-near-duplicates-legacy.json
  else
    echo "（分支工作树不存在，跳过回放；合并后改用 BR=$WT）"
  fi
  echo
  echo "== 2/2 dry-run：将撤回的 claim（不写入）"
  PYTHONPATH=$WT/src $PY -m dalton_core.claim_admission_cli retire --state-dir "$L" \
    $ARGS --reason "$REASON"
  echo
  echo "确认无误后执行：zsh $0 apply"
elif [[ $MODE == apply ]]; then
  echo "== 撤回 ${#REFS} 条（$ACTOR）"
  PYTHONPATH=$WT/src $PY -m dalton_core.claim_admission_cli retire --state-dir "$L" \
    $ARGS --reason "$REASON" --apply --actor "$ACTOR"
else
  echo "未知参数：$MODE（可用：不带参数 = dry-run，或 apply）" >&2
  exit 2
fi
