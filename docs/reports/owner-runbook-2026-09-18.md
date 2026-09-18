# 2026-09-18 部署 runbook（源码 3f8b23c9）

本批内容见 `docs/PROJECT_STATUS.md` 顶部各条（新环境自动采用方案、文档研究自动重试扩展、来源信封冲突、结论索引宽松解析、交付物修复与冷却、车道变化键与僵尸回收）。

```zsh
cd ~/Projects/dalton-research-agent-os
```

## 1. 部署（切换脚本会重启全部 10 个 LaunchAgent）
```zsh
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260918b.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260918b.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit 3f8b23c943811f8a8b08a6761983ea9741960b7a --apply
```

## 2. 现有 Hyperscaler 环境补齐凭证槽（新建环境已自动处理；这一步要停它的服务）
```zsh
WS=ws-7d894366d1132e2930475a60
for s in control controller writer; do launchctl bootout gui/$(id -u)/space.lumos.dalton.workspace.$WS.$s; done
.venv/bin/python scripts/repair_workspace_lane_parity.py --workspace $WS --source-state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core --actor-ref human:lumos --apply
for s in writer controller control; do launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/space.lumos.dalton.workspace.$WS.$s.plist; done
```

## 3. 验证（切换后两轮 tick）
```zsh
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
sqlite3 "file:$S/tick-ledger.sqlite?mode=ro" "select started_at, round((julianday(ended_at)-julianday(started_at))*86400) from tick_ledger_ticks order by started_at desc limit 3"   # 期望 < 40s
sqlite3 "file:$S/tick-ledger.sqlite?mode=ro" "select lane_operation, status_word from tick_ledger_lanes where tick_id=(select tick_id from tick_ledger_ticks order by started_at desc limit 1) and lane_operation in ('dispatch_company_dossier','dispatch_debate_map','dispatch_sales_notes_feed','dispatch_claim_index','dispatch_mission_document_research')"
ps -axo stat=,ppid= | awk '$1 ~ /^Z/' | wc -l    # 僵尸，期望 0
.venv/bin/python -m dalton_core.workspace_parity_cli --workspace ws-7d894366d1132e2930475a60 --source-state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core
```
