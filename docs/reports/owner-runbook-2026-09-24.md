# 2026-09-24 部署 runbook（分支 `fix/family-and-logrotate`）

本批三条提交：

| 问题 | 内容 |
|---|---|
| A 模型家族被重置为 unclassified | broker 把 `profile:claude-opus-5` 改指 `claude-opus-5-5`（`gpt-5-6-sol/luna` 改指 `gpt-6-*`）时，目录投影把家族一律重置为 `unclassified:<provider>`，而未分类家族永远不算独立，于是文档车道报 `verifier cannot remain independent of every producer family`、出版 worker 报 `verifier_not_independent`。现改为按新路由推导家族（规则见 `src/dalton_core/model_family_lineage.py`），无法确定才保持 unclassified |
| B 日志轮转每天失败 | 4fa8e771 把模板里的 `--apply` 误改成 `apply`；另外轮转只扫顶层，workspace 子目录日志从未轮转 |
| C 出版 worker stdout 过大 | 每 5 分钟打印整份 checkpoint（约 415 KB/行），日志已 300 MB；改为只打印计数摘要，完整 checkpoint 仍写 `worker-last-run.json` |

```zsh
cd ~/Projects/dalton-research-agent-os
```

## 0. 部署前基线（只读）

```zsh
.venv/bin/python scripts/check_model_family_independence.py; echo "exit=$?"
```

部署前三个环境（legacy、ws-7d89…、ws-e399…）结果一致：8 个 profile 为 `unclassified:*`，`family_drift` 6 条，`document_verifier_preflight` 为 failed。

## 1. 部署

合并后照常构建与切换（切换脚本会重启全部 LaunchAgent）：

```zsh
.venv/bin/python scripts/build_release.py --apply | tee /tmp/dalton-build-20260924.json
NEW=$(python3 -c "import json;print(json.load(open('/tmp/dalton-build-20260924.json'))['release_hash'])"); echo $NEW
.venv/bin/python scripts/release_switch.py ~/.dalton/runtime/releases/$NEW --source-commit <合并后的 commit> --apply
```

**日志轮转 plist 必须重装一次**：`release_switch.py` 只改 `ProgramArguments[0]`，线上 `~/Library/LaunchAgents/com.dalton.log-rotate.plist` 里的 `apply` 不会被切换修好。按 `deploy/macos/launchagents/README.md`（本批新增了 `@@DALTON_HOME@@` 占位符）只重装这一个：

```zsh
repo=~/Projects/dalton-research-agent-os
venv=~/.dalton/runtime/releases/$NEW/venv
logs="$HOME/Library/Logs/Dalton"
dalton_home="$HOME/.dalton"
sed -e "s#@@RELEASE_VENV@@#$venv#g" -e "s#@@LOG_DIR@@#$logs#g" \
    -e "s#@@REPO@@#$repo#g" -e "s#@@DALTON_HOME@@#$dalton_home#g" \
    "$repo/deploy/macos/launchagents/com.dalton.log-rotate.plist.template" \
    > "$HOME/Library/LaunchAgents/com.dalton.log-rotate.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.dalton.log-rotate.plist"
launchctl bootout gui/$(id -u)/com.dalton.log-rotate 2>/dev/null
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.dalton.log-rotate.plist"
```

注意该 plist 的脚本路径指向仓库工作树（`@@REPO@@/scripts/rotate_dalton_logs.py`），所以主仓库 main 必须已经包含本批提交。

## 2. 验证

### 2.1 模型家族与独立核验预检（A）

家族修正不需要手工改库：模型目录车道在 writer 重启后的第一个 controller tick 就会跑一次同步（此后每小时一次），对家族有变化的 profile 追加一个新版本，旧版本原样保留。

```zsh
# 只读：各环境的未分类 profile、家族漂移、文档车道独立核验预检
.venv/bin/python scripts/check_model_family_independence.py; echo "exit=$?"

# 只读：直接看路由库里的当前家族（任一环境，换路径即可）
R=~/.dalton/workspaces/ws-e399ececd5aa7a3762b1a0a4/state/dalton-core/model-router.sqlite
sqlite3 "file:$R?mode=ro" "WITH l AS (SELECT profile_id, max(rowid) r FROM model_endpoint_profile_versions GROUP BY profile_id)
SELECT v.profile_id, json_extract(v.profile_json,'$.version'), json_extract(v.profile_json,'$.model'), json_extract(v.profile_json,'$.family')
FROM model_endpoint_profile_versions v JOIN l ON v.rowid=l.r
WHERE v.profile_id IN ('profile:claude-opus-5','profile:gpt-5-6-sol','profile:gpt-5-6-luna','profile:grok-4-7',
 'profile:gemini-3-8-flash-antigravity-high','profile:auto-antigravity-cli-gateway-gemini-3-1-pro-e5bd948a788a')
   OR json_extract(v.profile_json,'$.family') LIKE 'unclassified:%'"
```

