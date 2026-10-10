#!/bin/zsh
# 提高出版 worker「研究语言修订」（research_language_revision）的每日费用上限。
# 默认上限 $25/天，其中低优先级内容（界面文字、周度自评）最多用 60%，即 $15。
# 设置写进 worker 配置的 purpose_daily_cap_usd；worker 每 5 分钟重新启动一次、每次都会重读配置，所以不需要重启服务。
# 用法：
#   zsh raise-language-revision-cap.sh            # 只预览（默认改成 $50：低优先级可用 $30）
#   zsh raise-language-revision-cap.sh 50 apply   # 写入；金额范围 0–100 美元
#   zsh raise-language-revision-cap.sh reset apply  # 删除这项设置，恢复默认 $25
# 验证：tail -1 ~/Library/Logs/Dalton/publication-worker.stdout.log 里 completed 大于 0，
#       日志中不再出现 "purpose_daily_cap_reached: research_language_revision ... limit 15000000"。

AMOUNT=${1:-50}
MODE=${2:-dry-run}
F="${WORKER_CONFIG:-$HOME/Library/Application Support/Dalton/state/dalton-core/research-publication-worker-config.json}"
/usr/bin/python3 - "$F" "$AMOUNT" "$MODE" <<'PY'
import json, os, shutil, sys, time
path, amount, mode = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))
caps = dict(data.get("purpose_daily_cap_usd") or {})
before = dict(caps)
if amount == "reset":
    caps.pop("research_language_revision", None)
else:
    value = float(amount)
    if not 0 <= value <= 100:
        sys.exit("金额必须在 0–100 美元之间")
    caps["research_language_revision"] = value
print("当前设置：", before or "（无，使用默认 $25）")
print("改为：    ", caps or "（无，使用默认 $25）")
if amount != "reset":
    print(f"低优先级可用：${float(amount) * 0.6:.2f}/天；高价值产物（档案、周报、有变化的判断等）可用全部 ${float(amount):.2f}")
if mode != "apply":
    print("（预览，未写入。确认后加 apply 重跑）")
    sys.exit(0)
if caps:
    data["purpose_daily_cap_usd"] = caps
else:
    data.pop("purpose_daily_cap_usd", None)
backup = f"{path}.bak-{time.strftime('%Y%m%dT%H%M%S')}"
shutil.copy2(path, backup)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("已写入。备份：", backup)
PY
