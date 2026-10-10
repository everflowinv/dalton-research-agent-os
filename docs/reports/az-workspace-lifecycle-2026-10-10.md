# 研究项目创建与生命周期

技术设计与验收摘要，2026-10-10。隔离测试和历史回放用于核对合同与兼容性；不等同于当前公司研究已完成。原始研究材料、运行账单及逐文件收据私有留存。

创建入口使用 host manager 权限和每日配额。完整初始目标沿 first-mission authority 发布 v1；信息不足先保存目标草稿。预算使用有限默认值，目标文字中的更高费用数字不能扩大授权，创建本身不调用模型或网络。

状态为 active、paused、archived、deleted。暂停排空后停止角色，其他活动 cockpit 提供只读研究视图；主环境不能暂停或删除。归档先保存并验证状态及外部项目日志包，再移除活动数据和路由，原位置留 tombstone；恢复拒绝越路径/危险链接，核验字节和端口授权，并在服务就绪后追加 active。

删除仅接受 paused/archived，须审阅真实待决策项和未发布草稿、核对项目名称与不可恢复确认。历史 lifecycle events/registry 保留，停用项目不参加费用任务、周报、配置同步和 release/parity 写入。仍可恢复的归档保留其钉定 release 依赖。

真实临时 host authority 的测试覆盖完整生命周期、重复请求、损坏归档、相邻目录、SSD registry、端口重分配和审计保留；页面测试覆盖创建/关闭/恢复焦点。合成 launchctl/Tailscale 不冒称生产服务观察。生产 canary 和运行管理验收单独记录，不能把清理旧项目作为后台自动操作。

设计见 [ADR-0021](../adr/0021-workspace-creation-and-audited-lifecycle.md)。