期望（用线上路由库拷贝在临时目录里实测过，三个环境一致）：

| profile | 部署前 | 部署后 |
|---|---|---|
| `claude-opus-5`（→ claude-opus-5-5） | unclassified:claude-cli-gateway | `anthropic-claude-5` |
| `gpt-5-6-sol` / `gpt-5-6-luna`（→ gpt-6-*） | unclassified:openai | `openai-gpt-6`（与 gpt-6-astra 同家族，**不是**原来的 openai-gpt-5.6） |
| `grok-4-7` | unclassified:xai | `xai-grok-4` |
| `gemini-3-8-flash-antigravity-high` | unclassified:antigravity-cli-gateway | `google-gemini-3` |
| `auto-…-gemini-3-1-pro-…` | unclassified:antigravity-cli-gateway | `google-gemini-3` |
| `auto-muse-cli-gateway-muse-spark-1-3…`（两条） | unclassified:muse-cli-gateway | **不变**（见下方待决事项） |

- `family_drift` 应为空（`{}`）；`unclassified_profile_ids` 只剩两条 muse。
- `document_verifier_preflight`：**在 muse 家族声明之前仍是 failed**。实测修复后三个环境的文档草稿链是 `claude-opus-5 → muse-spark-1.3 → zai-glm-5-3 → deepseek-v4-flash`，核验链是 gemini；只有 muse 这一条还拿不到家族。在临时拷贝里模拟声明 muse 家族后预检通过。

### 2.2 日志轮转（B）

```zsh
# 只读演练（不加 --apply 不动文件），应列出顶层和 workspaces/、~/.dalton/workspace-logs/ 子目录里的日志
.venv/bin/python scripts/rotate_dalton_logs.py --log-dir ~/Library/Logs/Dalton --log-dir ~/.dalton/workspace-logs | head -30
plutil -p ~/Library/LaunchAgents/com.dalton.log-rotate.plist | grep -A12 ProgramArguments   # 最后一项应为 "--apply"
```

次日 04:10 之后（或手动 `launchctl kickstart gui/$(id -u)/com.dalton.log-rotate`）：

```zsh
tail -3 ~/Library/Logs/Dalton/log-rotate.stderr.log          # 不应再新增 "unrecognized arguments: apply"
tail -c 400 ~/Library/Logs/Dalton/log-rotate.stdout.log       # 应有 rotated_count，publication-worker.stdout.log 应被轮转
ls -la ~/Library/Logs/Dalton/publication-worker.stdout.log*   # 原文件被就地截断，旁边多一个 .gz
```

### 2.3 出版 worker 输出（C）

```zsh
tail -1 ~/Library/Logs/Dalton/publication-worker.stdout.log | wc -c    # 期望 1 KB 左右（此前约 415 KB）
tail -1 ~/Library/Logs/Dalton/publication-worker.stdout.log            # 计数摘要，末尾 checkpoint 字段指向 worker-last-run.json
```

完整明细（products、blocked_reasons、ui_texts.batches）仍在 `research-publication-work/worker-last-run.json`，驾驶舱与运维脚本读的就是这个文件，行为不变。

---

## 需要你决定的事（本批没有动）

### muse-spark-1.3 的家族

`muse-cli-gateway/muse-spark-1.3` 与 `…-contributor` 不在人工整理的模型目录里，没有可靠依据推出它属于哪个家族，按「宁严勿松」保持 unclassified。它排在文档草稿链第二位，所以**文档车道的独立核验预检要等你处理这一条才会通过**。二选一：

1. 在模型页为这两个 profile 声明家族（metadata declaration；会立即触发一次目录同步）。声明的家族名必须与链上其他模型的家族都不同，才算独立；
2. 或者把 muse 从文档草稿链里拿掉。

### 已被未分类家族「污染」的在途产物

独立性核验读的是生产者**当时那条不可变路由决策**里记下的家族（`served_family`）。部署前由未分类 profile 产出的草稿，其决策里永远写着 `unclassified:*`，部署后核验它们仍会报 `verifier_not_independent`。只读统计 ws-7d89… 的路由库：antigravity 46 条（09-18 起）、claude-cli-gateway 7 条（09-23 起）、muse 2 条；另外两个环境为 0。出版 worker 里已连续失败 6 次的批次也不会再自动重试。需要你决定是否对这些批次重新起草/重新排队；本批没有改 `served_family` 的取证方式（那会让核验改信事后推导而不是当时记录）。

### 新模型家族规则需要随目录维护

推导规则只认人工目录已有的家族（Claude 5 代、GPT-5.5/5.6/6、Gemini 3.x、Grok 4.x、GLM 5.2/5.3、Qwen 3.8、DeepSeek V4）。下一代（如 claude-opus-6、gpt-6.1）会保持 unclassified，直到在 `model_deployment.py` 目录或 `model_family_lineage.py` 规则里加入，或在模型页声明。
