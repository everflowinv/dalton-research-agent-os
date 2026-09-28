#!/bin/zsh
# 恢复 independence predicates，并补齐 ws-7d 的 verifier 路由和 xai 凭证槽位（2026-09-28）。
# 用法（整个文件交给 zsh 执行，不需要往终端里粘贴长命令）：
#   zsh <仓库>/docs/ops/run-2026-09-28-restore-independence-and-parity-steps.sh
# 本脚本放在 feat/restore-independence-and-parity-steps 分支（worktree ~/Projects/dalton-wt-ownerfix）；
# 用哪个 checkout 里的这份脚本，就用那个 checkout 的 src 和 scripts。
#
# 做什么（任何一步失败都会停下，并说明停在哪里；输出都存在 $OUT 目录里）：
#   a) 只读跑 parity 检查，记录修复前的状态。
#   b) 恢复 independence predicates：legacy、ws-7d 各一次。先 dry-run，只有结果是 would-publish
#      （或上次中断留下的 would-resume-cascade）且没有受影响的 producer/verifier 路由时才 apply，
#      以 human:owner 通过该环境的 writer 发布 policy → constitution → mission。
#      谓词内容取自 dalton_core.policy.DEFAULT_POLICY（fresh Core 的 policy-1）：
#        producer.model_family ne verifier.model_family
#      其它策略字段不动；mandate 不动。
#   c) scripts/align_model_routing.py：先 dry-run，再 --apply --actor human:owner。
#      当前 ws-7d 需要发布 19 次选择：2 个类别（cheap、deliverable）+ 16 个 *_verifier 环节
#      + research_language_check。legacy 自己在 brain / verifier 两个类别上不一致，
#      对齐脚本会跳过这两项并提示，这是你的决定，不在本脚本范围内。
#   d) ws-7d 补 xai 凭证槽位（scripts/repair_workspace_lane_parity.py）：
#      停 control → controller → thesis-impact → 等 lane 子进程排空 → 停 writer，
#      等端口释放、sleep，再 --apply（会重渲染 plist），核对 control 的 ProcessType 仍为 Standard
#      （不是就用 plutil 改回），sleep 后逐个 bootstrap（失败会重试），最后确认服务都在运行。
#      ws-7d 目前没有 thesis-impact 的 plist（service.json 没配 thesis_impact），这一项会注明“未安装”。
#      一旦服务已停而后续步骤失败，脚本会先尝试把服务拉起来再退出。
#      不做 SEC 公司名查询（--no-sec-name-lookup）：本次不需要，也不向外部发请求。
#   e) 再跑一次 parity 检查：legacy 和 ws-7d 不应再有 drift，也不应再有路由或凭证类 gap；
#      并再跑一次谓词 dry-run，确认两边都是 already-restored、没有受影响的路由。
#
# 不做什么：不部署，不动 legacy 的服务，不访问 SEC 或任何外部服务。
# 注意：重渲染 ws-7d 的 writer plist 还会带上两处和 xai 无关的变化（当前代码渲染出来就是这样）：
#   planner 路由策略 pin dalton-openclaw-planner-decisions:40 → :62，凭证槽位多出 muse-cli-gateway。

set -u
setopt pipefail

REPO=${0:A:h:h:h}
PY=${DALTON_PY:-$HOME/Projects/dalton-research-agent-os/.venv/bin/python}
export PYTHONPATH=$REPO/src
cd "$REPO" || exit 1

ACTOR=human:owner
SLUG=ws-7d894366d1132e2930475a60
LEGACY_STATE="$HOME/Library/Application Support/Dalton/state/dalton-core"
MANIFEST=$HOME/.dalton/workspaces/$SLUG/workspace.json
LA=$HOME/Library/LaunchAgents
NS=space.lumos.dalton.workspace.$SLUG
GUI=gui/$(id -u)
OUT=$(mktemp -d /tmp/restore-independence-XXXXXX)
SERVICES_STOPPED=0

say()  { print -r -- "$*"; }
step() { print -r -- ""; print -r -- "== $*"; }

