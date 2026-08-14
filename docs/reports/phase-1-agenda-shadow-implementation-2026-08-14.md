# Phase 1 Agenda Shadow 实施与架构复核

日期：2026-08-14

## 结论

Phase 1 已进入单公司 shadow 运行。Dalton 能根据人类 Mandate 和只读感知快照提出研究问题，真实经过
Scheduler、Model Router、OpenClaw broker、Usage/Cost authority，再由 Core 确定性选出议程并写入
durable outbox。这个阶段只验证“Dalton 会不会选问题”，不执行研究，不写 Evidence、Claim、Thesis
或 Model IR。

旧 `dalton-coverage-*` 的 10 条 cron 仍全部启用。新系统没有取得任何 cutover 权限。

## Fable 独立审阅后的决定

独立审阅确认 commit gate、Scheduler、writer boundary、Router、Capability Registry、Usage/Cost 和
常驻服务确实存在，但此前没有真实研究控制流使用它们。最终决定如下：

- Phase 1 只冻结 Mandate、PriorityOverride、ResearchQuestion、AgendaCycle、AgendaDecision。
- LLM 只生成候选问题、回答标准、展示理由和有界 named features；Core 持有权重并确定性选择。
- 第一个 slice 必须真实走 Scheduler → Router → broker → Usage/Cost，不能用 mock 充当 live 验收。
- Agenda 只读 `PerceptionSnapshot`，不直接依赖旧 Coverage schema；legacy adapter 只是暂时的输入边界。
- 本地 durable outbox 先落地，OpenClaw 外部投递 adapter 后做。
- broker 暂时可继续复用 `chem` agent identity，但 agenda pause 必须能在调用前阻断模型；独立 broker
  identity 是关闭任何旧 cron 前的硬门槛。
- 人类治理使用一次性 token CLI，不在 token 配置里保留长期 human principal。
- 10 个工作日且至少 20 个有人工标签的 question decision，只允许把试点从 1 家扩到 3 家；关闭任何
  旧 cron 仍要求至少 4 周连续 shadow，并覆盖一次真实财报或 filing 事件。认可率阈值尚未由 owner
  决定，因此 `cutover_enabled=false`。

## 已实现

- append-only Agenda authority：policy、mandate、override、question、cycle、candidate、decision、feedback、
  outbox message/event 和 domain event。
- 全局 pause；controller 在任何 broker 调用前检查。
- 单道 coordinator：每日/每 policy version 幂等 cycle，失败 cycle 不占成功日配额。
- 四个 0—3 feature：mandate relevance、catalyst urgency、evidence staleness、decision impact。LLM 不得
  输出单一总分；Core 用 policy weights 和稳定 question-id tie-break 排序。
- legacy Coverage SQLite backup-API snapshot → 规范化 PerceptionSnapshot；Agenda 不读旧 schema。
- ephemeral `dalton-gov`：临时增加 human principal、执行一个治理 RPC、原子移除。
- owner-only SQLite backup/restore、每日 controller backup 和真实 restore 演练。
- 本地 durable outbox 与 delivery receipt 状态机；尚未对外投递。
- Router v2 的 broker-compatible `profile:*` identity，保留 Router v1 历史不改写。
- broker 在读完完整 JSONL frame 后关闭 socket idle timer，模型执行阶段由 request timeout 管理。
- route、adapter、output-contract 失败会同步终结 Scheduler WorkOrder 和 AgendaCycle，避免僵尸 retry。

## Live thin slice 验收

试点公司：万华化学（`wanhua`）。当前 policy：每天最多 1 个成功 cycle、每 cycle 1 次模型调用、
8,000 input tokens、2,000 output tokens、每日 0.50 美元、每月 10 美元；cutover 关闭。

前两次调用暴露了两个真实集成问题，并均 fail closed：

1. Router v1 profile id 与 broker 协议不一致；请求在 broker admission 前停止，未调用模型。
2. broker 的 5 秒 socket idle timer 在模型仍运行时关闭连接；broker journal 完成了模型调用，但 Core 没有
   收到 frame，也没有把它登记为正式 usage。修复后增加慢模型回归测试。

第三个 policy version 的 cycle 成功：

- DeepSeek V4 Flash direct route；broker 回报 agent id `chem`，无 fallback。
- 生成 6 个候选，Core 确定性选出 3 个。
- 1 个 AgendaDecision、1 条 pending outbox message。
- provider-reported usage：88 input tokens、1,588 output tokens；provider 未给 total，authority 保留 null，
  measurement status 为 partial。
- estimated cost：1,067 micros USD，即 0.001067 美元。
- Evidence、Claim、Thesis 表仍为 0；没有研究执行或 belief 写入。
- 两个早期失败 cycle 遗留的 Scheduler retry 已用正式 failed ResultEnvelope 收口，当前无僵尸 ready work。

Core、Scheduler、Router 三库 `PRAGMA integrity_check` 均为 `ok`。运行前已建立 pre-Phase1 快照并完成一次
独立目录 restore；controller 的每日 backup 也已成功执行。

## 当前边界

- outbox 只在本机 pending，不代表用户已收到议程卡；OpenClaw adapter 和补投演练尚未做。
- 公开看板是 owner 明确接受的当前选择。projection 仍禁止 credential、prompt、raw model output、token
  和数据库路径；agenda 语义字段若进入公开 projection，发布后可被抓取和缓存，风险已记录。
- broker 与旧 cron 仍共用 `chem` agent。Dalton pause 已可单独切断新 coordinator，但切换旧 cron 前必须拆出
  Dalton 专用身份。
- controller/writer/worker 仍是同一 macOS 用户。Phase 1 只读 shadow 可接受；运行非 fixture 自生成代码前
  必须使用独立 OS/container identity。
- 旧 cron 的业务覆盖、投递和异常恢复尚未由新 connector 替代。

## 下一步与放权门槛

接下来只做 shadow 运营所需的部分：OpenClaw outbox delivery adapter、delivery receipt/重启补投、人工
agree/disagree 回写、看板 agenda 监督视图和 10 日试点数据。暂不做研究 worker、Verifier、Claim/Thesis
commit、Model IR、Excel exporter 或旧 cron cutover。

从 1 家扩到 3 家之前必须同时满足：10 个工作日、至少 20 个有人工标签的 question decision、零重复外发、
零越权 authority 写入、全部模型调用都有 Usage/Cost、pause 后零 broker 调用，以及一次 worker/bridge 故障
恢复演练。关闭任何旧 cron 还需要至少 4 周 shadow、覆盖一次真实 filing/财报事件、明确的人类认可率阈值、
Dalton 专用 broker identity 和可执行 rollback。
