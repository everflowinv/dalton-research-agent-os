# 本机研究环境枚举（owner 脚本共用，source 后使用）。
#
# 为什么：09-24 以来的 ops 脚本都把 legacy 和 ws-7d 两个 state 目录写死在脚本里，
# 新建的环境（例如 ws-e399）一个都覆盖不到。这里改为从 ~/.dalton/manager.json 枚举：
# 用 release venv 里的 dalton_core.model_routing_sync.host_environments（只读：解析
# manager.json、legacy 的 service.json 和各工作区 workspace.json，不开任何数据库）。
#
# 用法：
#   source ~/Projects/dalton-research-agent-os/docs/ops/host-environments.zsh
#   dalton_release_py                 # 打印 manager.json release_path 下的 python
#   dalton_environments               # 每行：environment_id<TAB>slug<TAB>state_dir<TAB>workspace_manifest（legacy 的 manifest 为空）
#   dalton_environments_matching "$ONLY"   # 同上，只保留 slug/id 等于或以 $ONLY 开头的；ONLY 为空或 all 则全部
#
# 可调的环境变量：
#   MANAGER=<manager.json 路径>   默认 ~/.dalton/manager.json
#   PY=<python>                   默认 manager.json 的 release_path + /bin/python

: ${MANAGER:=$HOME/.dalton/manager.json}

dalton_release_py() {
  if [[ -n ${PY:-} ]]; then print -r -- "$PY"; return; fi
  print -r -- "$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_path"])' \
    "$MANAGER" 2>/dev/null)/bin/python"
}

dalton_environments() {
  local py
  py=$(dalton_release_py)
  if [[ ! -x $py ]]; then
    echo "找不到 release venv 的 python：$py（设置 PY=... 后重跑）" >&2
    return 2
  fi
  env -u PYTHONPATH "$py" - "$MANAGER" <<'PYEOF'
import json, sys
from pathlib import Path
from dalton_core.model_routing_sync import host_environments

manager = Path(sys.argv[1]).expanduser()
config = json.loads(manager.read_text(encoding="utf-8"))
host_root = Path(config.get("host_root") or manager.parent).expanduser()
for env in host_environments(manager):
    if env.environment_id == "legacy":
        manifest = ""
    else:
        manifest = str(host_root / "workspaces" / env.slug / "workspace.json")
    print("\t".join([env.environment_id, env.slug, str(env.state_dir), manifest]))
PYEOF
}

dalton_environments_matching() {  # $1 = ONLY（空或 all = 全部）
  local only=${1:-all}
  dalton_environments | while IFS=$'\t' read -r id slug state manifest; do
    if [[ $only == all || $slug == $only* || $id == $only* ]]; then
      print -r -- "$id"$'\t'"$slug"$'\t'"$state"$'\t'"$manifest"
    fi
  done
}
