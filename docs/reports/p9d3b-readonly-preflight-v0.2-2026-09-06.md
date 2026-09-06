# P9d-3b：只读访问修复与启用预检

日期：2026-09-06
状态：隔离副本开发和本地验收完成；没有启用真实模型，没有部署。
基线：`f0ab1cb376fda90eb14eaf716a9699df6dd42219`；分支：`p9d3b-readonly-preflight`。
接续：[预算与人工 staging 报告](p9d3b-budgeted-broker-and-human-staging-v0.1-2026-09-06.md)。约束仍遵循 [ADR-0003 B](../adr/0003-transcript-candidate-admission.md) 和 [ADR-0004](../adr/0004-mission-driven-autonomy-and-automation-write-scope.md)。

## 本轮结果

修复了三个读取入口会调用可写构造器的实际缺陷；新增 `document_extraction_preflight`，让已认证的人在调用模型前检查当前原文、绑定、路由和预算。预检只在内存副本里调用原有 `ModelRouter.route` 与 `ThesisImpactBudgetStore.admit`，不写持久 route、reservation、调用记录，也不改变 Core、Scheduler 或 CandidateStaging。

全仓 **1110/1110**，专项与邻接 **174/174**，broker **25/25** 通过；新增回归 **30 条**。wheel/sdist、干净 Python 3.13 安装和 installed canary 通过。以上测试有重叠，不能把数量相加。

**本地预检通过不是 broker 身份验证，也不是来源送模许可或实际调用授权。** 本轮没有访问 live 数据或配置，没有读取真实凭据内容，没有连接真实 broker，没有真实模型调用或账单验证。

## 只读访问修复

`ModelRouter` 和 `ThesisImpactBudgetStore` 新增 `read_only=True`：

- 只打开既有文件，使用 `Path.as_uri()` 编码路径，再追加 `mode=ro`；空格、中文、`#/?/%/&` 不会被当成 URI 参数。
- 设置 `PRAGMA query_only=ON`，不 chmod、touch、mkdir，不执行 schema，不建表或迁移，不设置 WAL。即使调用者清掉 query_only，底层 mode=ro 仍拒绝写库。
- 缺文件、缺父目录、旧 schema 均不自动修复。`:memory:` 或 caller-owned connection 不能冒充只读文件。
- `context`、`_suggestions`、`budget_status` 的持久 router/budget 读取改用该路径。`_generate_broker` 的执行连接仍可写，预算预留、真实记账和结算流程不改成只读。
- `memory_snapshot()` 从只读连接 backup 到独立内存库。backup 覆盖整个库和 schema，之后不执行迁移；旧库缺表会继续拒绝。内存副本的临时 SQL 数据也设为 MEMORY。

### SQLite WAL 的准确边界

不能把 `immutable=1` 用在可能有未 checkpoint 提交的共享账本上，否则可能漏读 WAL。这里没有使用它。

SQLite 即使 `mode=ro`，也可能在缺 sidecar 且目录可写时创建 WAL/SHM。因此本实现对 WAL 文件先要求既有 `-wal` 与 `-shm`；缺少时 **fail closed**，不会为了显示页面自动创建。干净关闭后只剩主文件的 WAL 库也会被拒绝；必须由 owner 在独立维护流程处理，预检不代做。

正常只读 WAL 查询会使用 SHM 的易变读标记和锁槽位。验证保证的是 **数据库/WAL 正文、文件集合、权限、大小、mtime 不变，以及 Core/Scheduler/staging 的完整 SQL dump 和 total_changes 不变**；不把 SHM 的锁协调字节宣称为逐字节不可变。初轮测试正是在这里发现了区别，失败日志保留。若使用方要求连 SHM 锁标记都绝不变化，当前实现不满足这一更严格的物理只读定义。

## 预检检查什么

新 writer op：`document_extraction_preflight`。

