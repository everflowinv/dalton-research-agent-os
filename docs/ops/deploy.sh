#!/bin/zsh
# 部署 main 当前的 HEAD：先构建 release，再执行切换。所有长命令都写在这里，避免终端粘贴时把长行拆断。
# 用法：zsh ~/Projects/dalton-research-agent-os/docs/ops/deploy.sh
set -e
cd ~/Projects/dalton-research-agent-os
OUT=/tmp/dalton-build-$(date -u +%Y%m%dT%H%M%SZ).json
.venv/bin/python scripts/build_release.py --apply | tee "$OUT"
NEW=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['release_hash'])" "$OUT")
echo "== release $NEW"
SW=/tmp/dalton-switch-$NEW.json
# 切换脚本在 controller 心跳还没推进时会返回非 0，但切换本身已经完成；这里不中断，照常打印摘要。
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit $(git rev-parse HEAD) --apply > "$SW" || echo "(release_switch 退出码 $?；请看下面的摘要)"
python3 - "$SW" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
h = d.get("health", {})
print("health ok:", h.get("ok"), "| every_agent_on_target:", h.get("every_agent_on_target"),
      "| heartbeat_advanced:", h.get("controller_heartbeat_advanced"), "| writer_socket:", h.get("writer_socket_readable"))
bad = [a for a in (d.get("applied") or {}).get("actions", []) if "->" in a and not a.rstrip().endswith("-> 0")]
print("非 0 的 launchctl 结果：", bad or "无")
print("完整输出:", sys.argv[1])
PY
sleep 60
launchctl list | grep -E "space.lumos.dalton|com.dalton" | awk '$1=="-" && $3!="com.dalton.log-rotate"{print "!! 未运行:",$3}'
echo "== 已部署 $NEW（源 $(git rev-parse --short HEAD)）"
