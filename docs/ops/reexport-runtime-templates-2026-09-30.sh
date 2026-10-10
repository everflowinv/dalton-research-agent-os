#!/bin/zsh
# 重新导出新建研究环境用的两份运行模板，并让 ~/.dalton/manager.json 指向它们（2026-09-30，
# 分支 fix/workspace-config-inheritance；复盘报告第二部分 a2 / 清单第 4 项）。
#
# 为什么：manager.json 现在固定的是 09-16 的模型模板 v5（21 份模型配置）和服务模板 v7。
#   之后现有环境（legacy）又改了不少：第 22 份 quality-verifier-model-config.json（09-25 起）、
#   mission-document-draft 的单次输出上限 4096 -> 16000（09-30）等。新环境从模板起步，
#   这些都会以旧值被继承。创建时虽然会按现有环境重新对齐模型路由、凭证槽位和每次调用预算，
#   但模板里的其它字段（重试、run budget、服务引擎参数、配置份数）只能靠重新导出。
#   部署本分支后，驾驶舱「研究环境」页在模板落后时会显示警告，并指向本脚本。
#
# 做什么：
#   1. 用正在运行的 release（manager.json 的 release_path）从 legacy 导出：
#        模型模板：python -m dalton_core.workspace_model_setup export（只读 legacy 的路由库和日预算账本）
#        服务模板：python -m dalton_core.workspace_service_setup export（只读 legacy 的 service.json）
#      导出结果先写到 /tmp 下的临时目录。dry-run 到此为止：打印份数、是否含 quality-verifier、
#      起草定向回答的输出上限、日预算策略最新版本、sha256，以及与当前固定模板的差别。
#   2. apply：把两份文件放进 ~/.dalton/connections/（model-runtime-20260930-v6.json、
#      service-runtime-20260930-v8.json；同名文件已存在且内容不同则拒绝），权限 600；
#      备份 manager.json（manager.json.pre-runtime-templates-v6-<UTC>.json，600），
#      然后原子替换 manager.json：runtime_templates.model / .service 改成新路径和 sha256，
#      shared_readonly_paths 追加两条新路径。其它字段一个不动。
#
# 用法：
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/reexport-runtime-templates-2026-09-30.sh         # dry-run
#   zsh ~/Projects/dalton-research-agent-os/docs/ops/reexport-runtime-templates-2026-09-30.sh apply
#
# 可调的环境变量：
#   PY=<python>            默认 manager.json 的 release_path + /bin/python
#   LEGACY_STATE=<目录>    默认 manager.json legacy_workspace.state_dir，否则 ~/Library/Application Support/Dalton/state/dalton-core
#   LEGACY_CONFIG=<文件>   默认 manager.json legacy_workspace.config_path，否则 ~/Library/Application Support/Dalton/config/service.json
#   MODEL_OUT / SERVICE_OUT  目标文件名（默认见上）
#
# 不做什么：不部署、不重启、不改任何研究环境的状态目录、不访问外部服务。
# 模型模板导出会检查被钉住的链路都可路由；线上有过期/冷却中的模型时导出会失败并列出原因，
# 先修好线上策略（scripts/repair_brain_chains.py）再重跑。

set -u
setopt pipefail
MANAGER=${MANAGER:-$HOME/.dalton/manager.json}
MODE=${1:-dry-run}
[[ $MODE == dry-run || $MODE == apply ]] || { echo "未知参数：$MODE（可用：不带参数 = dry-run，apply）" >&2; exit 2; }

manager_get() {  # dotted key -> value or empty
  /usr/bin/python3 - "$MANAGER" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else value)
PY
}

if [[ -z ${PY:-} ]]; then PY="$(manager_get release_path)/bin/python"; fi
[[ -x $PY ]] || { echo "找不到 release venv 的 python：$PY" >&2; exit 2; }
LEGACY_STATE=${LEGACY_STATE:-$(manager_get legacy_workspace.state_dir)}
LEGACY_STATE=${LEGACY_STATE:-"$HOME/Library/Application Support/Dalton/state/dalton-core"}
LEGACY_CONFIG=${LEGACY_CONFIG:-$(manager_get legacy_workspace.config_path)}
LEGACY_CONFIG=${LEGACY_CONFIG:-"$HOME/Library/Application Support/Dalton/config/service.json"}
CONNECTIONS=${CONNECTIONS:-$HOME/.dalton/connections}
MODEL_OUT=${MODEL_OUT:-$CONNECTIONS/model-runtime-20260930-v6.json}
SERVICE_OUT=${SERVICE_OUT:-$CONNECTIONS/service-runtime-20260930-v8.json}
TMP=$(mktemp -d /tmp/reexport-templates-XXXXXX)
chmod 700 "$TMP"

echo "== release python：$PY"
echo "== legacy state：$LEGACY_STATE"
echo "== legacy service.json：$LEGACY_CONFIG"
case $LEGACY_CONFIG in *legacy-control*) echo "!! 不能用 ~/.dalton/legacy-control 下的旧副本导出" >&2; exit 2 ;; esac