| 参数 | 含义 |
|---|---|
| `review_id`、`expected_review_hash` | exact 待审项；已处理或 hash 变化即拒绝 |
| `offset` | 与原文页相同的有界窗口 |
| `expected_context_hash` | 可选；提供时必须与当前完整 context 相同。首次排查配置缺口可以不提供，不因此形成审批 |
| `actor_ref` | 由认证 principal 注入；客户端不能冒报或替换 |

该 op 加入既有 human-governance 操作表、closed 参数表及 actor 绑定表；不向 automation、Core 特例或 Cockpit 的 dashboard principal 开放。未增加 UI 启用按钮，没有自动发布配置或扩大现有 live 权限。

检查顺序和结果：

1. 重读 exact review、已 acquired 的 discovered document、全部页的 Core receipt/raw artifact、manifest、组装原文及 SHA-256；重验 active mission、公司、来源计划。
2. 重验 constitution、mandate 和当前 governance 的 exact ref/hash、pointer、有效期；mandate/governance 都必须有显式闭合 `research_budget`，mission 不能高于外层任何上限。
3. 校验 closed 模型配置、绝对路径及 broker client/dedicated-agent 的语法；只处理逻辑 credential slots，**不 stat/open 凭据路径，也不连接 socket**。语法有效不代表远端身份有效。
4. 只读打开既有 router/budget，分别 backup 到内存；检查 exact 单模型 policy，拒绝 superseded routing policy。路由 policy 的这道换版检查也进入实际 context 重验，避免旧页面继续采用过期 policy。
5. 从同一 context 构造 WorkOrder，按 canonical worker 的 initial 参数调用内存 `router.route`：research、text、UTF-8 字节输入估算、3000 输出 token、exact credential slots、相同 namespace/idempotency 形态。selected profile 还复用原有 profile/route/budget validator 检查，**没有复制路由选择算法**。
6. 在同一内存预算账本调用 `budget.admit`：owner cap、mission 调用数/费用、外层共享调用数/费用、跨 mission 支出、未绑定旧记录、跨日未结算预留、superseded budget policy、overrun alert 均由 canonical admission 判定。路由与预算可以同时报告各自的 blocker。
7. 已有同一 WorkOrder、route、admission 或 rejection 时，返回需查看状态/恢复的 blocker，不把原预留的 duplicate 当成新的可用余额。末尾再重验 source/config/outer context，发现漂移就拒绝。

返回值固定保留 `preview_only=true`、`execution_authorized=false`、`reservation_created=false`。只有本地检查全部通过才是 `local_checks_passed`；它不是 enablement 状态。返回的 policy/profile ref/hash 来自既有 authority，context 明确属于预览；**不返回模拟 route、admission 或 WorkOrder ref 来充当持久 authority**。

预检不是预留；多库快照不是一个跨库原子事务，期间和返回后都可能过期。真实执行仍须重验当前绑定并原子预留。broker 身份、credential 有效性、该来源是否允许送给该模型、真实账单和模型抽取质量始终列为未验证。

## 验收证据

以下目录均相对本 clone 的 `temp/p9d3b-preflight/`，不是原仓的 temp。

| 检查 | 本轮最终结果 | 日志/产物 |
|---|---|---|
| 全仓 unittest | **1110/1110**，200.770 秒，0 fail/error | `logs/full-final.log` |
| 专项与邻接 | **174/174**，44.008 秒，0 fail/error；含新增 30 条 | `logs/targeted-final-02.log` |
| broker check | **25/25**，0 skip；offline + 禁外网 sandbox | `logs/broker-final.log` |
| wheel/sdist | 均成功构建 | `logs/build-final.log`、`dist/` |
| 干净安装 | Python 3.13，`--no-index --no-deps`；import 来自新环境 site-packages | `logs/install-final.log` |
| installed 预检 canary | 正常快照 local pass；加入跨日未结算占额后 blocked；预检前后 authority 不变 | `installed-preflight/result.json`、`logs/installed-preflight.log` |
| installed 合成 broker/socket | **1 次合成本地 Unix socket 请求**；重复生成不再发请求；50000 微美元预留、1000 微美元合成结算 | `installed-budget/result.json`、`logs/installed-budget.log` |
| installed HTTP/writer/staging | 通过；既有人工 fixture 流程，不是真实 human accept 或 live 操作 | `installed-document_extraction/result.json`、`logs/installed-document_extraction.log` |
| installed hermetic replay | **1/1**，0 provider call | `logs/hermetic-installed.log` |
| 静态与包内容 | 218 个 runtime 文件与 wheel 逐字节一致；165 个 contract JSON、293 个 Python AST；compileall、diff check 通过 | `package-verification.json`、`logs/static-package-final.log`、`logs/compileall-final.log` |

