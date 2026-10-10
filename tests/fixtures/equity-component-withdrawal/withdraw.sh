#!/bin/zsh
# Synthetic withdrawal execution fixture: never discovers a real environment.
# STATE_A/STATE_B and WORKSPACE_MANIFEST must name isolated test inputs.
# Use a released interpreter through PY or an explicit RELEASE_MANIFEST;
# MODE is dry-run unless the caller explicitly selects apply.
WT=${0:A:h:h:h:h}
BR=${BR:-$WT}
L=${STATE_A:?set STATE_A to the isolated standalone test state}
W=${STATE_B:?set STATE_B to the isolated managed test state}
WS_MANIFEST=${WORKSPACE_MANIFEST:?set WORKSPACE_MANIFEST to the isolated manifest}
if [[ -z ${PY:-} ]]; then
  : ${RELEASE_MANIFEST:?set PY or an isolated RELEASE_MANIFEST}
  PY=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_path"])' "$RELEASE_MANIFEST")/bin/python
fi
if [[ ! -x $PY ]]; then
  echo "release interpreter unavailable: $PY" >&2
  exit 2
fi
ACTOR=${ACTOR:-human:owner}
MODE=${1:-dry-run}
ONLY=${2:-}
if [[ $MODE != dry-run && $MODE != apply ]]; then
  echo "unknown mode: $MODE" >&2
  exit 2
fi
REFS_FILE=${0:A:h}/refs.tsv

SEL_A_ALPHA=${SEL_A_ALPHA:-811ca4f9dd5c53ae25b108450c21b6908399dcc29fc242fc82baedc9f89a4d15}
SEL_A_BETA=${SEL_A_BETA:-f6299639f4cb550d8b69bcf4e9177bc81546ad1e0420f1fb51bfb07ef6d00b01}
SEL_B_ALPHA=${SEL_B_ALPHA:-e53f133761ba4e8cc95b1d13ac26da5ef7dbc4612a4766f8d6ae5833604eb29c}
SEL_B_BETA=${SEL_B_BETA:-4770c750f17038f167b6f40b82fb9421f65503b1dc741a2aa1172fc49fd203a1}
SEL_C_ALPHA=${SEL_C_ALPHA:-0e02c0e8529dba95bd78b506706af40a571729545ecef03b3125c6fd39148ea3}
SEL_C_BETA=${SEL_C_BETA:-76ade95553509344ff7ac0065eaad23d41b80a0c4bb99018638c06459f7ec67a}
A_REASON="合成权益组成列不是业务分部。"
B_REASON="合成非分部属性不能当作业务分部。"
C_REASON="合成旧分部名称应退役，后续原规则可生成诚实的新指标名称。"
refs_of() { /usr/bin/awk -F'\t' -v g="$1" -v e="$2" '$1==g && $2==e {print $3}' "$REFS_FILE"; }
run_group() {  # name state reason expected-sha256 refs...
  local NAME=$1 STATE=$2 REASON=$3 SEL=$4; shift 4
  local ARGS=()
  for R in "$@"; do ARGS+=(--claim-version-ref "$R"); done
  echo "== $NAME（${#@} 条，$STATE）"
  if (( ${#@} == 0 )); then
    echo "（本组在该环境没有 claim，跳过）"; echo; return 0
  fi
  # The managed test state uses its explicit workspace manifest.
  if [[ $STATE == "$W" ]]; then export DALTON_WORKSPACE_MANIFEST=$WS_MANIFEST; else unset DALTON_WORKSPACE_MANIFEST; fi
  # 先 dry-run（只读，mode=ro）：打印将撤回的内容；哈希与记下的不一致时 CLI 报错退出。
  local DRY
  DRY=$($PY -m dalton_core.claim_admission_cli retire --state-dir "$STATE" \
    $ARGS --reason "$REASON" --expect-selection "$SEL")
  local RC=$?
  print -r -- "$DRY"
  # 旧 release 的 CLI 对逐条列出的 claim 不核对 --expect-selection，这里再核对一次。
  local GOT
  GOT=$(print -r -- "$DRY" | $PY -c 'import json,sys; print(json.load(sys.stdin).get("selection_sha256",""))' 2>/dev/null)
  if [[ $RC != 0 || $GOT != $SEL ]]; then
    echo "!! $NAME：选择集与核对过的不一致（得到 ${GOT:-无}，应为 $SEL）。先看上面的 dry-run，确认后用 SEL_… 覆盖再重跑；本组不执行。" >&2
    FAILED+=("$NAME")
    unset DALTON_WORKSPACE_MANIFEST
    echo
    return 1
  fi
  if [[ $MODE == apply ]]; then
    $PY -m dalton_core.claim_admission_cli retire --state-dir "$STATE" \
      $ARGS --reason "$REASON" --expect-selection "$SEL" --apply --actor "$ACTOR"
    local APPLY_RC=$?
    if (( APPLY_RC != 0 )); then
      echo "!! $NAME：apply 子命令失败（退出码 $APPLY_RC），本组没有成功完成。" >&2
      FAILED+=("$NAME")
      unset DALTON_WORKSPACE_MANIFEST
      echo
      return $APPLY_RC
    fi
    # A transport refusal never authorizes a broader selection on retry.
  fi
  unset DALTON_WORKSPACE_MANIFEST
  echo
}

FAILED=()
if [[ ! -f $REFS_FILE ]]; then exit 2; fi
if [[ -z $ONLY ]]; then
  if [[ $MODE == dry-run ]]; then
    PYTHONPATH=$BR/src $PY $BR/scripts/replay_equity_component_segment_claims_2026_09_30.py \
      --state-dir "$L" --state-dir "$W"
  fi
  run_group "A synthetic alpha" "$L" "$A_REASON" "$SEL_A_ALPHA" ${(f)"$(refs_of equity alpha)"}
  run_group "A synthetic beta" "$W" "$A_REASON" "$SEL_A_BETA" ${(f)"$(refs_of equity beta)"}
  run_group "B synthetic alpha" "$L" "$B_REASON" "$SEL_B_ALPHA" ${(f)"$(refs_of other alpha)"}
  run_group "B synthetic beta" "$W" "$B_REASON" "$SEL_B_BETA" ${(f)"$(refs_of other beta)"}
elif [[ $ONLY == C ]]; then
  run_group "C synthetic alpha" "$L" "$C_REASON" "$SEL_C_ALPHA" ${(f)"$(refs_of renamed alpha)"}
  run_group "C synthetic beta" "$W" "$C_REASON" "$SEL_C_BETA" ${(f)"$(refs_of renamed beta)"}
else
  echo "unknown group: $ONLY" >&2; exit 2
fi
if (( ${#FAILED} )); then exit 1; fi
if [[ $MODE == dry-run ]]; then echo "reviewed selections match; apply must be explicit"; fi
