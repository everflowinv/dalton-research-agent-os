# ADR 0021 — 工作区创建、停用与审计生命周期

状态：已实现并部署；实际产物完成情况与长期观察单独验收。
日期：2026-10-10。

研究项目的生命周期由 host manager 管理，研究 writer 不处理跨项目文件、服务或删除。旧 manifest 未声明 lifecycle 时视为 active；其原 hash 不改写。创建记录保留 creator、UTC created_at、请求身份和初始化阶段。manager 的 allowed_tailscale_logins 开放分析员创建；admin_logins 缺省沿用 owner 行为。每人每天默认 3 次新建，重放同一请求不重复占用配额。配置可显式改变配额。

显式、范围完整的初始目标通过现有 first-mission authority 发布 v1，沿用有限预算默认值；目标中的任意更高费用数字不会扩张授权。创建不调用模型或网络，证券标识解析留给启动后的现有发现流程。信息不足的目标保存在现有 cockpit draft journal，界面可以补充和重新整理。研究设置和顶部项目切换器共用创建入口，完成后进入新项目首页。

状态流为 active → paused → archived → deleted；paused 和 archived 均可恢复为 active。主环境不能停用或删除。暂停先 drain，再 bootout 所有 launchd 角色，保留端口和状态。运行中的另一 cockpit 提供已暂停项目的只读研究视图，不保留后台付费研究进程。冷 WAL 状态只在无 sidecar、整个读取期间未变动时通过 immutable 快照读取，避免只读页面新建 WAL/SHM。

归档使用受保护目录 `<host_root>/archive/<slug>-<date>-<request-digest>/workspace.tar.gz`，校验 SHA-256 后才移除原目录中的状态、配置、日志与 launchd 定义，并移除本项目的 Tailscale Serve 地址。原位置保留 manifest tombstone 和显示名。归档包含原命名空间和数据库 sidecar；恢复仅提取到该原位置，并拒绝越界路径、特殊设备、链接下写入和越界硬链接。恢复前校验包 hash；旧端口已被占用时从配置授权端口池选择空闲端口，同时重绑 manifest、service/control 和 registry。无空闲端口就拒绝，不能抢占其他项目。安装并启动后，Cockpit、writer 和 controller 就绪检查通过才标记 active。

管理员删除只接受 paused/archived。须输入具体项目名、确认不可恢复，并确认审阅服务器重新读取的待决策项与未发布草稿。读取失败阻止删除。列表包含现有深度认知门、投资备忘录待裁决和 cockpit 草稿；归档事件冻结审阅清单。删除清除研究状态及本项目全部归档，保留 manifest、creation registry 与 append-only 审计。外部 `<host_root>/workspace-logs/<slug>` 同样归档、恢复或删除，日志包独立哈希验证；精确目录及其父目录/内部链接拒绝，不能触及共享或相邻项目。整体 workspaces registry 指向 SSD 的既有链接不影响受管子项目的正常清理。

`workspace_lifecycle_events` 是 host 级 SQLite 表；创建、操作 intent、归档 proof、失败和完成事件均记录 actor/time/reason，UPDATE/DELETE 触发器拒绝修改历史。原请求身份重放不重复完成动作。归档 proof 在破坏原状态之前落盘，崩溃后的同请求可沿用 proof；失败可用同请求重试。HTTP 在权限与确认预检后启动 detached 固定模块进程，立即返回 accepted，以免 self-pause 的 control bootout 中止操作；manager 清除调用方 DALTON_WORKSPACE_MANIFEST，子进程再次核验全部边界。终态在项目列表和审计中可查，操作日志为 host 私有文件。

停用项目不参加模型选择同步、周报、发布 gate、release manifest/launchd 切换和 parity 安装；过期 parity plan 也不能写入已停用目录。health/parity 显示刻意停止与保留状态/归档完整性，不将它们算成研究服务故障。七天无目标的项目改为停用/删除/保留提示；保留记录刷新提醒年龄。CLI `python -m dalton_core.workspace_fleet pause|archive|restore|delete|keep` 与 HTTP 使用同一权限、确认、服务和审计实现。

已归档项目保留钉定 release 的依赖；协调者不能在仍需恢复的归档存在时随意清理旧 release。已删除 tombstone 不要求旧 release 仍存在。实际服务/路由恢复和管理员清理另行验收。隔离 canary 的生命周期通过不授权自动删除其它既有项目。
