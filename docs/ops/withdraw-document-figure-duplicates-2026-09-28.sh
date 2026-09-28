#!/bin/zsh
# 撤回同一文档里重复入账的数字 claim（2026-09-28，分支 fix/tenk-q4-and-figure-dedupe）。
#
# 背景：mission-verified-figure 规则签入后，数字入账按“自由文本期间 + figure 行 id”判重，
# 同一份 10-K 里同一个数因为期间写法不同（"2025" / "full year 2025" /
# "Year Ended December 31, 2025"）或精度不同（200.97 billion / 200966 million）被当成几个数。
# ws-7d：META 2025 收入 3 条、净利润 2 条（其一标签写成 "net income adjusted for certain
# non-cash items"），AMZN 2025 收入 2 条（标签 "Consolidated"）。legacy 同样有 6 条。
# 修复后同一文档按 (公司, 指标, 期间起止日期, 数值×量级) 只入账一次；已入账的重复需要人工撤回
# （human_judgment 退役，可再恢复）。每组保留的那条（有申报行佐证 / 精度最高 / 最早写入）不动。
#
# 用法：
#   zsh docs/ops/withdraw-document-figure-duplicates-2026-09-28.sh
#       默认只读：先用新规则重放判重（mode=ro），核对清单没有变化，再对下面的 claim 做 dry-run，
#       打印将撤回的内容和 selection_sha256。
#   zsh docs/ops/withdraw-document-figure-duplicates-2026-09-28.sh apply
#       真正撤回：以 human:owner 身份经 writer 的 retire_claim_by_hand 入口写入（对应环境的 writer
#       必须在运行）。ACTOR=human:<名字> 可以改执行人；ONLY=ws7d 或 ONLY=legacy 只处理一个环境。

WT=~/Projects/dalton-research-agent-os
BR=${BR:-$HOME/Projects/dalton-wt-q4}
PY=$WT/.venv/bin/python
WS7D=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
LEGACY=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
ACTOR=${ACTOR:-human:owner}
ONLY=${ONLY:-all}
MODE=${1:-dry-run}
REASON="同一文档、同一指标、同一期间（起止日期）、同一数值的重复入账；保留有申报行佐证/精度最高/最早的一条（2026-09-28 判重规则回放）"

WS7D_REFS=(
  claim-version:523f63816fd8c9ea0ee65f6e44b349cae5083953ea33f3c719aceb85d8e23b61  # META 收入 200.97B「Total revenue for 2025」(2025) → 保留 200966M「Revenue」
  claim-version:90acf84aa04c191795931b0d66a4a5cb694948bd8b03f28d69d63adebb692184  # META 收入 200.97B「Total revenue for 2025」(full year 2025) → 同上
  claim-version:d54da950bda5ce719b465c225aaaf7220538ffa0a1981088095cb4e4a81f3534  # META 净利润 60.46B「net income adjusted for certain non-cash items」(2025) → 保留「Net income」
  claim-version:94d221bc88a422bfbf38b286da36e4f550e2535fbd95729e91e8729794df7a8a  # AMZN 收入 716924M「Consolidated」(2025) → 保留「Net sales」
)
LEGACY_REFS=(
  claim-version:45584af8e106127bb49bfcab9ed760dc5d965bab04d1c7b44a40035f9361c6b9  # CTSH metric:adj-eps 5.28 (year ended Dec 31, 2025) → 保留 (2025)
  claim-version:de2d8263a441e15be34da4fbeee7b019c333a88630845c24dcb5b7b5f331a61e  # CTSH metric:adjusted-eps 5.28 (Year Ended Dec 31, 2025) → 保留 (2025)
  claim-version:c6c0f0f368f454109ba8a8073a200456ebece91bffb77cfb235e26e9eea7fe7f  # CTSH adjusted-diluted-eps-growth 11.2 (2025) → 保留 (2025 compared to 2024)
  claim-version:68761dda3b10e6b14021fa76af50a41da348f8e51e9bd278473aeb334c31b4b9  # CTSH 同上 (YE 2025 vs YE 2024)
  claim-version:ceb0ffe270279ca63b2b574d6228fe9f5c9576d0157b02919609e62a539bc34f  # CTSH 同上「Adjusted Diluted EPS 1 up $0.53 or 11.2% from 2024」
  claim-version:87fde4e28f57c557db86ce9447202dc6077d5ec8031e5af31e10d5ce6bc6a51b  # DXC metric:adjusted-eps 3.23「Diluted EPS」(FY ended Mar 31, 2026) → 保留 (fiscal 2026)
)

run_env() {
  local NAME=$1 STATE=$2
  shift 2
  local REFS=("$@")
  local ARGS=()
  for R in $REFS; do ARGS+=(--claim-version-ref "$R"); done
  if [[ $MODE == dry-run ]]; then
    echo "== $NAME 1/2 用新判重规则重放（只读，分支代码 $BR）"
    if [[ -d $BR/src ]]; then
      local NOW
      NOW=$(PYTHONPATH=$BR/src $PY $BR/scripts/list_document_figure_duplicates.py \
        --state-dir "$STATE" --refs-only | sort)
      PYTHONPATH=$BR/src $PY $BR/scripts/list_document_figure_duplicates.py --state-dir "$STATE"
      if [[ "$NOW" != "$(print -l $REFS | sort)" ]]; then
        echo "!! 重放出的清单与本脚本里的清单不一致（可能已撤回一部分，或又有新的重复）；先核对再 apply" >&2
      else
        echo "清单一致：${#REFS} 条"
      fi
    else
      echo "（分支工作树不存在，跳过重放；合并后改用 BR=$WT）"
    fi
    echo
    echo "== $NAME 2/2 dry-run：将撤回的 claim（不写入）"
    PYTHONPATH=$WT/src $PY -m dalton_core.claim_admission_cli retire --state-dir "$STATE" \
      $ARGS --reason "$REASON"
    echo
  elif [[ $MODE == apply ]]; then
    echo "== $NAME 撤回 ${#REFS} 条（$ACTOR）"
    PYTHONPATH=$WT/src $PY -m dalton_core.claim_admission_cli retire --state-dir "$STATE" \
      $ARGS --reason "$REASON" --apply --actor "$ACTOR"
  fi
}

if [[ $MODE != dry-run && $MODE != apply ]]; then
  echo "未知参数：$MODE（可用：不带参数 = dry-run，或 apply）" >&2
  exit 2
fi
[[ $ONLY == all || $ONLY == ws7d ]] && run_env ws-7d "$WS7D" $WS7D_REFS
[[ $ONLY == all || $ONLY == legacy ]] && run_env legacy "$LEGACY" $LEGACY_REFS
if [[ $MODE == dry-run ]]; then
  echo "确认无误后执行：zsh $0 apply（可加 ONLY=ws7d 或 ONLY=legacy）"
fi
