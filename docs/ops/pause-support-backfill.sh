#!/bin/zsh
# 暂停（或恢复）晨报 claim 核验的存量回补。
# 背景：2026-09-26 发现 verifier 会把带日期、发言人的陈述误判为 not_supported，回补因此误撤回 claim。
#       修复部署前先暂停回补；入账前的核验不受影响，照常运行。
#
# 2026-09-30：不再写死 legacy + ws-7d 两个路径，改为从 ~/.dalton/manager.json 枚举本机全部研究环境
#   （docs/ops/host-environments.zsh；ws-e399 这类新环境以前会被漏掉）。
#
# 用法：
#   zsh pause-support-backfill.sh              # dry-run：列出每个环境当前的设置和将写入的值，不写
#   zsh pause-support-backfill.sh pause        # 暂停（backfill_batches_per_run = 0）
#   zsh pause-support-backfill.sh resume       # 恢复（删除该设置，回到默认值 1）
#   ONLY=ws-e399 zsh pause-support-backfill.sh pause   # 只处理一个环境（slug 前缀或 legacy）
# 设置在下一次抽取运行开始时生效，不需要重启服务。文件不存在时会新建（其余键保持默认）。

set -u
HERE=${0:A:h}
source "$HERE/host-environments.zsh"
MODE=${1:-dry-run}
ONLY=${ONLY:-all}
case $MODE in dry-run|pause|resume) ;; *) echo "未知参数：$MODE（可用：不带参数 = dry-run，pause，resume）" >&2; exit 2 ;; esac

ENVS=$(dalton_environments_matching "$ONLY") || exit 2
[[ -z $ENVS ]] && { echo "没有匹配 ONLY=$ONLY 的研究环境" >&2; exit 2; }
print -r -- "$ENVS" | while IFS=$'\t' read -r ID SLUG S MANIFEST; do
  F="$S/claim-support-verification.json"
  /usr/bin/python3 - "$F" "$MODE" "$SLUG" <<'PY'
import json, os, sys
path, mode, slug = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path)) if os.path.exists(path) else {}
target = dict(data)
if mode == "resume":
    target.pop("backfill_batches_per_run", None)
else:
    target["backfill_batches_per_run"] = 0
if mode == "dry-run":
    print(f"{slug}: {path}\n   现在：{json.dumps(data, ensure_ascii=False)}"
          f"\n   pause 后：{json.dumps(target, ensure_ascii=False)}")
    sys.exit(0)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(json.dumps(target, indent=2, ensure_ascii=False) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(mode, slug, path, target)
PY
done
[[ $MODE == dry-run ]] && echo "确认后执行：zsh $0 pause（或 resume）"
