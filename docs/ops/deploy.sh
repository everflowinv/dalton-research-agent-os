#!/bin/zsh
# 部署 main 当前的 HEAD：先构建 release，再执行切换。所有长命令都写在这里，避免终端粘贴时把长行拆断。
# 用法：zsh ~/Projects/dalton-research-agent-os/docs/ops/deploy.sh
set -e
REPO=${REPO:-"$HOME/Projects/dalton-research-agent-os"}
cd "$REPO"
# pypi 偶尔超时：放宽 pip 超时；构建失败时清掉半成品目录（没有 release-manifest.json、也没被任何服务引用），最多重试 3 次。
export PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=8
# 发布必须从干净的 HEAD 构建：先检查，不干净就直接说明是哪些文件（这不是网络问题，重试没有用）。
DIRTY=$(git status --porcelain)
if [[ -n $DIRTY ]]; then
  echo "!! 工作树不干净，不能构建发布。下面这些文件未提交：" >&2
  echo "$DIRTY" >&2
  echo "   把上面的输出发给我，我来提交或移走后再部署。" >&2
  exit 1
fi
OUT=/tmp/dalton-build-$(date -u +%Y%m%dT%H%M%SZ).json
MARK=$(mktemp /tmp/dalton-build-start.XXXXXX)   # 只清理这次构建期间新建的目录，旧发布（回滚目标）一律不碰
for attempt in 1 2 3; do
  if .venv/bin/python scripts/build_release.py --apply > "$OUT"; then break; fi
  echo "!! 第 $attempt 次构建失败（多半是网络），清理半成品后重试……" >&2
  for d in ~/.dalton/runtime/releases/*(N/); do
    h=${d:t}
    if [[ $d -nt $MARK ]] && [[ ! -e $d/release-manifest.json ]] && ! grep -rqs "$h" ~/Library/LaunchAgents ~/.dalton/manager.json; then
      echo "   删除半成品 $h" >&2; rm -rf "$d"
    fi
  done
  [[ $attempt == 3 ]] && { echo "!! 连续 3 次构建失败，停止。请稍后再试或把上面的报错发给我。" >&2; exit 1; }
  sleep 30
done
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
acts = (d.get("applied") or {}).get("actions", [])
retried_ok = {a.split("retry ", 1)[1].split(" ->")[0] for a in acts if a.startswith("retry ") and a.rstrip().endswith("-> 0")}
# bootout 返回 3（No such process）只说明服务本来就没在跑，随后的 bootstrap 才是关键，不算失败。
not_loaded = [a for a in acts if " bootout " in a and "No such process" in a]
bad = [a for a in acts if "->" in a and not a.rstrip().endswith("-> 0")
       and not a.startswith("retry ") and a.split(" ->")[0] not in retried_ok
       and a not in not_loaded]
if not_loaded:
    print("切换前本来就没在运行（已重新拉起）：", len(not_loaded), "个")
print("首次失败但重试成功：", len(retried_ok), "个" if retried_ok else "")
print("仍然失败的 launchctl：", bad or "无")
print("完整输出:", sys.argv[1])
PY
sleep 60
launchctl list | grep -E "space.lumos.dalton|com.dalton" | awk '$1=="-" && $3!="com.dalton.log-rotate"{print "!! 未运行:",$3}'
echo "== 已部署 $NEW（源 $(git rev-parse --short HEAD)）"