# 读 JSON 文件里的一个值：jq_py <文件> <python 表达式，变量 d 是整个 JSON>
jq_py() {
  "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}

die() {
  print -u2 -r -- ""
  print -u2 -r -- "!! $*"
  if [[ $SERVICES_STOPPED == 1 ]]; then
    print -u2 -r -- "!! ws-7d 的服务在本脚本里被停过，现在尝试把它们拉起来……"
    start_services || print -u2 -r -- "!! 拉起失败，请手动检查：launchctl print $GUI/$NS.writer"
  fi
  print -u2 -r -- "!! 停止执行。本次所有输出在 $OUT"
  exit 1
}

[[ -x "$PY" ]] || die "找不到 Python：$PY（可用 DALTON_PY=... 指定）"
[[ -f "$MANIFEST" ]] || die "找不到 ws-7d 的工作区清单：$MANIFEST"
[[ -f "$LEGACY_STATE/core.sqlite" ]] || die "找不到 legacy Core：$LEGACY_STATE"
[[ -f "$REPO/scripts/restore_independence_predicates.py" ]] || \
  die "$REPO 里没有 scripts/restore_independence_predicates.py，请用包含它的 checkout 运行"
W7D_STATE=$(jq_py "$MANIFEST" 'd["state_dir"]') || die "读不出 $MANIFEST 的 state_dir"
RELEASE_PY=$(jq_py "$MANIFEST" 'd["release_path"]')/bin/python
COCKPIT_PORT=$(jq_py "$MANIFEST" 'd["cockpit_port"]') || die "读不出 cockpit_port"
say "仓库：$REPO"
say "输出目录：$OUT"

# ---------------------------------------------------------------------------
# a) 修复前 parity

step "a) 修复前：只读 parity 检查"
"$PY" scripts/check_workspace_parity.py > "$OUT/parity-before.txt" 2>&1 \
  || die "parity 检查运行失败，见 $OUT/parity-before.txt"
grep -E "^==|^   mission|drift|GAP" "$OUT/parity-before.txt" || true

# ---------------------------------------------------------------------------
# b) 恢复谓词

restore_dry_run() {  # <名字> <state dir> <输出文件>
  "$PY" scripts/restore_independence_predicates.py --state-dir "$2" > "$3" 2> "$3.err" \
    || { cat "$3.err" >&2; return 1; }
}

restore_env() {  # <名字> <state dir>
  local NAME=$1 STATE=$2 PLAN=$OUT/restore-$1-dryrun.json STATUS AFFECTED
  say "   -- $NAME：dry-run"
  restore_dry_run "$NAME" "$STATE" "$PLAN" || die "$NAME：谓词 dry-run 失败"
  STATUS=$(jq_py "$PLAN" 'd["status"]')
  AFFECTED=$(jq_py "$PLAN" 'len(d["affected_routes"])')
  say "      状态：$STATUS；当前策略 $(jq_py "$PLAN" 'd["active_policy"]')；"\
"将发布：$(jq_py "$PLAN" '", ".join(d["publishes"]) or "（无）"')"
  say "      历史上会被新谓词拒绝的 producer/verifier 记录：$(jq_py "$PLAN" 'd["historical_pairs_that_would_fail"]')"
  if [[ "$STATUS" == "already-restored" ]]; then
    say "      已经恢复过，跳过。"
    return 0
  fi
  if [[ "$STATUS" != "would-publish" && "$STATUS" != "would-resume-cascade" ]]; then
    die "$NAME：预期 would-publish，实际是 '$STATUS'（完整输出：$PLAN）"
  fi
  if [[ "$AFFECTED" != "0" ]]; then
    jq_py "$PLAN" '"\n".join(r["producer"]+" -> "+r["verifier"]+": "+r["verdict"] for r in d["affected_routes"])' >&2
    die "$NAME：恢复后上面这些受策略约束的 producer/verifier 路由会有同家族的情况，先由你决定再恢复"
  fi
  local i ok=0 ERR=$OUT/restore-$NAME-apply.err
  for i in 1 2 3 4; do
    if "$PY" scripts/restore_independence_predicates.py --state-dir "$STATE" \
         --apply --actor "$ACTOR" > "$OUT/restore-$NAME-apply.json" 2> "$ERR"; then
      ok=1; break
    fi
    cat "$ERR" >&2
    grep -qi "unavailable" "$ERR" || die "$NAME：发布失败，且不是 writer 暂时不可用（见 $ERR）"
    say "      writer 暂时不可用（第 $i 次），30 秒后重新 dry-run 再继续……"
    sleep 30
    restore_dry_run "$NAME" "$STATE" "$PLAN" || die "$NAME：重试前的 dry-run 失败"
    STATUS=$(jq_py "$PLAN" 'd["status"]')
    say "      现在的状态：$STATUS"
    [[ "$STATUS" == "already-restored" ]] && { ok=1; break; }
  done
  [[ $ok == 1 ]] || die "$NAME：重试 4 次后仍然发布失败（见 $ERR）"
  restore_dry_run "$NAME" "$STATE" "$PLAN.after" || die "$NAME：发布后的核验 dry-run 失败"
  STATUS=$(jq_py "$PLAN.after" 'd["status"]')
  [[ "$STATUS" == "already-restored" ]] || die "$NAME：发布后核验不是 already-restored，而是 '$STATUS'"
  say "      已恢复：当前策略 $(jq_py "$PLAN.after" 'd["active_policy"]')，"\
"constitution $(jq_py "$PLAN.after" 'd["active_constitution"]')，mission $(jq_py "$PLAN.after" 'd["active_mission"]')"
}

step "b) 恢复 independence predicates"
restore_env legacy "$LEGACY_STATE"
restore_env ws-7d "$W7D_STATE"

# ---------------------------------------------------------------------------
# c) 路由对齐

step "c) 模型路由对齐（先 dry-run）"
"$PY" scripts/align_model_routing.py > "$OUT/align-dryrun.txt" 2>&1 \
  || die "align_model_routing dry-run 失败，见 $OUT/align-dryrun.txt"
grep -E "^环境|需要发布|已经和源环境一致|⚠|共 " "$OUT/align-dryrun.txt" || true
"$PY" scripts/align_model_routing.py --json > "$OUT/align-dryrun.json" 2> "$OUT/align-dryrun.err" \
  || die "align_model_routing --json dry-run 失败"
DIFFERING=$(jq_py "$OUT/align-dryrun.json" 'd["environments_differing"]')
if [[ "$DIFFERING" == "0" ]]; then
  say "   所有环境已和 legacy 一致，跳过。"
else
  ALIGNED=0
  for i in 1 2 3 4; do
    say "   --apply 第 $i 次"
    "$PY" scripts/align_model_routing.py --apply --actor "$ACTOR" --json \
      > "$OUT/align-apply-$i.json" 2> "$OUT/align-apply-$i.err" \
      || die "align_model_routing --apply 运行失败，见 $OUT/align-apply-$i.err"
    RESULT=$(jq_py "$OUT/align-apply-$i.json" '"|".join(
      a["environment_id"]+"="+a["status"] for a in d.get("applied", []))')
    say "      结果：$RESULT"
    FAILED=$(jq_py "$OUT/align-apply-$i.json" '"\n".join(
      p.get("reason", "") for a in d.get("applied", []) for p in a.get("published", [])
      if p.get("status") == "failed")')
    if [[ -n "$FAILED" ]]; then
      print -u2 -r -- "$FAILED"
      print -r -- "$FAILED" | grep -qi "unavailable" \
        || die "路由对齐失败，且不是 writer 暂时不可用（见 $OUT/align-apply-$i.json）"
      say "      writer 暂时不可用，30 秒后重试（脚本是幂等的，已发布的不会重复）……"
      sleep 30
      continue
    fi
    LEFT=$(jq_py "$OUT/align-apply-$i.json" 'd.get("residual", {}).get("environments_differing", "?")')
    [[ "$LEFT" == "0" ]] && { ALIGNED=1; break; }
    say "      还有 $LEFT 个环境不同（类别保存会清掉同类别的环节选择），再跑一次。"
  done
  [[ $ALIGNED == 1 ]] || die "路由对齐 4 次后仍未完成（见 $OUT/align-apply-*.json）"
fi
"$PY" scripts/align_model_routing.py --json > "$OUT/align-after.json" 2> /dev/null \
  || die "对齐后的核验 dry-run 失败"
[[ $(jq_py "$OUT/align-after.json" 'd["environments_differing"]') == "0" ]] \
  || die "对齐后仍有环境与 legacy 不同（见 $OUT/align-after.json）"
say "   对齐完成。"

# ---------------------------------------------------------------------------
# d) ws-7d 凭证槽位

ROLES_STOP=(control controller thesis-impact)
ROLES_START=(writer controller control thesis-impact)

loaded() { launchctl print "$GUI/$NS.$1" > /dev/null 2>&1; }
running() {
  local s
  s=$(launchctl print "$GUI/$NS.$1" 2> /dev/null) || return 1
  [[ "$s" == *"state = running"* ]]
}
port_busy() {
  "$PY" -c 'import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try: s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError: sys.exit(0)
sys.exit(1)' "$COCKPIT_PORT"
}

stop_role() {  # <role>
  local label=$NS.$1 n
  if ! loaded "$1"; then
    say "      $1：未加载，跳过"
    return 0
  fi
  launchctl bootout "$GUI/$label" 2> "$OUT/bootout-$1.err" || {
    loaded "$1" && { cat "$OUT/bootout-$1.err" >&2; return 1; }
  }
  for n in {1..30}; do
    loaded "$1" || { say "      $1：已停"; return 0; }
    sleep 1
  done
  print -u2 -r -- "      $1：bootout 后 30 秒仍在"
  return 1
}

bootstrap_role() {  # <role>，失败重试
  local plist=$LA/$NS.$1.plist n
  if [[ ! -f "$plist" ]]; then
    say "      $1：没有 plist（未安装），跳过"
    return 0
  fi
  for n in 1 2 3 4 5; do
    if loaded "$1"; then
      say "      $1：已加载"
      return 0
    fi
    if launchctl bootstrap "$GUI" "$plist" 2> "$OUT/bootstrap-$1.err"; then
      say "      $1：已 bootstrap"
      return 0
    fi
    print -u2 -r -- "      $1：bootstrap 第 $n 次失败：$(cat "$OUT/bootstrap-$1.err")"
    sleep $(( n * 5 ))
  done
  loaded "$1"
}

start_services() {
  local role rc=0
  for role in $ROLES_START; do
    bootstrap_role "$role" || rc=1
  done
  return $rc
}

step "d) ws-7d 补 xai 凭证槽位"
REPAIR=(scripts/repair_workspace_lane_parity.py --workspace "$SLUG" --no-sec-name-lookup)
"$PY" $REPAIR --json > "$OUT/repair-dryrun.json" 2> "$OUT/repair-dryrun.err" \
  || die "repair dry-run 失败，见 $OUT/repair-dryrun.err"
"$PY" $REPAIR > "$OUT/repair-dryrun.txt" 2>&1 || true
ACTIONS=$(jq_py "$OUT/repair-dryrun.json" '" ".join(a["kind"] for a in d["actions"])')
say "   dry-run 将补：${ACTIONS:-（无）}"
if [[ -z "$ACTIONS" ]]; then
  say "   没有需要补的东西，跳过（不重启服务）。"
else
  [[ "$ACTIONS" == *credential_slots* ]] || say "   注意：dry-run 里没有 credential_slots，只会补上面列出的项目。"
  say "   停止 ws-7d 服务（control → controller → thesis-impact → 排空 → writer）"
  SERVICES_STOPPED=1
  for role in $ROLES_STOP; do
    stop_role "$role" || die "停止 $role 失败"
  done
  say "      等待 writer 的 lane 子进程结束（最多 10 分钟）……"
  "$RELEASE_PY" -m dalton_core.launch_drain --state-dir "$W7D_STATE" --timeout 600 \
    > "$OUT/drain.out" 2>&1 || die "lane 排空失败，writer 仍在运行（见 $OUT/drain.out）"
  stop_role writer || die "停止 writer 失败"
  for n in {1..60}; do
    port_busy || break
    sleep 1
  done
  port_busy && die "服务已停，但 cockpit 端口 $COCKPIT_PORT 60 秒后仍被占用"
  say "      服务已全部停止，sleep 10 秒"
  sleep 10

  say "   repair --apply"
  "$PY" $REPAIR --apply --json > "$OUT/repair-apply.json" 2> "$OUT/repair-apply.err" \
    || die "repair --apply 失败，见 $OUT/repair-apply.err"
  [[ $(jq_py "$OUT/repair-apply.json" 'd["status"]') == "applied" ]] \
    || die "repair --apply 没有返回 applied（见 $OUT/repair-apply.json）"
  jq_py "$OUT/repair-apply.json" '"\n".join("      ["+r["result"]+"] "+r["target"] for r in d["performed"])'

  CONTROL_PLIST=$LA/$NS.control.plist
  if [[ -f "$CONTROL_PLIST" ]]; then
    PT=$(plutil -extract ProcessType raw "$CONTROL_PLIST" 2> /dev/null || print -r -- "(无)")
    say "   control 的 ProcessType：$PT"
    if [[ "$PT" != "Standard" ]]; then
      plutil -replace ProcessType -string Standard "$CONTROL_PLIST" || die "改写 ProcessType 失败"
      PT=$(plutil -extract ProcessType raw "$CONTROL_PLIST")
      [[ "$PT" == "Standard" ]] || die "ProcessType 改写后仍然是 $PT"
      say "   已改回 Standard"
    fi
  fi

  say "   sleep 10 秒后拉起服务（立即 bootstrap 曾报 5: Input/output error）"
  sleep 10
  start_services || die "有服务 bootstrap 失败"
  SERVICES_STOPPED=0
  say "   确认服务在运行"
  for n in {1..60}; do
    MISSING=()
    for role in writer controller control; do
      [[ -f "$LA/$NS.$role.plist" ]] || continue
      running "$role" || MISSING+=($role)
    done
    if [[ -f "$LA/$NS.thesis-impact.plist" ]] && ! loaded thesis-impact; then
      MISSING+=(thesis-impact)
    fi
    (( ${#MISSING} == 0 )) && break
    sleep 2
  done
  (( ${#MISSING} == 0 )) || die "这些服务 2 分钟后仍没在运行：${MISSING[*]}"
  for role in writer controller control thesis-impact; do
    if [[ ! -f "$LA/$NS.$role.plist" ]]; then
      say "      $role：未安装（没有 plist）"
    elif [[ $role == thesis-impact ]]; then
      say "      $role：已加载（按 StartInterval 定时运行）"
    else
      say "      $role：running"
    fi
  done
fi

# ---------------------------------------------------------------------------
# e) 修复后 parity

step "e) 修复后：只读 parity 检查"
"$PY" scripts/check_workspace_parity.py > "$OUT/parity-after.txt" 2>&1 \
  || die "parity 检查运行失败，见 $OUT/parity-after.txt"
grep -E "^==|^   mission|drift|GAP" "$OUT/parity-after.txt" || true
"$PY" scripts/check_workspace_parity.py --json > "$OUT/parity-after.json" 2> /dev/null \
  || die "parity --json 运行失败"
"$PY" - "$OUT/parity-after.json" "$SLUG" <<'PYEOF' || die "修复后 legacy / ws-7d 仍有 drift 或路由/凭证类 gap（见上）"
import json, sys
data = json.load(open(sys.argv[1]))
wanted = {"legacy", sys.argv[2]}
blocking, other = [], []
for env in data["environments"]:
    if env["environment"] not in wanted:
        continue
    for row in env["rows"]:
        routing_or_credential = any(key in row["check"] for key in ("routing", "credential"))
        if row["status"] == "drift" or (row["status"] == "gap" and routing_or_credential):
            blocking.append(f"{env['environment']}: {row['status']} {row['check']}")
        elif row["status"] == "gap":
            other.append(f"{env['environment']}: gap {row['check']}")
for line in other:
    print(f"   （不在本次范围内）{line}")
for line in blocking:
    print(f"!! {line}", file=sys.stderr)
sys.exit(1 if blocking else 0)
PYEOF
say "   legacy 与 ws-7d：没有 drift，没有路由/凭证类 gap。"
for pair in "legacy|$LEGACY_STATE" "ws-7d|$W7D_STATE"; do
  NAME=${pair%%|*}; STATE=${pair#*|}
  restore_dry_run "$NAME" "$STATE" "$OUT/restore-$NAME-final.json" || die "$NAME：最终核验 dry-run 失败"
  STATUS=$(jq_py "$OUT/restore-$NAME-final.json" 'd["status"]')
  AFFECTED=$(jq_py "$OUT/restore-$NAME-final.json" 'len(d["affected_routes"])')
  say "   $NAME：谓词 $STATUS，受影响的受约束路由 $AFFECTED 条"
  [[ "$STATUS" == "already-restored" && "$AFFECTED" == "0" ]] \
    || die "$NAME：最终核验不通过（见 $OUT/restore-$NAME-final.json）"
done

step "完成。所有输出在 $OUT"
