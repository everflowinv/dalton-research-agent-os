# 2026-09-16 批次发布 runbook（源码 7f24d085，release 7ae45005…）

自动模式的安全分类器把"生产部署"类动作拦在了执行前，以下步骤由 owner（或以放开权限的会话）按顺序执行。每一步都可复制；除标注外均幂等。

前置状态（已完成）：
- 源码 `7f24d085` 已提交到 main，全量 9,147 项测试全绿。
- wheel 已构建：`/tmp/dalton-src-7f24d085/dist/dalton_core-0.1.0.dev0-py3-none-any.whl`（sha256 `daf965e1…4301`）。
- release venv 已建好（Python 3.14.6，依赖集与锁定清单一致）：`~/.dalton/runtime/releases/7ae450054d82c8fa0d420b072198839eec85953b736c7130de3c34e1c0f2a130/venv`。
- 缺的只是 `release-manifest.json`（第 1 步）。

```zsh
cd ~/Projects/dalton-research-agent-os
S="$HOME/Library/Application Support/Dalton/state/dalton-core"
NEW=7ae450054d82c8fa0d420b072198839eec85953b736c7130de3c34e1c0f2a130
R=~/.dalton/runtime/releases/$NEW
```

## 1. 补写 release manifest（若 /tmp 里的 wheel 已丢失，改用 `scripts/build_release.py --apply --python .venv/bin/python` 重新构建并以其输出的新 hash 替换 NEW）
```zsh
.venv/bin/python scripts/write_release_manifest.py $R \
  --wheel /tmp/dalton-src-7f24d085/dist/dalton_core-0.1.0.dev0-py3-none-any.whl \
  --source-commit 7f24d085a31718c830a836b7132d778d248fee5a --apply
```

## 2. 线上配置：投影间隔 2 → 60（三个环境）
```zsh
for f in "$HOME/Library/Application Support/Dalton/config/service.json" ~/.dalton/workspaces/*/config/service.json; do
  python3 - "$f" <<'PY'
import json,sys,os
p=sys.argv[1]; d=json.load(open(p)); d["projection_min_interval_seconds"]=60
tmp=p+".tmp"; open(tmp,"w").write(json.dumps(d,ensure_ascii=False,indent=2,sort_keys=True)+"\n"); os.chmod(tmp,0o600); os.replace(tmp,p); print("ok",p)
PY
done
```

## 3. 重新渲染 writer/controller/control plist（让新车道开关进入启动参数）
```zsh
# legacy
.venv/bin/python -m dalton_core.macos_launchagent \
  --launch-agents-dir ~/Library/LaunchAgents --python-env-bin $R/venv/bin \
  --state-dir "$S" --config "$HOME/Library/Application Support/Dalton/config/service.json" \
  --log-dir ~/Library/Logs/Dalton
# 两个 workspace（先把 manifest 的 release_path 指向新 release，再渲染）
for ws in ~/.dalton/workspaces/*/workspace.json; do
  .venv/bin/python - "$ws" "$R/venv" "$NEW" <<'PY'
import json,sys,os,hashlib
p,venv,new=sys.argv[1:]; d=json.load(open(p))
d["release_path"]=venv; d["release_ref"]="release:sha256:"+new
d["shared_readonly_paths"]=sorted(set(d.get("shared_readonly_paths",[]))|{venv})
body={k:v for k,v in d.items() if k!="content_hash"}
d["content_hash"]=hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
tmp=p+".tmp"; open(tmp,"w").write(json.dumps(d,ensure_ascii=False,indent=2,sort_keys=True)+"\n"); os.chmod(tmp,0o600); os.replace(tmp,p); print("ok",p)
PY
  .venv/bin/python -c "from dalton_core.workspace_process import install_workspace; import sys,json; print(json.dumps(install_workspace(sys.argv[1], sys.argv[2])['plists'], indent=1))" "$ws" ~/Library/LaunchAgents
done
grep -c -- '--valuation-snapshot-lane' ~/Library/LaunchAgents/space.lumos.dalton.writer.plist        # 期望 1
grep -c -- '--quantitative-claim-promotion' ~/Library/LaunchAgents/space.lumos.dalton.writer.plist   # 期望 1
```

## 4. 发布 worker plist 从模板重装（补 stderr）
```zsh
sed -e "s#@@RELEASE_VENV@@#$R/venv#g" -e "s#@@STATE_DIR@@#$S#g" -e "s#@@LOG_DIR@@#$HOME/Library/Logs/Dalton#g" \
  deploy/macos/launchagents/com.dalton.research-publication-worker.plist.template \
  > ~/Library/LaunchAgents/com.dalton.research-publication-worker.plist
plutil -lint ~/Library/LaunchAgents/com.dalton.research-publication-worker.plist
```

## 5. 一次性切换全部服务与指针（会重启 10 个 LaunchAgent）
```zsh
.venv/bin/python -m dalton_core.launch_drain --state-dir "$S"      # 确认没有在跑的子进程
.venv/bin/python scripts/release_switch.py $R                       # 只看计划
.venv/bin/python scripts/release_switch.py $R --apply               # 退出码 0 = 健康校验通过；3 = 校验未过，用旧 release 再跑一次即回滚
```

## 6. 投影索引（约 40 秒，幂等；拿不到锁只会 deferred，重跑即可）
```zsh
$R/venv/bin/python -m dalton_core.migrations.projection_indexes --core-db "$S/core.sqlite" --scheduler-db "$S/scheduler.sqlite"
```

## 7. 修复模型链（通过在跑的 writer，与 cockpit 模型页同一路径）
```zsh
.venv/bin/python scripts/repair_brain_chains.py --state-dir "$S"                       # 预演
.venv/bin/python scripts/repair_brain_chains.py --state-dir "$S" --apply --actor human:lumos   # 预期：已发布 5 项
.venv/bin/python scripts/repair_brain_chains.py --state-dir "$S"                       # 预期：没有需要修复的链
```