专项覆盖缺文件/父目录、特殊路径、只读权限与拒绝写 SQL、旧 schema、WAL 提交与缺 sidecar、读取建议不 chmod、Core/Scheduler/staging 无逻辑变化、无 broker/凭据读取、认证和 closed params、source/context/mission/governance 漂移、缺外层 cap、配置错误、profile 过期、routing/budget policy 换版、跨 mission/日占额、共享调用数、overrun、输入字节上限，以及预检后预算耗尽时实际执行拒绝且零 adapter 调用。

### 运行隔离与失败记录

- 只在 `/Users/everflow/.openclaw/workspace/temp/dalton-p9d3b-preflight` 写入。原仓仅作为既有 Python 解释器/依赖的只读来源；用 `PYTHONPATH=<clone>/src` 验证源码 import。安装验收移除 src 路径，另外核验 site-packages。
- 测试使用 clone 内 TMPDIR、禁止自动写 bytecode 的环境变量和自建的更严格 macOS 验证 sandbox；最终 sandbox 禁止 clone 外写入和非 loopback 网络。没有修改 OpenClaw 配置、skills、原 venv 或系统权限。
- 长目录会超过 macOS Unix socket 限长。仅测试环境用 `sitecustomize.py` / `node-paths.cjs` 把 in-clone socket 地址改成指向同一文件的较短相对拼写，不移动到别处、不使用 symlink、不修改生产接口或 fixture 输出。
- 既有两处测试/canary 原来硬编码 `/tmp`，本轮改为遵循 TMPDIR。broker 虽没有 node_modules，check 只需已安装 Node 的 builtin 模块，无需下载依赖。
- `preflight-01.log`：初轮有 SHM 字节断言和过长 socket 路径失败，随后修正验收口径和临时目录；并移除 import TestCase 带来的重复枚举。`preflight-02.log` 为当时 25/25。
- `full-01.log` 是增加最后 5 条回归前的 **1105/1105**，不替代最终 1110。`targeted-final.log` 的 1 个 HTTP 错误来自自建 sandbox 把 loopback 也拦住；按本机 sandbox 语法精确允许 localhost TCP 后，最终专项和全仓都重跑通过。没有放开外网。
- `install-final.log` 中 pip 的系统缓存不可写警告说明 sandbox 阻止了 clone 外缓存；wheel 安装成功，未修改系统缓存。初次 npm 打印升级提示，最终重跑显式 offline/关闭 notifier，没有执行升级或安装。
- checkpoint 分阶段保存在本证据目录，初次失败日志未删除。

## 尚未验证与导回边界

没有 live 数据 canary、真实 broker 身份或来源送模许可核验，没有真实模型输出/抗提示注入/真账单验证。当前新预检只在认证 writer RPC 提供，没有新增 Cockpit 交互，因此本轮没有重跑浏览器截图或人工视觉审阅，也不沿用旧截图冒充本轮验收。

没有部署、重启、push、merge，没有新授权、live config 写入、自动 accept Claim/Thesis，未将代码复制回原仓。交付只含本地 commit 和从 `f0ab1cb` 导出的 format-patch；导回、审阅与启用由后续授权流程处理。
