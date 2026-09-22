# OpenClaw 完整模型目录自动同步（2026-09-22）

状态：代码完成，线上验收因系统磁盘访问阻塞而未完成。候选源码 `31d41894`，发布 `0c10396a`。已回退运行指针、目录开关和 broker 插件路径至原状态，但旧版进程也阻塞，三个环境的心跳仍停在 16:24 UTC；不能宣称服务恢复或自动同步已经上线。

## 原因

原有自动同步只读取 broker 的 `config.profiles`。所以 OpenClaw provider 目录的新模型，只要尚未加入 broker profile 和 `llm.allowedModels`，就不会出现在 Dalton 模型选择列表。16:00 UTC 的只读核对发现三个环境都与 broker 清单一致，但 Muse Spark 1.3 缺少 allowed/profile，Antigravity Gemini 3.1 Pro 已 allowed 却缺少 profile。这不是用户希望的“任何模型改动自动出现”。

## 实现

三个环境计划启用 `model-catalog-sync.json` 的 `follow_provider_catalog: true`。目录车道在每个 controller tick 比较公开目录指纹，变化时执行以下操作，无需再按一次“允许使用”：

1. 从 `models.providers` 派生完整模型集合，只协调 broker 的 `llm.allowedModels` 和 `config.profiles`。
2. 已有 profile 保留 ID、thinking level、受控能力证明和自定义调用上限；同一模型的 low/high 等多档位全部保留。缺少 profile 的模型获得按准确 provider/model ref 生成的稳定 ID。
3. 删除模型撤下 broker 声明，Router 追加退役版本；重新添加恢复同一自动 ID。上下文、输出上限和价格等公开目录参数通过新版本更新，历史版本保持不可变。
4. 原子写入完成后重新读取落盘配置，再同步每个工作区的 Router 和模型选择页。只更新目录，不重排现有模型梯队，也不为目录检查调用模型。

新安装和新 workspace 默认启用；已有显式 `false` 与自定义路径继续保留。模型页显示自动同步说明，自动模式不再出现手动放行按钮。broker 支持空 profile 集合，所以删除全部模型会得到空的 broker 目录，不会卡死同步。

写入使用同机跨进程 flock、精确内容与 inode 比较、0600 备份以及不覆盖新写入的发布方式。只改 broker 的两个字段；凭据、provider 端点和其他插件设置保持原值。遇到外部并发修改时拒绝并等待后续 tick，公开回执不包含完整配置或凭据。

## 验证

源码相关测试 **144 项通过**；实际 Python 3.14 发布包同样 **144 项通过**；实际 OpenClaw Node 的 broker 测试 **35 项通过**。首轮全量回归因部署预演解析器把 Python heredoc 的 `for` 误当 shell 循环而陷入正则回溯，在 1,138 秒中断，未计为通过。已收窄 shell 循环头到同一逻辑行，后续预演及 picker 回归 112 项通过；最终全量重跑中。

候选包从干净 Git archive 构建，依赖锁验证一致。曾将 11 个服务切到候选包并启用三个目录开关，但模型同步车道尚未执行，broker 仍有 21 个 profile，因此 Muse Spark 1.3 的线上自动出现尚未通过验收。

启动诊断：旧、新 Python 进程均阻塞于外置盘目录或文件访问（`getcwd` / `open`）；`tccd` 对相关可执行路径记录 `kTCCServiceSystemPolicyAllFiles` 预检 `authValue=0`。这指向系统磁盘权限检查问题，但仅此预检不能证明具体授权弹窗已出现。电脑控制工具明确禁止操作 `com.apple.UserNotificationCenter`，已请用户检查 Python 的磁盘访问授权，未尝试修改权限数据库或绕过系统限制。一个空工作区的 `PYTHONSAFEPATH` 诊断设置无效，已撤回。

恢复步骤：用户完成系统权限检查后，确认上一版三个 writer/socket/heartbeat 恢复；重新部署已验证的候选 broker 路径；排空 Dalton 任务，启用三个 follow 开关并切候选 release；等待常驻目录车道运行；最后逐环境核对实际模型选择数据、所有公开 provider 的覆盖、原梯队未变及三份心跳。不能用手动填写 Router 代替自动同步验收。

新增、参数更新、删除、重新添加的测试直接读取 `CockpitPlane.models()`（与 `/v1/cockpit/models` 相同的数据生产路径），并核对原模型梯队、选择配置和历史版本保持不变。三进程并发、发布期间外部写入、空目录、ID 冲突、超出 broker token 上限、符号链接、幂等、脱敏回执均有覆盖。

补充：自动跟随模式也从选择列表隐藏不在 provider 目录中的旧 `model-profile:` 档案，保留数据库历史记录；这覆盖 legacy 曾遗留的旧 DeepSeek 条目。
