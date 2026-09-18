# 2026-09-18 部署 runbook（源码 ebf7273d）

本批内容见 `docs/PROJECT_STATUS.md` 顶部各条（定性陈述被拒收只记一条不再整轮失败、10-K 这类表格名不算数字、预算策略读取重试、文档研究 admission 在 mission 版本滚动后不再整批死掉、升级项的 owner 门与 CLI、新环境自动采用方案、文档研究自动重试扩展、来源信封冲突、结论索引宽松解析、交付物修复与冷却、车道变化键与僵尸回收）。

```zsh
cd ~/Projects/dalton-research-agent-os
```

## 1. 部署（切换脚本会重启全部 10 个 LaunchAgent）
```zsh
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260918d.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260918d.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit ebf7273df422f78b1e0d0c0f60dd2d0093a9acfb --apply
```

## 2. 现有 Hyperscaler 环境补齐凭证槽（已于 09-18 10:00 UTC 做过一次；再部署后不必重复）
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

## 4. 本批专项验证：升级给人的文档研究 admission 应自行恢复（切换后等两到三轮 tick）
部署前 legacy 有 21 条、Hyperscaler 有 4 条停在「reentry_failed_after_automatic_rebind」，根因都是 mission 版本滚动。部署后车道会各自再重入一次；预期两个环境的「需要你处理」里这类条目大幅减少。另有 7 条（legacy 4、Hyperscaler 3）是定性陈述含数字被拒收：部署后会各再派发一次，其中 4 条（只含「10-K」这种表格名）会正常入库，3 条（真含百分比/金额）会记一条拒收并结清，不再升级给人。
```zsh
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
.venv/bin/python -m dalton_core.document_recovery_cli holds --state-dir $L | head -40
.venv/bin/python -m dalton_core.document_recovery_cli holds --state-dir $W | head -40
```
仍然停着的（真正失败的），才用 `authorize-paid` / `authorize-unproved` / `authorize-all-escalated --max-total-cost-usd N`（不加 `--apply` 是只读预览）。
