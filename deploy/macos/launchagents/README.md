# LaunchAgent 模板

这些是模板，不是可直接 `launchctl bootstrap` 的文件：里面的 `@@…@@` 占位符要先
替换。安装/切换时用：

```zsh
repo=~/Projects/dalton-research-agent-os
venv=~/.dalton/runtime/releases/<sha>/venv
state="$HOME/Library/Application Support/Dalton/state/dalton-core"
logs="$HOME/Library/Logs/Dalton"
dalton_home="$HOME/.dalton"

for name in com.dalton.research-publication-worker com.dalton.log-rotate; do
  sed -e "s#@@RELEASE_VENV@@#$venv#g" \
      -e "s#@@STATE_DIR@@#$state#g" \
      -e "s#@@LOG_DIR@@#$logs#g" \
      -e "s#@@REPO@@#$repo#g" \
      -e "s#@@DALTON_HOME@@#$dalton_home#g" \
      "$repo/deploy/macos/launchagents/$name.plist.template" \
      > "$HOME/Library/LaunchAgents/$name.plist"
  plutil -lint "$HOME/Library/LaunchAgents/$name.plist"
done
```

替换完再 `launchctl bootout` / `bootstrap`。之后的发布切换用
`scripts/release_switch.py`，它只改 `ProgramArguments[0]`，模板里的其他字段
（包括 `StandardOutPath` / `StandardErrorPath`）会原样保留。

## 发布 venv 里的 python 为什么是符号链接（macOS TCC）

服务要读写外接 SSD（`/Volumes/EveSSD`），这受 macOS TCC 的"可移除宗卷"权限
管。Homebrew 的 python 只有 adhoc 签名，TCC 对这类程序按**可执行文件的真实路径**
记授权。发布 venv 以前用 `python -m venv --copies` 建，每个发布都有一个新路径的
`venv/bin/python`，于是每次部署都要人去点一次授权（TCC 库里攒了十几条
`~/.dalton/runtime/releases/<hash>/venv/bin/python`）。而复制出来的 python 仍然
链接着 Homebrew 的 `Python.framework`，并没有因此独立于 Homebrew。

现在 `scripts/build_release.py` 与 `dalton_core.workspace_release` 都不再加
`--copies`：`venv/bin/python`、`python3` → `python3.14`（相对链接），
`python3.14` → `/opt/homebrew/opt/python@3.14/bin/python3.14`（绝对链接），
最终落到 `/opt/homebrew/Cellar/python@3.14/<版本>/Frameworks/Python.framework/Versions/3.14/bin/python3.14`。
launchd 仍然执行 `venv/bin/python`（plist 与 `release_switch.py` 都不用改），内核
解析到 Cellar 里那个已经授权过的文件，TCC 看到的始终是同一个客户端。

完整性不因此放松：venv 里的 `dalton-release.json` 升为 schema 0.3，记录基础
解释器的真实路径与 sha256；校验时只允许 `bin/` 下名为 `python`、`python3`、
`python3.X` 的符号链接，而且必须解析到这个解释器、哈希一致，其他任何符号链接
照旧拒绝。旧的 `--copies` 发布（schema 0.1/0.2）照常校验，可以照常回滚过去。

注意：

- Homebrew 升级 python@3.14 的小版本（如 3.14.6 → 3.14.7）后，真实路径里的版本
  号变了，TCC 会把它当成新程序，**需要重新授权一次**；同时所有 0.3 发布的校验
  会报"base interpreter … rebuild the release"。建议平时 `brew pin python@3.14`，
  要升级 Python 时主动 `brew unpin` → 升级 → 用 `scripts/build_release.py` 重建
  发布 → 切换 → 授权一次。
- 验证部署后不再弹窗：切换后
  `sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" "select client, auth_value from access where service='kTCCServiceSystemPolicyRemovableVolumes'"`
  不应出现新发布的 `venv/bin/python` 条目，只有已有的 Cellar 路径条目
  （auth_value=2）；服务日志里也不应再有访问 `/Volumes/EveSSD` 的
  `Operation not permitted`。

## 为什么发布 worker 的 plist 要重装一次

线上那份 `com.dalton.research-publication-worker.plist` 完全没有
`StandardOutPath` / `StandardErrorPath`：worker 每 5 分钟跑一次，连续 17 轮把
同一批 329 个批次跑失败，而所有输出都进了 /dev/null。这个字段是加不上去的
"切换"能做的事，必须用模板重装一次。
