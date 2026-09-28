#!/bin/zsh
# 给 legacy 和 ws-7d 补签 FY−9M 第四季推导规则（2026-09-28，分支 feat/q4-from-fy-minus-9m）。
#
# 规则：research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1
#   10-K 只报全年数时，第四季 = 10-K 全年收入 − 同一财年前三季（10-Q 的九个月累计行，没有时用 Q1+Q2+Q3），
#   去年第四季同法算出，再求同比。输入全部是报表车道已入库的申报行；同一概念、财年边界逐日对齐、
#   有重述/概念变化/边界对不上/前三季未持有/申报精度不够时拒绝推导。入账的 claim 的 basis 是
#   official-filing-xbrl-derived，句子写明“derived value (FY − 9M), not a filed quarter”，证据列出每一行。
#   新 workspace 由创建流程默认签入（workspace_governance_baseline）；legacy 和 ws-7d 用本脚本补签。
#
# 用法（整个文件交给 zsh 执行）：
#   zsh docs/ops/sign-fy-minus-9m-rule-2026-09-28.sh            # 默认 dry-run：只读，什么都不写
#   zsh docs/ops/sign-fy-minus-9m-rule-2026-09-28.sh rehearse   # 在 /tmp 的 Core 副本上发布一遍，线上库不动
#   zsh docs/ops/sign-fy-minus-9m-rule-2026-09-28.sh apply      # 真正发布：以 human:owner 经该环境 writer 发布
#   可选环境变量：ONLY=legacy|ws7d 只处理一个环境；ACTOR=human:<名字> 改签署人；
#   LEGACY_STATE / WS7D_STATE / LEGACY_RELEASE_PY / WS7D_RELEASE_PY 覆盖默认路径。
#
# 每个环境做什么：
#   1) 核对该环境正在运行的 release 认识这条规则。不认识就不能签：旧 evaluator 看到不认识的规则会把
#      整组规则判为不支持，所有自动入账都会停。dry-run 只提示；apply 直接停止。
#   2) 只读预览（scripts/preview_fy_minus_9m.py，mode=ro）：每份已入库的 10-K 会推导出哪个季度、
#      数值和精度，或者为什么拒绝；并和已入账的季度数据交叉核对（前三季 + 推导的第四季 = 全年）。
#   3) scripts/sign_auto_commit_rules.py --rule <本规则> 的 dry-run：应为 would-publish（或已签过的
#      already-signed）。policy → constitution → mission 一起升一版，其他规则和字段不变。
#   4) apply 时：发布（失败重试 3 次），再 dry-run 一次，确认 already-signed。
#      发布后季度车道的下一个 tick 会自己推导、暂存、入账，不需要再做别的；不会发起任何 SEC 请求。
#
# 不做什么：不部署，不重启服务，不访问 SEC 或任何外部服务，不直接写库（发布走 writer）。

set -u
setopt pipefail

SCRIPT=${0:A}
REPO=${0:A:h:h:h}
PY=${DALTON_PY:-$HOME/Projects/dalton-research-agent-os/.venv/bin/python}
export PYTHONPATH=$REPO/src
RULE=research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1
ACTOR=${ACTOR:-human:owner}
ONLY=${ONLY:-all}
MODE=${1:-dry-run}
LEGACY_STATE=${LEGACY_STATE:-/Volumes/EveSSD/Dalton/legacy-state/dalton-core}
WS7D_STATE=${WS7D_STATE:-/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core}
MANAGER=$HOME/.dalton/manager.json
WS7D_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json
OUT=$(mktemp -d /tmp/sign-fy-minus-9m-XXXXXX)

say()  { print -r -- "$*"; }
step() { print -r -- ""; print -r -- "== $*"; }
die()  { print -u2 -r -- "!! $*"; print -u2 -r -- "!! 停止执行。输出在 $OUT"; exit 1; }
jq_py() { "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"; }

