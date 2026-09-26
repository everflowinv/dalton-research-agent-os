#!/bin/zsh
# 批次 2026-09-25d 部署后需要 owner 执行的步骤（2026-09-26）。
# 用法：zsh ~/Projects/dalton-research-agent-os/docs/ops/run-2026-09-26-batch-d-owner-steps.sh
# 每一步都以 human:owner 身份经 writer 的正式入口写入。某条 hold 如果已经被车道自动恢复，
# 对应那一行会报错，属于正常情况，脚本会继续往下执行。

PY=~/.dalton/runtime/releases/41fe150af11aeabd7ef467366b5f6388113642761a961621a47bbd0409503bab/venv/bin/python
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
WS_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json

# writer 刚重启、任务积压时可能超时，报 "writer service is unavailable"；最多重试 3 次，每次间隔 20 秒。
try() {
  for i in 1 2 3; do
    "$@" && return 0
    echo "  ...第 $i 次失败，20 秒后重试" >&2
    sleep 20
  done
  echo "  !! 放弃：$*" >&2
  return 1
}

echo "== 1/4 legacy：authorize-unproved（4 条）"
for r in b2f1a00d3546674012131452f9623bc6 ddf3a04b3c9209ac50c3ba8c0c805e86 6a2bcd446237e1bd9732690b4b542b3a a9e588b02de9c7a713167cdafec2d9f1; do
  try $PY -m dalton_core.document_recovery_cli authorize-unproved --state-dir "$L" --admission-ref "mission-document-research-admission:$r" --max-cost-usd 1.0 --actor human:owner --apply
done

echo "== 2/4 legacy：authorize-paid（918307dc）"
try $PY -m dalton_core.document_recovery_cli authorize-paid --state-dir "$L" --admission-ref mission-document-research-admission:918307dc4626f6a8d549501eb7e193a9 --max-cost-usd 1.0 --actor human:owner --apply

echo "== 3/4 ws-7d：authorize-unproved（3 条）"
export DALTON_WORKSPACE_MANIFEST=$WS_MANIFEST
for r in bec19d089314e5d4669702639ba1a296 c737cc83e9281d8689805b104653e0e3 4225dbd968a0226c11a88b5f9439d539; do
  try $PY -m dalton_core.document_recovery_cli authorize-unproved --state-dir "$W" --admission-ref "mission-document-research-admission:$r" --max-cost-usd 1.0 --actor human:owner --apply
done

echo "== 4/4 ws-7d：撤回 6 个 SEO 统计汇编页上的 58 条 claim"
# 选择集已由 dry-run 确认（2026-09-26）：TechnologyChecker Google Cloud 16 条、3 个 Amazon Ads 统计页 36 条、
# Azure 统计页 5 条、M365 统计页 1 条。如果这之后又入账了同类 claim，选择集的哈希会变，脚本会被拒绝执行，
# 这时重新跑一次 dry-run 再确认即可。
try $PY -m dalton_core.claim_admission_cli retire --state-dir "$W" --from-replay statistics_compilation --from-replay temporal_impossibility --reason "SEO 统计汇编页：数字无原始出处" --expect-selection d38624a4350936a29bfaab88b0e10cd5448aed0f21c74ccdcfcb31203a2e9693 --apply --actor human:owner
unset DALTON_WORKSPACE_MANIFEST

echo "== 完成"
