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

SEL_A=${SEL_A:-2f48e46c82c5d3c2d6d8e17336c4c31bf4e80ed58105c21540e48db06b6e20d4}
A_STATE=$L
A_REASON="同一合成原文、主体与引用跨度被重复阅读。"
A_REFS=(
  claim-version:9d18a2b05803521f45aaaff2e6539d80893d50b529002a756c2ca9743f5391d8
  claim-version:eee0c5b3b9f985db666a0071a983f4ab9f4318763c645c86ea62bd14ef3363d5
  claim-version:6172c666fb1d4d0923155ff6a09c1845c9bed371a99ac22a136436b0a8fe57a3
)

SEL_B1=${SEL_B1:-0602ddd65f5a2aad33152e62a9a98b96dd706c9ddc208925da14e42354b3448c}
B1_STATE=$W
B1_REASON="合成访谈的提问被错误归为受访者回答。"
B1_REFS=(
  claim-version:d9c5e41753a02c2342d5276bec838b3470814e4f91e9314067d18ac73627a1af
)

SEL_B2=${SEL_B2:-66e17c6572963203f7cd9ede8066a1b9691377880bd09932e38fd6d012c73bda}
B2_STATE=$L
B2_REASON="合成材料被挂到另一家公司。"
B2_REFS=(
  claim-version:fb00952fe92a8fa090ee33056df9c2ceedef43e4cc3a8bd3e8dff330430226fb
)

SEL_C=${SEL_C:-0861a5658448663c009d41ef0ae32b03db545045675e88b1f275851904ba9c53}
C_STATE=$L
C_REASON="同一次合成文档阅读里的重复事实，保留先入账版本。"
C_REFS=(
  claim-version:0de1d22e51c303b97ee66ff4b638cbde460c421b52aefef080d9878c73de2b0e
  claim-version:ec67ea00e78cceee4f1547369bbf4214eff619f75f949adfa04652a9e24debd6
  claim-version:a6dc973ee9a87693f9d98d2009cabe8e4d4f8acb4113c47355e833f72506c2a4
  claim-version:293f3b507acf521896b8dfc00899e1d3fae902860583e509134f02a84c96d0bf
  claim-version:2584f7c334b1d5cbc39e82f650ffe6ffe831292b6311fc1da1ea24ad2b802e0e
  claim-version:d4d0048c73e7d04aaf08fa184c5fd1b6d3235a99633d8712b386e62f8010dc6b
)

SEL_D=${SEL_D:-f991ff00ca6d505b448e8a2a403903ed0c80ea2db7d4972fe85b7daceef34ec0}
D_STATE=$L
D_REASON="合成厂商宣传不构成原始事实来源。"
D_REFS=(
  claim-version:b2b13edf5c4eea9a605ebf451370f6b2dbc798785b225dda4cf4995f7c2dfbb6
  claim-version:f84379182d1b79532a4980d6490fad4cf23cdb38b2b42bc497e798b82ba77bbe
  claim-version:582638c1c071379a82cf4ffcac71e0c056776eb1f7b0162a5c92d3fe8113d7bc
  claim-version:789b78694b334ae935be6172fd5d9424071ec924f60951c422bcb8e58f71f480
  claim-version:9d0bc7f2797481d690b366fc47791d9aa82ee0f6bf1e66c166c3c4cd448ddc4d
  claim-version:cce418d6747cb7477452d3b80f838e827e5f5a19fb8262aa6f086591cdaa3aa6
  claim-version:b635ed9ab5044b40f957cbf3ee35f1388cf03df8066e347662b91ed44fd41768
  claim-version:b3d02173421c778bfb803e4164f7b57b5f1e7acb42369f10de42271112b144c4
  claim-version:d4096004104f82bd6d247d0d5bd98f1a4fefe350e54e7400963d71e54a7d21ac
  claim-version:d51bc5ab9b88dae773a0855a64c7b93cc27c54c2ae10ada22a8c477512376fac
  claim-version:463bc2283b1e856a3c2c470f08936fa0dbae12ec337b69afb36e486288ea1baf
  claim-version:f6fbf04782c946dbea25e5a4ed3db2636eac332a7aaa34a65c821792f9cecbd4
  claim-version:6cb73c30aab307c3ca32261261c49775fe75b84c53e4e7209742a536a7877832
  claim-version:0885ec1c6a6e83c31a10dc7aa084e4a3cb70f39e73885c4b334e500741286a3c
)

SEL_X_ENV_A=${SEL_X_ENV_A:-f65f8086f014be82278bb50394a4585005280ad2e3759a491150694f0e34050c}
X_ENV_A_STATE=$L
X_ENV_A_REASON="合成提问不是回答。"
X_ENV_A_REFS=(
  claim-version:05974f1d6504ac75052a80c30dddff409c920bd693396cbb749175e2a423a7fd
)

SEL_X_ENV_B=${SEL_X_ENV_B:-835200a550db3005873e83805892670d84979aa7469c3415ad4dbb7a4e31212b}
X_ENV_B_STATE=$W
X_ENV_B_REASON="合成提问不是回答。"
X_ENV_B_REFS=(
  claim-version:c72390e18e32c242b3f213afd5cc746ae98d4cfb5ec31d57a829b423a9ededc9
)

run_group() {  # name state reason expected-sha256 refs...
  local NAME=$1 STATE=$2 REASON=$3 SEL=$4; shift 4
  local ARGS=()
  for R in "$@"; do ARGS+=(--claim-version-ref "$R"); done
  echo "== $NAME（${#@} 条，$STATE）"
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
if [[ -z $ONLY ]]; then
  if [[ $MODE == dry-run ]]; then
    PYTHONPATH=$BR/src $PY $BR/scripts/replay_claim_scope_2026_09_29.py \
      --state-dir "$L" --state-dir "$W" --since 2031-02-03T11:00 \
      --marketing-url https://vendor.example.invalid/alternatives
  fi
  run_group "A synthetic scope" "$A_STATE" "$A_REASON" "$SEL_A" $A_REFS
  run_group "B1 synthetic scope" "$B1_STATE" "$B1_REASON" "$SEL_B1" $B1_REFS
  run_group "B2 synthetic scope" "$B2_STATE" "$B2_REASON" "$SEL_B2" $B2_REFS
  run_group "C synthetic scope" "$C_STATE" "$C_REASON" "$SEL_C" $C_REFS
  run_group "D synthetic scope" "$D_STATE" "$D_REASON" "$SEL_D" $D_REFS
elif [[ $ONLY == X ]]; then
  run_group "X_ENV_A synthetic scope" "$X_ENV_A_STATE" "$X_ENV_A_REASON" "$SEL_X_ENV_A" $X_ENV_A_REFS
  run_group "X_ENV_B synthetic scope" "$X_ENV_B_STATE" "$X_ENV_B_REASON" "$SEL_X_ENV_B" $X_ENV_B_REFS
else
  echo "unknown group: $ONLY" >&2; exit 2
fi
if (( ${#FAILED} )); then exit 1; fi
if [[ $MODE == dry-run ]]; then
  echo "reviewed selections match; apply must be explicit"
fi