## 8. 新环境模板重新导出 + manager.json 指针
```zsh
rm -rf /tmp/dalton-runtime-20260916
.venv/bin/python scripts/bind_shared_call_budget_policy.py \
  --source-state "$S" --source-service "$HOME/Library/Application Support/Dalton/config/service.json" \
  --policy-path ~/.dalton/connections/model-call-budget-policy.json --stage /tmp/dalton-runtime-20260916
install -m 600 /tmp/dalton-runtime-20260916/model-runtime.json   ~/.dalton/connections/model-runtime-20260916-v5.json
install -m 600 /tmp/dalton-runtime-20260916/service-runtime.json ~/.dalton/connections/service-runtime-20260916-v7.json
.venv/bin/python - <<'PY'
import hashlib, json, os
from pathlib import Path
manager = Path.home()/".dalton/manager.json"
model = Path.home()/".dalton/connections/model-runtime-20260916-v5.json"
service = Path.home()/".dalton/connections/service-runtime-20260916-v7.json"
c = json.loads(manager.read_text())
c["runtime_templates"] = {"model": {"path": str(model), "sha256": hashlib.sha256(model.read_bytes()).hexdigest()},
                          "service": {"path": str(service), "sha256": hashlib.sha256(service.read_bytes()).hexdigest()}}
c["shared_readonly_paths"] = sorted(set(c.get("shared_readonly_paths") or []) | {str(model), str(service)})
tmp = manager.with_name(".manager.json.tmp"); tmp.write_text(json.dumps(c, indent=2, sort_keys=True)+"\n"); os.chmod(tmp, 0o600); os.replace(tmp, manager)
print(json.dumps(c["runtime_templates"], indent=2))
PY
```

## 9. 签署定量自动入账规则（owner 检查点），然后立即跑一次
```zsh
.venv/bin/python scripts/sign_quantitative_auto_commit_policy.py --state-dir "$S"                         # 预演
.venv/bin/python scripts/sign_quantitative_auto_commit_policy.py --state-dir "$S" --apply --actor human:lumos
.venv/bin/python -m dalton_core.quantitative_claim_promotion_cli --state-dir "$S" \
  --summary-dir "$S/quantitative-claim-promotion-runs/manual-1" \
  --candidate-staging "$S/research-review/candidate-staging.sqlite" --limit 5000     # 约 25 分钟纯 CPU，零模型花费
```

## 10. 日志轮转 + 回收无引用 release
```zsh
sed -e "s#@@RELEASE_VENV@@#$R/venv#g" -e "s#@@LOG_DIR@@#$HOME/Library/Logs/Dalton#g" -e "s#@@REPO@@#$PWD#g" \
  deploy/macos/launchagents/com.dalton.log-rotate.plist.template > ~/Library/LaunchAgents/com.dalton.log-rotate.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.log-rotate.plist
.venv/bin/python scripts/gc_releases.py --apply
```

## 11. 验证（切换后 30 分钟）
```zsh
python3 -c "import json;d=json.load(open('$S/run/heartbeat.json'));print(d['state'],d['last_projection_at'])"
sqlite3 "file:$S/tick-ledger.sqlite?mode=ro" "select started_at, round((julianday(ended_at)-julianday(started_at))*86400,1) from tick_ledger_ticks order by started_at desc limit 6"   # 期望 < 60s
ps -axo stat=,ppid= | awk '$1 ~ /^Z/' | wc -l                                                    # 期望 0–1
.venv/bin/python -m dalton_core.needs_human_cli --state-dir "$S" | head -40
sqlite3 "file:$S/core.sqlite?mode=ro" "select company_ref,count(*) from valuation_snapshot_versions group by 1"   # 1 小时内五家各 1
```

## 12. 迁移到 EveSSD（最后做；会停全部服务、rsync 约 9.2 GB、原目录改名保留）
```zsh
sudo diskutil enableOwnership /Volumes/EveSSD        # 可选但推荐：保住 0600/0700 的内核级属主约束
.venv/bin/python scripts/migrate_state_dir.py --dest /Volumes/EveSSD/Dalton            # 预演
.venv/bin/python scripts/migrate_state_dir.py --dest /Volumes/EveSSD/Dalton --apply    # 执行；回滚：--rollback --apply
```

## 执行记录（2026-09-17 03:00–03:35 UTC）与偏差

- 第 1 步：`build_release.py` 的 `pip install <wheel>` 装不上 optional extras，改为用仓库自带 `workspace_release.install_release(wheelhouse=/private/tmp/dalton-workspace-dependencies-0914/wheelhouse, dependency_lock=…/dependencies.lock.json)` 正规安装（生成 venv 内 `dalton-release.json`），再用 `write_release_manifest.py` 补 release 级 manifest。`build_release.py` 已改为按锁定清单安装依赖。
- 第 3 步：`install_workspace` 在服务运行时会因端口占用拒绝；改为直接调用 `macos_launchagent.render`。workspace.json 改动后必须把 `content_hash` 同步到 service.json 的 `workspace.manifest_hash`（`release_switch.py` 已补）。
- 第 5 步：legacy controller 首次 bootstrap 遇 launchd 半注册态 I/O 错误，单独 bootout/bootstrap 一次即可。
- 新增：三个环境的 writer-tokens.json 需把新 lane 的 dispatch op 加入 core 主体（`writer_server.load_principals` + `replace_token_config`），否则新车道报 operation is not permitted。
- 第 12 步：迁移见 PROJECT_STATUS 2026-09-17 记录的三处修复。
