#!/bin/zsh
# 把所有 Dalton 服务指向一个发布。
#
# 这个脚本原来自己做全部的事，而且做漏了两件：
#   1. 它的 glob 只有 space.lumos.dalton*.plist，所以
#      com.dalton.research-publication-worker.plist 从来没被切过 —— 服务在
#      37b73370，发布 worker 还停在 d1f2f062。
#   2. 它只改 plist，不改 ~/.dalton/manager.json、workspace.json、
#      current-release.json / current-runtime-config.json 和发布 worker 配置里的
#      publication_gate，于是"当前发布"同时有三个互相矛盾的答案。
#
# 这两件事不能分开做，所以实现搬到了 scripts/release_switch.py（它一次性校验、
# 一次性写、最后统一健康校验，并且默认 dry-run）。这里只做参数转发，保持既有
# 调用方式可用。
#
# 用法：
#   scripts/point_services_at_release.sh <release-dir>[/venv]            # 只看计划
#   scripts/point_services_at_release.sh <release-dir>[/venv] --apply    # 真的切
set -euo pipefail

RELEASE="${1:?usage: point_services_at_release.sh <release>[/venv] [--apply ...]}"
shift

here="${0:A:h}"
python="${DALTON_SWITCH_PYTHON:-$here/../.venv/bin/python}"
[[ -x "$python" ]] || python="$(command -v python3)"

exec "$python" "$here/release_switch.py" "$RELEASE" "$@"
