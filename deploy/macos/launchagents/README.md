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

## 为什么发布 worker 的 plist 要重装一次

线上那份 `com.dalton.research-publication-worker.plist` 完全没有
`StandardOutPath` / `StandardErrorPath`：worker 每 5 分钟跑一次，连续 17 轮把
同一批 329 个批次跑失败，而所有输出都进了 /dev/null。这个字段是加不上去的
"切换"能做的事，必须用模板重装一次。
