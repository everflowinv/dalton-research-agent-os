# 2026-09-17 晚间 owner runbook（源码 40f060e2）

自动模式的分类器把「构建/切换发布」和「改 Claude Code 权限设置」都拦下（owner 口头授权不能覆盖），以下由 owner 在终端按顺序执行。已由 Claude 执行完毕的：共享每日预算已写入并绑定三个环境；Hyperscaler 研究目标 v2 预算已提到 legacy 水平；company-ir 已移出来源计划。

```zsh
cd ~/Projects/dalton-research-agent-os
```

## 1. 部署
```zsh
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260917e.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260917e.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit 40f060e2172597ba0064bb42dde57dcc128122ed --apply
```

## 2. 把两个 workspace 补齐（车道、公司名、缺失的策略血统）——服务需停下
```zsh
for WS in ws-7d894366d1132e2930475a60 ws-e399ececd5aa7a3762b1a0a4; do
  for s in control controller thesis-impact writer; do launchctl bootout gui/$(id -u)/space.lumos.dalton.workspace.$WS.$s 2>/dev/null; done
done
.venv/bin/python scripts/repair_workspace_lane_parity.py --workspace ws-7d894366d1132e2930475a60 \
  --source-state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core \
  --company-name AMZN=Amazon.com --company-name GOOGL=Alphabet --company-name META="Meta Platforms" --company-name MSFT=Microsoft --apply
.venv/bin/python scripts/repair_workspace_lane_parity.py --workspace ws-e399ececd5aa7a3762b1a0a4 \
  --source-state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core --apply
for WS in ws-7d894366d1132e2930475a60 ws-e399ececd5aa7a3762b1a0a4; do
  for s in writer controller control; do launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/space.lumos.dalton.workspace.$WS.$s.plist; done
done
```

## 3. 模型配置对齐（幂等，跑两遍）
```zsh
.venv/bin/python scripts/align_model_routing.py --apply --actor human:lumos
.venv/bin/python scripts/align_model_routing.py --apply --actor human:lumos
```
然后在任一环境的模型页把「独立复核」整类保存一次（legacy 内部有两条不同的 verifier 链，脚本不替你选；保存会自动同步到所有环境）。

## 4. 验证
```zsh
.venv/bin/python -m dalton_core.workspace_parity_cli --workspace ws-7d894366d1132e2930475a60 --source-state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core
.venv/bin/python scripts/align_model_routing.py          # 期望：0 个环境与源环境不同（verifier 除外）
```