case $MODE in
  dry-run|rehearse|apply) ;;
  *) print -u2 -r -- "未知参数：$MODE（可用：不带参数 = dry-run，rehearse，apply）"; exit 2 ;;
esac
[[ -x "$PY" ]] || die "找不到 Python：$PY（可用 DALTON_PY=... 指定）"
[[ -f "$REPO/scripts/sign_auto_commit_rules.py" && -f "$REPO/scripts/preview_fy_minus_9m.py" ]] || \
  die "$REPO 里没有 sign_auto_commit_rules.py / preview_fy_minus_9m.py，请用包含它们的 checkout 运行"
"$PY" -c 'import sys; from dalton_core.research_auto_commit import KNOWN_RULE_REFS as K; sys.exit(0 if sys.argv[1] in K else 1)' "$RULE" || \
  die "$REPO 的代码不认识 $RULE，请用 feat/q4-from-fy-minus-9m（或合并后的 main）运行"

release_py() {
  local NAME=$1
  if [[ $NAME == legacy ]]; then
    if [[ -n ${LEGACY_RELEASE_PY:-} ]]; then print -r -- "$LEGACY_RELEASE_PY"; return; fi
    [[ -f $MANAGER ]] || return 1
    print -r -- "$(jq_py "$MANAGER" 'd["release_path"]')/bin/python"
  else
    if [[ -n ${WS7D_RELEASE_PY:-} ]]; then print -r -- "$WS7D_RELEASE_PY"; return; fi
    [[ -f $WS7D_MANIFEST ]] || return 1
    print -r -- "$(jq_py "$WS7D_MANIFEST" 'd["release_path"]')/bin/python"
  fi
}

