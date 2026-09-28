#!/bin/zsh
# 批次 2026-09-28g 部署后，owner 依次执行的全部步骤，一条命令跑完：
#   1) 恢复 independence predicates（legacy、ws-7d 各一次），对齐 ws-7d 的模型路由，补 ws-7d 的 xai 凭证槽位（会自动停、再起 ws-7d 的服务）；
#   2) 撤回同一文档里重复入账的数字 claim（ws-7d 4 条、legacy 6 条；这是 human_judgment 退役，之后还能恢复）。
# 用法：zsh ~/Projects/dalton-research-agent-os/docs/ops/run-batch-g-post-deploy.sh
set -u
D=~/Projects/dalton-research-agent-os/docs/ops
echo "######## 1/3 independence predicates、路由、凭证槽位"
zsh $D/run-2026-09-28-restore-independence-and-parity-steps.sh || { echo "!! 第 1 步失败，停止。请把上面的输出发给我。" >&2; exit 1; }
echo "######## 2/3 撤回重复的数字 claim"
zsh $D/withdraw-document-figure-duplicates-2026-09-28.sh apply || { echo "!! 第 2 步失败。请把上面的输出发给我。" >&2; exit 1; }
echo "######## 3/3 签入 FY−9M 第四季推导规则"
zsh $D/sign-fy-minus-9m-rule-2026-09-28.sh apply || { echo "!! 第 3 步失败。请把上面的输出发给我。" >&2; exit 1; }
echo "######## 全部完成"