echo "== 1/2 导出模型模板到 $TMP/model.json"
env -u PYTHONPATH "$PY" -m dalton_core.workspace_model_setup export \
  --source-state "$LEGACY_STATE" --output "$TMP/model.json" > "$TMP/model.receipt" \
  || { echo "!! 模型模板导出失败（见上方原因）" >&2; exit 1; }
echo "== 2/2 导出服务模板到 $TMP/service.json"
env -u PYTHONPATH "$PY" -m dalton_core.workspace_service_setup export \
  --source-config "$LEGACY_CONFIG" --output "$TMP/service.json" > "$TMP/service.receipt" \
  || { echo "!! 服务模板导出失败（见上方原因）" >&2; exit 1; }

/usr/bin/python3 - "$MANAGER" "$TMP/model.json" "$TMP/service.json" <<'PY'
import hashlib, json, sys
manager = json.load(open(sys.argv[1]))
new = {"model": sys.argv[2], "service": sys.argv[3]}
for name, path in new.items():
    raw = open(path, "rb").read(); value = json.loads(raw)
    pinned = manager.get("runtime_templates", {}).get(name, {})
    old = json.load(open(pinned["path"])) if pinned.get("path") else {}
    print(f"-- {name} 模板")
    print(f"   新 sha256：{hashlib.sha256(raw).hexdigest()}（content_hash {value['content_hash'][:16]}…）")
    print(f"   现固定：{pinned.get('path')}（sha256 {str(pinned.get('sha256'))[:16]}…）")
    if name == "model":
        configs = value["configs"]
        print(f"   模型配置份数：{len(configs)}（原 {len(old.get('configs', {}))}）；"
              f"含 quality-verifier：{'quality-verifier-model-config.json' in configs}")
        draft = configs.get("mission-document-draft-model-config.json", {})
        print("   起草定向回答的每次调用预算：",
              json.dumps(draft.get("purpose_call_budgets"), ensure_ascii=False))
        heads = sorted({c.get("budget_policy_ref") for c in configs.values()})
        print(f"   日预算策略：{heads}（共 {len(value['budget_policies'])} 个版本）")
        changed = sorted(n for n in configs if old.get("configs", {}).get(n) != configs[n])
        print(f"   与现固定模板不同的配置：{len(changed)} 份")
    else:
        thesis = value["operating"]["thesis_impact"]["config"]
        print(f"   thesis_impact.budget_policy_version_id：{thesis.get('budget_policy_version_id')}")
        changed = sorted(k for k in value["operating"]
                         if old.get("operating", {}).get(k) != value["operating"][k])
        print(f"   与现固定模板不同的段：{changed}")
PY

if [[ $MODE == dry-run ]]; then
  echo
  echo "dry-run：只写了 $TMP，~/.dalton 下什么都没改。确认后执行：zsh $0 apply"
  exit 0
fi

place() {  # tmp target
  if [[ -e $2 ]]; then
    if cmp -s "$1" "$2"; then echo "   $2 已存在且内容相同，沿用"; return 0; fi
    echo "!! $2 已存在且内容不同，拒绝覆盖（换 MODEL_OUT / SERVICE_OUT 再试）" >&2; return 1
  fi
  cp "$1" "$2.tmp.$$" && chmod 600 "$2.tmp.$$" && mv -n "$2.tmp.$$" "$2" && echo "   写入 $2"
}
place "$TMP/model.json" "$MODEL_OUT" || exit 1
place "$TMP/service.json" "$SERVICE_OUT" || exit 1

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="${MANAGER:h}/manager.json.pre-runtime-templates-v6-$STAMP.json"
cp -p "$MANAGER" "$BACKUP" && chmod 600 "$BACKUP" && echo "   备份 manager.json -> $BACKUP"
/usr/bin/python3 - "$MANAGER" "$MODEL_OUT" "$SERVICE_OUT" <<'PY'
import hashlib, json, os, sys
path, model, service = sys.argv[1:4]
before = open(path, "rb").read()
config = json.loads(before)
for name, target in (("model", model), ("service", service)):
    config.setdefault("runtime_templates", {})[name] = {
        "path": target, "sha256": hashlib.sha256(open(target, "rb").read()).hexdigest()}
    shared = config.setdefault("shared_readonly_paths", [])
    if target not in shared:
        shared.append(target)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(json.dumps(config, ensure_ascii=False, indent=4) + "\n")
os.chmod(tmp, 0o600)
if open(path, "rb").read() != before:
    os.unlink(tmp); sys.exit("!! manager.json 在读取后被改过，放弃；重跑即可")
os.replace(tmp, path)
print("   manager.json 已更新：runtime_templates ->", model, service)
PY
echo "== 完成。之后新建的研究环境从新模板起步；驾驶舱「研究环境」页的模板警告应消失（部署本分支后）。"
