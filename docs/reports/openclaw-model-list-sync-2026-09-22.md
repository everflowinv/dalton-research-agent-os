# OpenClaw 完整模型目录自动同步（2026-09-22）

状态：**2026-09-23 已上线并完成三个环境的模型列表验收**。运行源码 `51420b67`，发布 `1528e626`；11 个服务及发布指针一致。无需用户再修改 macOS 权限。9 月 22 日的启动阻塞和回退记录保留在后文，已不代表当前状态。

## 2026-09-23 上线验收

08:45（纽约时间）复查发现原有三个服务均已恢复；随后用候选包经 launchd 执行一次性只读探针，成功读取 EveSSD 上的三个 state，退出并卸载探针，再排空任务并切换正式服务。

三个 `model-catalog-sync.json` 均启用自动跟随。常驻同步任务自主补齐 broker 和 Router：OpenClaw 当前 **21 个 provider model ref** 对应 **22 个 broker profile**（包含 Flash low/high 两档）。本次自动补上 `muse-spark-1.3`、`muse-spark-1.3-contributor`、Antigravity `gemini-3.1-pro`。三个环境均 `catalog_in_sync=true`，四类目录差集均为空。

通过实际模型页面使用的数据生产路径 `CockpitPlane.models()` 读取验收：legacy 有 24 个选项（含仍对应有效 provider 的历史档案），Hyperscaler 与新环境各 22 个；上述三个新增选项均出现。三个环境模型梯队快照哈希保持不变。原 broker profile 的每个字段和两处受管字段之外的 OpenClaw 配置都保持原值。同步验收没有调用模型。

运行检查：全部服务位于目标 release、manager 指针一致、writer socket 可用；三个环境心跳已推进且 `last_error=null`。OpenClaw 已检测到新 profile/allow-list，按其既有机制等待在途请求结束后重载；这里验收的是目录与选择列表，没有宣称新增模型的付费调用已通过。

最终发布包相关测试 **145 项通过**，broker **35 项通过**。完整回归执行 **9,733 项**（4 skipped），仅 SEC launcher 一项测试在临时目录清理时与 supervisor 写入竞争。修复仅涉及测试：目录退出前关闭并 join launcher；该用例重复 **50 次全部通过**，所属模块 **10 项通过**，生产源码及发布包未改。详细收据见 [9 月 23 日上线证据](evidence/openclaw-model-list-sync-2026-09-23/)。

## 原因

原有自动同步只读取 broker 的 `config.profiles`。所以 OpenClaw provider 目录的新模型，只要尚未加入 broker profile 和 `llm.allowedModels`，就不会出现在 Dalton 模型选择列表。16:00 UTC 的只读核对发现三个环境都与 broker 清单一致，但 Muse Spark 1.3 缺少 allowed/profile，Antigravity Gemini 3.1 Pro 已 allowed 却缺少 profile。这不是用户希望的“任何模型改动自动出现”。

## 实现

三个环境已启用 `model-catalog-sync.json` 的 `follow_provider_catalog: true`。目录车道在每个 controller tick 比较公开目录指纹，变化时执行以下操作，无需再按一次“允许使用”：

1. 从 `models.providers` 派生完整模型集合，只协调 broker 的 `llm.allowedModels` 和 `config.profiles`。
2. 已有 profile 保留 ID、thinking level、受控能力证明和自定义调用上限；同一模型的 low/high 等多档位全部保留。缺少 profile 的模型获得按准确 provider/model ref 生成的稳定 ID。
3. 删除模型撤下 broker 声明，Router 追加退役版本；重新添加恢复同一自动 ID。上下文、输出上限和价格等公开目录参数通过新版本更新，历史版本保持不可变。
4. 原子写入完成后重新读取落盘配置，再同步每个工作区的 Router 和模型选择页。只更新目录，不重排现有模型梯队，也不为目录检查调用模型。

新安装和新 workspace 默认启用；已有显式 `false` 与自定义路径继续保留。模型页显示自动同步说明，自动模式不再出现手动放行按钮。broker 支持空 profile 集合，所以删除全部模型会得到空的 broker 目录，不会卡死同步。

写入使用同机跨进程 flock、精确内容与 inode 比较、0600 备份以及不覆盖新写入的发布方式。只改 broker 的两个字段；凭据、provider 端点和其他插件设置保持原值。遇到外部并发修改时拒绝并等待后续 tick，公开回执不包含完整配置或凭据。

## 验证

源码相关测试 **144 项通过**；初次 Python 3.14 发布包 **144 项通过**，补修后的最终发布包 **145 项通过**；实际 OpenClaw Node 的 broker 测试 **35 项通过**。首轮全量回归因部署预演解析器把 Python heredoc 的 `for` 误当 shell 循环而陷入正则回溯，在 1,138 秒中断，未计为通过。已收窄 shell 循环头到同一逻辑行，后续预演及 picker 回归 112 项通过；最终全量及该次清理竞态修复结果见上方 9 月 23 日验收。

候选包从干净 Git archive 构建，依赖锁验证一致。曾将 11 个服务切到候选包并启用三个目录开关，但模型同步车道尚未执行，broker 仍有 21 个 profile，因此 Muse Spark 1.3 的线上自动出现尚未通过验收。

9 月 22 日历史启动诊断：旧、新 Python 进程均阻塞于外置盘目录或文件访问（`getcwd` / `open`）；`tccd` 对相关可执行路径记录 `kTCCServiceSystemPolicyAllFiles` 预检 `authValue=0`。这指向系统磁盘权限检查问题，但仅此预检不能证明具体授权弹窗已出现。电脑控制工具明确禁止操作 `com.apple.UserNotificationCenter`，已请用户检查 Python 的磁盘访问授权，未尝试修改权限数据库或绕过系统限制。一个空工作区的 `PYTHONSAFEPATH` 诊断设置无效，已撤回。

当时拟定的恢复步骤（现已完成，不再需要用户操作）：用户完成系统权限检查后，确认上一版三个 writer/socket/heartbeat 恢复；重新部署已验证的候选 broker 路径；排空 Dalton 任务，启用三个 follow 开关并切候选 release；等待常驻目录车道运行；最后逐环境核对实际模型选择数据、所有公开 provider 的覆盖、原梯队未变及三份心跳。不能用手动填写 Router 代替自动同步验收。

新增、参数更新、删除、重新添加的测试直接读取 `CockpitPlane.models()`（与 `/v1/cockpit/models` 相同的数据生产路径），并核对原模型梯队、选择配置和历史版本保持不变。三进程并发、发布期间外部写入、空目录、ID 冲突、超出 broker token 上限、符号链接、幂等、脱敏回执均有覆盖。

补充：自动跟随模式也从选择列表隐藏不在 provider 目录中的旧 `model-profile:` 档案，保留数据库历史记录；这覆盖 legacy 曾遗留的旧 DeepSeek 条目。

独立只读复核确认三个 state 都经符号链接落到 EveSSD；回滚 writer 再次采样 803/803 在启动 `getcwd/open` 上，TCC 对同一进程有对应检查。挂载为可写 APFS、Unix 所有者及 mode 正常、Python 无 App Sandbox entitlement。近因归于系统隐私文件访问门控；所需具体授权项仍须用户在本机核对，不能仅凭 AllFiles 预检选择扩大权限。
