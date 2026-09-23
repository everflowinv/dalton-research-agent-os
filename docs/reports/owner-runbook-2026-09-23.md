# 2026-09-23 部署 runbook（源码 9b4f4d54）

本批三条提交，都是 09-22 修复批上线后暴露出来的问题，外加 owner 在模型页看到的两件事：

| 提交 | 内容 |
|---|---|
| `f8b77e0e` | 模型页 10 秒加载：跳过记录无上限且被重复读 13 遍；受控核验「部分不可用」区分「链缺能力」与「本环境未绑定」 |
| `4abe4567` | 文档研究车道在 legacy 整条停摆（`recovery hint drifted`）改为只挂起单条 admission；落盘上限不再只认 launchd 注入的环境变量 |
| `9b4f4d54` | state 目录是符号链接时找不到 `service.json`，导致 legacy 四个阶段被误报未配置 |

全量测试 9,758 项通过（4 skipped），工作树干净。

```zsh
cd ~/Projects/dalton-research-agent-os
```

## 1. 部署（切换脚本会重启全部 11 个 LaunchAgent）

```zsh
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260923a.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260923a.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit 9b4f4d54e3ecf99966fe77b7ad614fe208ca32eb --apply
```

## 2. 验证（切换后等两到三轮 tick）

### 2.1 文档研究车道恢复（本批最要紧的一条）

部署前 legacy 该车道近 2 小时 23 轮全部 `unavailable`，writer 日志累计 304 次 `recovery hint drifted`。部署后应当不再 `unavailable`。

```zsh
L=/Volumes/EveSSD/Dalton/legacy-state/dalton-core
sqlite3 "file:$L/tick-ledger.sqlite?mode=ro" "select started_at, status_word, substr(counts_json,1,120) from tick_ledger_lanes where lane_operation='dispatch_mission_document_research' order by started_at desc limit 5"
# 期望：status_word 不再是 unavailable
grep -c "recovery hint drifted" ~/Library/Logs/Dalton/writer.stderr.log   # 记下数字，两轮 tick 后再查一次，期望不再增长
```

只读回放已验证 4 条有恢复链的 admission 全部能解析，`_execution_state` 均返回 `resume`。若仍有个别 admission 验证不过，它会单独挂起、理由 `recovery_hint_unverifiable`，并在待办里指明该查哪条恢复链——那是预期行为，不是故障。

### 2.2 模型页加载时间

```zsh
for p in 8793 8795; do
  curl -s -o /dev/null -w "port $p  %{time_total}s  %{size_download} 字节\n" \
    -H "Tailscale-User-Login: richard.lu.everflowinv@gmail.com" \
    "http://127.0.0.1:$p/v1/cockpit/models"
done
```

部署前 legacy 为 11.30s / 537,016 字节，Hyperscaler 1.10s / 201,606 字节。用同一批线上数据库在仓库代码里实测，legacy 降到 0.14 秒（热）、167 KB。部署后 legacy 应当进入 Hyperscaler 的量级。

### 2.3 受控核验与阶段绑定

模型页的核验梯队应显示「受控核验可用」，不再是「部分不可用」；未绑定的阶段单独列为提示，并指明该在哪个文件或配置字段里绑定。

legacy 的四个阶段应从「未配置」变为已配置：

| 阶段 | 部署前 | 部署后应为 |
|---|---|---|
| `plan` | `…openclaw-planner-decisions:62`（兜底，且与常驻规划器实际固定的不是同一条） | `…openclaw-planner:9` |
| `agenda_planning` | 未配置 | `…dalton-openclaw:11` |
| `thesis_impact_assessment` | 未配置 | `…openclaw-assessment:9` |
| `thesis_impact_verifier` | 未配置 | `…openclaw-verifier:4` |

两个 workspace 环境的所有阶段应当逐字节不变。

### 2.4 落盘上限

```zsh
ls -l /Volumes/EveSSD/Dalton/legacy-state/dalton-core/connector-spool/capacity.json   # 部署重启后应当出现
```

配置与 plist 本来就是 4 GB，**不需要你改任何配置**。此前待办里那条「已用到上限的 111%」是读取端的缺陷：约 20 个构造点硬编码 1 GB 兜底，只靠 launchd 注入的环境变量覆盖，所以任何不是 launchd 启动的进程（含手敲的 CLI 和巡检脚本）都会误报。真正跑活的 writer 环境里上限是 4 GB、用量 1.12 GB，**没有任何写入被拒绝**。部署后该条目应当消失。

如果你确实想把落盘压下去（可选，不是必需）：

```zsh
.venv/bin/python -m dalton_core.raw_spool_maintenance archive \
  --spool-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core/connector-spool \
  --min-age-seconds 604800
```

### 2.5 常规检查

```zsh
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
sqlite3 "file:$S/tick-ledger.sqlite?mode=ro" "select started_at, round((julianday(created_at)-julianday(started_at))*86400) from tick_ledger_ticks order by started_at desc limit 3"
ps -axo stat= | awk '$1 ~ /^Z/' | wc -l    # 僵尸，期望个位数且随即回收
```

部署前 legacy tick 平均 67.9 秒、Hyperscaler 32.2 秒，均无漏跳（近 6 小时 70/72 次）。时长上升是 09-22 修复后车道重新有事可做，不是回归：人群来源车道单次成本 8.0 → 8.6 秒但次数从 37 涨到 130，持股车道 16.8 → 14.5 秒、24 涨到 54。

---

## 需要你决定的事（本批没有动）

### 质量核验阶段一直没有开

`quality_verifier` 在两个环境都没有配置文件，模型页因此显示未绑定。查清的结论是**这是设计如此**，不是漏装：唯一的创建入口在 `deploy/macos/install.sh`，由一个从未设置过的环境变量控制，文档也写明该配置为可选、装了也不会新增车道或触发模型调用。它唯一的入口是手动命令行，没有任何调度器或车道引用它。

后果为零：两个环境的质量评分表都是 **0 行**，从来没跑过。**你此前遇到的研究门草稿卡在「质量/评分标准」，与这个阶段无关。**

要开的话需要你定两件事：一是用一条新的专属路由策略（安装脚本原本的做法，但线上路由库里并不存在这条策略），还是复用 `dalton-openclaw-dossier-verifier:58`——后者正是另外三个同类核验阶段的实际做法；二是这个用途的单次费用上限。梯队本身在代码里已经定死，与其余 14 个已绑定核验阶段同一条链。

另外提醒：真要开的话，`workspace_model_setup.py` 里把模型配置数量硬编码为 21 并按集合相等校验，legacy 一旦多出这个文件，导出运行时模板会直接失败，需要同步改成 22 并重新导出。

### 文档研究待办

legacy 15 条、Hyperscaler 6 条 admission 仍在等授权。legacy 那 15 条此前因车道停摆根本排不出去，部署后车道恢复才能看清哪些会自行消化、哪些是真需要你按。等两三轮 tick 后再用现有入口看：

```zsh
.venv/bin/python -m dalton_core.document_recovery_cli holds --state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core | head -40
```