sign_env() {
  local NAME=$1 STATE=$2
  step "$NAME（$STATE）"
  [[ -f "$STATE/core.sqlite" ]] || die "$NAME：找不到 $STATE/core.sqlite"

  say "-- 1/4 运行中的 release 是否认识这条规则"
  local RPY KNOWN=unknown
  RPY=$(release_py "$NAME") || RPY=""
  if [[ -n "$RPY" && -x "$RPY" ]]; then
    if env -u PYTHONPATH "$RPY" -c 'import sys; from dalton_core.research_auto_commit import KNOWN_RULE_REFS as K; sys.exit(0 if sys.argv[1] in K else 1)' "$RULE" 2>/dev/null; then
      KNOWN=yes
    else
      KNOWN=no
    fi
  fi
  say "   release：${RPY:-（读不到）} -> $KNOWN"
  if [[ $KNOWN != yes ]]; then
    if [[ $MODE == apply ]]; then
      die "$NAME：运行中的 release 不认识 $RULE。先部署包含 feat/q4-from-fy-minus-9m 的版本再签，否则所有自动入账会停。"
    fi
    say "   !! 还没部署：现在 apply 会被本脚本拒绝（dry-run / rehearse 照常）"
  fi

  say "-- 2/4 只读预览：会推导出哪些第四季（mode=ro）"
  "$PY" "$REPO/scripts/preview_fy_minus_9m.py" --state-dir "$STATE" > "$OUT/$NAME-preview.json" || \
    die "$NAME：预览失败"
  "$PY" - "$OUT/$NAME-preview.json" <<'PYEOF' || die "$NAME：读不出预览结果"
import json, sys
for row in json.load(open(sys.argv[1])):
    head = f"   {row['company_ref']} 10-K {row['accession']}（财年截至 {row['fiscal_year_end']}）"
    if row["status"] != "derivable":
        print(head + "：拒绝推导 —— " + row["reason"].split(": ", 1)[-1])
        continue
    checks = row["cross_check"]
    held = "，已有同期 claim，不会再写" if row["already_held"] else ""
    print(head + f"：{row['period']} Q4 = {row['q4']['value']}（±{row['q4']['uncertainty']}），"
          f"去年同季 {row['prior_q4']['value']}，同比 {row['growth_percent']}%"
          f"（精度区间 {row['precision']['interval_pp'][0]}%..{row['precision']['interval_pp'][1]}%）{held}")
    for year in ("current", "prior"):
        item = checks[year]
        print(f"      {year}: 申报前三季+Q4−全年 = {item['filed_quarters_plus_q4_minus_fy']}；"
              f"已入账季度 {item['ledger_quarters_held']}/3，已入账前三季+Q4−全年 = "
              f"{item['ledger_quarters_plus_q4_minus_fy']}；与已入账不一致 {len(item['ledger_mismatches'])} 处")
PYEOF

  say "-- 3/4 签入 dry-run"
  "$PY" "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE" --rule "$RULE" \
    > "$OUT/$NAME-plan.json" || die "$NAME：dry-run 失败"
  local STATUS
  STATUS=$(jq_py "$OUT/$NAME-plan.json" 'd["status"] + " " + d.get("next_policy", d["active_policy"]) + " " + ",".join(d.get("publishes") or [])')
  say "   -> $STATUS"
  if [[ $STATUS == already-signed* ]]; then
    say "   已经签过，跳过。"
    return 0
  fi
  [[ $STATUS == would-publish* ]] || die "$NAME：预期 would-publish 或 already-signed，实际是 '$STATUS'"

  if [[ $MODE == rehearse ]]; then
    say "-- 4/4 在副本上发布（$OUT/$NAME-copy，线上库只读）"
    "$PY" "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE" --rule "$RULE" \
      --rehearse "$OUT/$NAME-copy" --actor "$ACTOR" > "$OUT/$NAME-rehearsal.json" || \
      die "$NAME：副本发布失败"
    say "   $(jq_py "$OUT/$NAME-rehearsal.json" '"added=" + ",".join(d["added"]) + " mission_binds_new_constitution=" + str(d["mission_binds_new_constitution"])')"
    rm -f "$OUT/$NAME-copy/core.sqlite" "$OUT/$NAME-copy/core.sqlite-wal" "$OUT/$NAME-copy/core.sqlite-shm"
    return 0
  fi
  if [[ $MODE != apply ]]; then
    say "-- 4/4 dry-run 到此为止；确认无误后执行：zsh $SCRIPT apply"
    return 0
  fi

  say "-- 4/4 发布（$ACTOR，经 $NAME 的 writer）"
  local ok=0 i
  for i in 1 2 3; do
    "$PY" "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE" --rule "$RULE" \
      --apply --actor "$ACTOR" > "$OUT/$NAME-apply.json" && { ok=1; break; }
    print -u2 -r -- "   ...第 $i 次失败，20 秒后重试"
    sleep 20
  done
  [[ $ok == 1 ]] || die "$NAME：发布失败"
  "$PY" "$REPO/scripts/sign_auto_commit_rules.py" --state-dir "$STATE" --rule "$RULE" \
    > "$OUT/$NAME-verify.json" || die "$NAME：发布后核验失败"
  STATUS=$(jq_py "$OUT/$NAME-verify.json" 'd["status"] + " " + d["active_policy"]')
  [[ $STATUS == already-signed* ]] || die "$NAME：发布后核验不是 already-signed：$STATUS"
  say "   核验：$STATUS"
}

say "仓库：$REPO"
say "模式：$MODE    规则：$RULE"
say "输出目录：$OUT"
[[ $ONLY == all || $ONLY == legacy ]] && { sign_env legacy "$LEGACY_STATE" || exit 1; }
[[ $ONLY == all || $ONLY == ws7d ]] && { sign_env ws7d "$WS7D_STATE" || exit 1; }
step "完成（$MODE）"
[[ $MODE == dry-run ]] && say "确认无误后：部署包含本分支的版本，再执行 zsh $SCRIPT apply（可加 ONLY=legacy 或 ONLY=ws7d）"
exit 0
