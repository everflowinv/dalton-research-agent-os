#!/bin/zsh
# 暂停（或恢复）晨报 claim 核验的存量回补。
# 背景：2026-09-26 发现 verifier 会把带日期、发言人的陈述误判为 not_supported，回补因此误撤回 claim。
#       修复部署前先暂停回补；入账前的核验不受影响，照常运行。
# 用法：
#   zsh pause-support-backfill.sh          # 暂停（backfill_batches_per_run = 0）
#   zsh pause-support-backfill.sh resume   # 恢复（删除该设置，回到默认值 1）
# 设置在下一次抽取运行开始时生效，不需要重启服务。

MODE=${1:-pause}
for S in "/Volumes/EveSSD/Dalton/legacy-state/dalton-core" "/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core"; do
  F="$S/claim-support-verification.json"
  python3 - "$F" "$MODE" <<'PY'
import json, os, sys
path, mode = sys.argv[1], sys.argv[2]
data = json.load(open(path)) if os.path.exists(path) else {}
if mode == "resume":
    data.pop("backfill_batches_per_run", None)
else:
    data["backfill_batches_per_run"] = 0
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(mode, path, data)
PY
done
