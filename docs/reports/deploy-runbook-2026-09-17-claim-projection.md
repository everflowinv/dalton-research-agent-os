# 2026-09-17 部署 runbook：公司档案车道拖垮 writer 的修复（源码 db30f19d）

自动模式的安全分类器把"生产部署"类动作拦在执行前，以下两步由 owner 在终端执行（Claude Code 里可用 `! <命令>` 直接跑）。修复内容与实测见 `docs/PROJECT_STATUS.md` 顶部条目。

## 症状与根因（已定位、已在源码修复、全量 9,181 + 10 项测试通过）

- 05:38 UTC 起每轮 tick 里 `dispatch_company_dossier` 都报 `writer did not finish the request in time`，tick 从 50 秒涨到 120 秒以上；cockpit 的「退回补充」等裁决要写的 `decide_deep_insight_gate` 与它排同一条 store 线程，排不进去就显示"保存决定暂时未完成"；研究目标页在 tick 期间读取 28 秒以上（前端 30 秒即放弃）。
- 根因：每家公司的来源指纹对每条 Claim 都把整个快照走一遍（3,031 × 3,400 ≈ 1,000 万次语义键），每个章节把 2.9 万条索引全部解码一遍，每家公司各取一份快照并哈希（`canonical_json` 每层都 dumps+loads）。定量结论入账把 Ledger 推到 1 万条后，5 家公司合计 58 秒/轮。
- 修复后用线上 Core 实测：单家公司指纹 55 秒 → 1.4 秒；一轮五家 58 秒 → 4.7 秒；快照哈希 6.2 秒 → 0.5 秒。字节级等价（新旧算法逐条比对）。

## 1. 构建 release（约 2–4 分钟；用线上同款 Homebrew python@3.14，已获完全磁盘访问）
```zsh
cd ~/Projects/dalton-research-agent-os
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260917.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260917.json'))['release_hash'])"); echo $NEW
R=~/.dalton/runtime/releases/$NEW
```

## 2. 切换全部 10 个 LaunchAgent 与指针（先看计划，再 --apply；退出码 3 = 健康校验未过，用旧 release `3687233129fd…` 再跑一次即回滚）
```zsh
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
.venv/bin/python -m dalton_core.launch_drain --state-dir "$S"          # 期望 drained: true
.venv/bin/python scripts/release_switch.py $R --source-commit db30f19d15b369833a3e7cbcb0886c5f72e3e96e
.venv/bin/python scripts/release_switch.py $R --source-commit db30f19d15b369833a3e7cbcb0886c5f72e3e96e --apply
```

## 3. 验证（切换后等两轮 tick，约 10 分钟）
```zsh
sqlite3 "file:$S/tick-ledger.sqlite?mode=ro" "select started_at, status_word, substr(counts_json,1,80) from tick_ledger_lanes where lane_operation='dispatch_company_dossier' order by started_at desc limit 3"   # 期望不再是 unavailable / RemoteError
sqlite3 "file:$S/tick-ledger.sqlite?mode=ro" "select started_at, round((julianday(ended_at)-julianday(started_at))*86400) from tick_ledger_ticks order by started_at desc limit 4"   # 期望 < 60s
grep -c "writer op failed (unmapped) operation=dispatch_company_dossier" ~/Library/Logs/Dalton/writer.stderr.log   # 记下数字，10 分钟后不应再增长
ps -axo stat=,ppid= | awk '$1 ~ /^Z/' | wc -l    # 僵尸子进程，期望回落到 0–1
```
然后回到待办审批页，对 ACN/EPAM/IBM/CTSH 四张深度认知门卡片点「按系统建议退回」，DXC 可直接裁决；决定应在几秒内保存。
