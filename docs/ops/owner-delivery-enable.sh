#!/bin/zsh
# 开通（或关闭）某个环境的飞书推送（vision-gap #13，分支 feat/feishu-delivery）。
# 适用于任何环境：按 state 目录参数化，legacy、ws-7d、ws-e399、以后新建的环境都用同一个脚本。
#
# 推送什么（中文，与 cockpit 用词一致）：
#   - 每周研究 Brief 发布：每期一条，带本期变化摘要和 cockpit 链接；
#   - 「需要你处理」摘要：投资 call 提案、投资备忘录等你裁决时，最多每 N 小时一条（默认 6），
#     绝不一件一条；
#   - 模型回退通知：立即，但最多每 N 分钟一条（默认 60），同一时段的几条合成一条；
#   - 你自己的紧急停机 / 恢复全部运行：立即。
#   - 所有消息合计每天最多 N 条（默认 12，按 Asia/Shanghai 计日），按上面的优先级花。
#   只推送批准之后发生的事（批准不会把历史周报补发一遍）；摘要描述的是此刻还在等你的事。
#
# 前提：
#   1. 车道不需要任何安装步骤：writer 启动时按代码（lane_registry）补齐自己缺的车道参数，
#      owner_delivery 车道在每个环境都常开，是否发送只由合同文件决定（batch t 起；deploy.sh
#      切换 release 时重启 writer 即生效，不需要重新渲染 plist）。
#      合同文件 <state>/connector-governance/owner-delivery-feishu-v1.json：install.sh 和新建
#      workspace 会先放一份 proposed（没有接收人）；本脚本 apply 时按当前代码的条款重新生成并
#      写成 approved（带 chat id），文件不存在也直接写——即播种并批准；--revoke 写回 proposed。
#   2. OpenClaw 的飞书机器人已经在目标群里。chat id（oc_ 开头）可以在宿主上用 feishu_chat
#      工具 action=info 核对；本脚本不替你调用任何宿主工具，也不发任何消息。
#
# 用法（默认 dry-run，只打印将写入的审批记录；加 apply 才写）：
#   zsh docs/ops/owner-delivery-enable.sh --state-dir <state> --chat-id oc_xxx --approved-by human:<你> [--account <OpenClaw 飞书账号>]
#   zsh docs/ops/owner-delivery-enable.sh --state-dir <state> --chat-id oc_xxx --approved-by human:<你> apply
#   zsh docs/ops/owner-delivery-enable.sh --state-dir <state> --revoke [apply]      # 关闭：回到 proposed
#   zsh docs/ops/owner-delivery-enable.sh --state-dir <state> --status              # 只读：合同状态、参数、最近回执
#
#   legacy 的 state 目录：~/Library/Application Support/Dalton/state/dalton-core
#   （可以是指向外置盘状态目录的符号链接；保留逻辑路径即可）。
#   workspace 的 state 目录：manager.json 里该环境的 state_dir。
#
# 可调参数（不需要就不用写，默认值在代码里，所有环境同一份代码）：在该环境的 service.json
# 顶层加一个 owner_delivery 块，只能覆盖这些键：
#   {"owner_delivery": {"digest_interval_hours": 6, "daily_message_cap": 12,
#                       "fallback_min_interval_minutes": 60, "retry_after_minutes": 30,
#                       "poll_interval_minutes": 10, "day_timezone": "Asia/Shanghai",
#                       "cockpit_base_url": "https://<tailscale 主机>:<端口>/",
#                       "openclaw_executable": "/opt/homebrew/bin/openclaw",
#                       "send_timeout_seconds": 60}}
#   cockpit_base_url 不写时取 service.json 的 control（tailscale_host:port）；
#   openclaw_executable 不写时取 outbox 块里的，或 PATH 上的 openclaw。
#
# 做什么：只写 <state>/connector-governance/owner-delivery-feishu-v1.json（0600，旧文件先另存为
#   .pre-<UTC> 备份）。不部署、不重启、不碰 core.sqlite、不访问外部服务、不发消息。
# 之后：writer 的下一次 tick 看到合同已批准，就以子进程方式发第一轮（发送经由 OpenClaw 宿主工具
#   openclaw message send --channel feishu）；回执在 <state>/owner-delivery.sqlite 的
#   delivery_receipts 表（只追加），用 --status 查看。

set -u
REPO=${REPO:-${0:A:h:h:h}}
PYTHON=${PYTHON:-$REPO/.venv/bin/python}
[[ -x $PYTHON ]] || PYTHON=$HOME/Projects/dalton-research-agent-os/.venv/bin/python

STATE="" CHAT="" BY="" ACCOUNT="" ACTION=approve MODE=dry-run
while (( $# )); do
  case $1 in
    --state-dir) STATE=$2; shift 2 ;;
    --chat-id) CHAT=$2; shift 2 ;;
    --approved-by) BY=$2; shift 2 ;;
    --account) ACCOUNT=$2; shift 2 ;;
    --revoke) ACTION=revoke; shift ;;
    --status) ACTION=status; shift ;;
    apply) MODE=apply; shift ;;
    dry-run) MODE=dry-run; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
[[ -n $STATE ]] || { echo "!! 需要 --state-dir <环境的 state 目录>" >&2; exit 2; }
[[ -f $STATE/core.sqlite ]] || { echo "!! $STATE 下没有 core.sqlite，不像一个环境的 state 目录" >&2; exit 2; }

export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
echo "== 环境 state：$STATE"
echo "== 模式：$ACTION / $MODE"

case $ACTION in
  status)
    exec "$PYTHON" -m dalton_core.owner_delivery status --state-dir "$STATE" ;;
  revoke)
    args=(revoke --state-dir "$STATE") ;;
  approve)
    [[ -n $CHAT ]] || { echo "!! 需要 --chat-id oc_...（飞书群聊 id）" >&2; exit 2; }
    [[ $BY == human:?* ]] || { echo "!! 需要 --approved-by human:<你>" >&2; exit 2; }
    args=(approve --state-dir "$STATE" --chat-id "$CHAT" --approved-by "$BY")
    [[ -n $ACCOUNT ]] && args+=(--account "$ACCOUNT") ;;
esac
[[ $MODE == apply ]] && args+=(--apply)
"$PYTHON" -m dalton_core.owner_delivery "${args[@]}" || exit $?
if [[ $MODE == dry-run ]]; then
  echo "== dry-run：什么都没写。确认无误后在命令末尾加 apply。"
else
  echo "== 已写入。下一次 writer tick 起生效；用 --status 看回执。"
fi
