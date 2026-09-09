# Wave 0：lane registry、schema glob、词表与两份 ADR 草稿 v1.0

日期：2026-09-09
分支：`wave0-lane-registry`（基于 main `47d3316`），HEAD `d4145e1`，未 push
执行：Opus 5 subagent，worktree `~/Projects/dalton-wave0-lane-registry-worktree`
验收：`Ran 2071 tests in 181.409s` / `OK (skipped=1)`（基线 2,034，新增 37 项）

---

## 1. 一句话

一条 tick lane 原来要改 `writer_server.py` 的十个区域外加 12 个共享文件；现在要改
**它自己的模块 + 它自己的 `*_schema.sql` + 它自己的测试 + `lane_registry.LANE_MODULES` 里的一行**。
cockpit 与 `install.sh` 仍需人工接线（见第 6 节），这两处 Wave 0 没有收。

## 2. 改了什么（文件清单）

**新增**

| 文件 | 是什么 |
| --- | --- |
| `src/dalton_core/lane_registry.py` | `LaneSpec` 冻结记录、注册表、`LANE_MODULES` 显式 import 列表、三个消费点用的派生函数 |
| `src/dalton_core/writer_lanes.py` | 四条「writer 自己实现」的 lane 的 spec（无 handler，见第 4 节） |
| `src/dalton_core/model_configurations.py` | 模型配置文件名注册表（原 `raise_day_budget_cap.MODEL_CONFIG_NAMES`） |
| `tests/test_lane_registry.py` | 注册一条假 lane → writer / driver / launchagent 三处同时出现；重复注册被拒；旧字面量被钉成期望值 |
| `tests/test_model_vocabularies.py` | `PURPOSES` 与模型配置名两个注册表 |
| `tests/test_mission_vocabulary_extension.py` | 新词可授予、live mission 未授予、JSON 合同与代码一致 |
| `docs/adr/0007-thesis-revision-candidates-and-verified-figures-in-the-ledger.md` | 草稿，`Status: proposed` |
| `docs/adr/0008-research-outputs-are-never-terminal.md` | 草稿，`Status: proposed` |

**改动**

| 文件 | 改动 |
| --- | --- |
| `src/dalton_core/writer_server.py` | 操作集 / `OPERATION_FIELDS` / `__init__` / `close()` / `_handle` 分发 / `main()` argparse 与 launcher 构造全部从 registry 派生；删掉五个 `_op_dispatch_*` 方法；新增 `state_dir`、`lane_launcher()`、`lane_company_checklist()`、`lane_state`、`install_lane_operations()` |
| `src/dalton_core/bounded_planner_driver.py` | `run_once` 的九个 try/except 收成一个按 `order` 的循环 |
| `src/dalton_core/macos_launchagent.py` | writer `ProgramArguments` 拼接 `lane_argv(LaunchAgentContext(...))`；`STATEMENT_LANE_USER_AGENT` 移到 statements lane 模块 |
| `src/dalton_core/mission_statement_lane.py`、`mission_model_spec_lane.py`、`mission_sec_quarters.py`、`initial_screen_launcher.py`、`research_planner_launcher.py` | 各自加 `dispatch` / `add_arguments` / `build_launcher` / `argv_fragment` 与 `register_lane(LaneSpec(...))` |
| `src/dalton_core/cockpit_model.py` | `PURPOSES` → `register_purpose()` / `purposes()`，六个词作种子 |
| `src/dalton_core/coverage_mission.py` | `AUTOMATION_WRITE_SCOPES` +10 词、`CHECKPOINT_KINDS` +3 词 |
| `contracts/coverage-mission-version.schema.json` | 两个 enum 与代码对齐（顺带补上早已漂移的四个 scope） |
| `pyproject.toml` | package-data 的 37 个 schema 名 → `schema.sql` + `*_schema.sql` |
| `scripts/raise_day_budget_cap.py` | 读 `model_configurations.model_config_names()` |
| `tests/test_packaging.py` | 改为「磁盘上每个资产都被某个 pattern 覆盖」 |

## 3. LaneSpec API

```python
@dataclass(frozen=True)
class LaneSpec:
    operation: str                       # writer 操作名，必须 dispatch_ 开头
    order: int                           # controller tick 里的位置，全局唯一
    driver_key: str | None = None        # tick 摘要里的 key；None = controller 不驱动
    core_discovery: bool = True          # 是否进 CORE_DISCOVERY_OPERATIONS
    param_fields: frozenset[str] = frozenset()          # OPERATION_FIELDS 条目
    handler: Callable[[WriterServer, Mapping], Any] | None = None
    init_kwarg: str | None = None        # WriterServer(...) 上 launcher 的关键字
    argparse: Callable[[ArgumentParser], None] | None = None
    launcher_factory: Callable[[Namespace], Any | None] | None = None
    argv_fragment: Callable[[LaunchAgentContext], list[str]] | None = None
    note: str = ""
```

`LaunchAgentContext(state: Path, extraction_model_config_path: str | None, candidate_staging_path: str | None)`：
`argv_fragment` 只能看这三样。计划里写的是 `argv_fragment(state_dir)`，实际不够用——
initial screen 与 research plan 两条 lane 的开关本来就依赖「装没装 extraction 模型配置」，
所以 context 多带了两个字段。`core_only` 改名 `core_discovery`（语义就是「进不进
`CORE_DISCOVERY_OPERATIONS`」），`build_coordinator` 并入 `handler`。

**登记一条新 lane（十行）**

```python
# src/dalton_core/market_price_lane.py
from .lane_registry import LaneSpec, register_lane

def dispatch(server, params):
    launcher = server.lane_launcher("market_price_launcher")
    if launcher is None:
        return {"status": "unconfigured", "reason": "no market price lane on this writer"}
    from .market_price_coordinator import MarketPriceCoordinator   # 懒 import
    return MarketPriceCoordinator(store=server.store, launcher=launcher).dispatch_once()

def add_arguments(parser): parser.add_argument("--market-price-governance")
def build_launcher(args):
    if args.market_price_governance is None: return None
    from .market_price_launcher import MarketPriceLauncher
    return MarketPriceLauncher(state_dir=Path(args.db).parent, governance_path=args.market_price_governance)
def argv_fragment(ctx):
    record = ctx.state / "connector-governance" / "yfinance-download-v1.json"
    return ["--market-price-governance", str(record)] if record.is_file() else []

register_lane(LaneSpec(operation="dispatch_market_prices", order=120, driver_key="market_prices",
                       handler=dispatch, init_kwarg="market_price_launcher", argparse=add_arguments,
                       launcher_factory=build_launcher, argv_fragment=argv_fragment))
```

外加 `lane_registry.LANE_MODULES` 里一行 `"dalton_core.market_price_lane",`。
`writer_server.py`、`bounded_planner_driver.py`、`macos_launchagent.py` 一个字不用改。

**约束**：注册表拒绝重复的 `operation`、重复的 `order`、重复的 `driver_key`、重复的 `init_kwarg`；
`operation` 必须 `dispatch_` 开头；有 `launcher_factory` 就必须有 `init_kwarg`；
lane 模块的 import 期副作用只能是 `register_lane`，`lane_registry` 本身不 import `writer_server`
（有测试钉住）。

## 4. 迁了哪些、没迁哪些

**完整迁移（handler + launcher + argparse + argv 都在自己的模块里）**

| lane | 模块 | init_kwarg | order / driver_key |
| --- | --- | --- | --- |
| SEC quarters | `mission_sec_quarters.py` | 无（无 launcher） | 70 / `mission_sec_quarters` |
| statements | `mission_statement_lane.py` | `statement_lane_launcher` | 80 / `mission_statements` |
| company model spec | `mission_model_spec_lane.py` | `model_spec_launcher` | 90 / `company_model_spec` |
| research plan | `research_planner_launcher.py` | `research_planner_launcher` | 100 / `research_plan` |
| initial screen | `initial_screen_launcher.py` | `initial_screen_launcher` | 110 / `initial_screen` |

**只登记不搬 handler**（`handler=None`，`_op_*` 仍在 `writer_server`）：

| lane | 为什么没搬 |
| --- | --- |
| `dispatch_mission_source_discovery` (30) | 一个 op 驱三个 coordinator（AlphaEngine / SEC filings index / web search），共享一个 deadline，三份 plan 路径与三个 launcher 都是 writer 构造参数。搬出去等于把 writer 的一半私有状态搬出去 |
| `dispatch_document_extraction` (40) | coordinator 在 `start()` 里用 extraction 模型配置与三个 window 上限构造，那段配置校验属于 writer |
| `dispatch_mission_stage` (50) | 没有 launcher，纯读 writer 自己的 store 与三份 discovery plan |
| `dispatch_claim_review` (60) | 没有 launcher，要 writer 的 transcript spool、store、challenge authority 和三份 plan 的 needles |

这四条**仍然**从 registry 拿到操作集成员、`OPERATION_FIELDS` 条目与 tick 位置和 key——
也就是新 lane 会撞车的那部分；只有方法体留在原处。测试钉住「没有 handler 的 lane 必须有
`_op_<operation>` 方法」。

**完全没登记**：`dispatch_coverage_mission_sec_lane`（S7d 公司事实 lane）。它不在
`CORE_DISCOVERY_OPERATIONS` 里，tick 里排在 `reconcile_forecasts` **之前**，而
`reconcile_forecasts` 不是 lane。把它塞进 registry 就得让 driver 循环在中间插一个非 lane 调用，
比留一个字面量更难读。它的 launcher（`sec_lane_launcher`）也和 `--candidate-staging` 绑在一起，
和其它 lane 的开关规则不同。

## 5. 行为等价的证据

- 全量 2,071 项通过；`test_service.py` 的 launchagent argv 断言一字未改，全部通过。
- `tests/test_lane_registry.py` 把 P14-0 之前的 `CORE_DISCOVERY_OPERATIONS`（14 项）、九条 lane 的
  operation 名、各自的 `param_fields`、以及 tick 的九步顺序与 key 原样写成期望值。
- **有意的差异一处**：writer plist 里 `--research-planner-model-config` 与
  `--initial-screen-model-config` 从「extraction 块的尾部」移到「lane 片段区」（statements /
  model-spec 原来的位置），因为现在九条 lane 的片段按 `order` 一次拼完。argv 的**内容**与
  **出现条件**完全不变，只有位置变了；argparse 不在乎，测试是成员判断也不在乎。
  相对顺序（planner 在 initial screen 之前）保持不变。
- driver `run_once` 返回的 dict 键顺序从「字面量顺序」变成「调用顺序」。键集合与值不变；
  没有测试或消费者依赖 dict 键顺序。
- schema 打包：真跑了一次 `pip wheel --no-deps --no-build-isolation`，wheel 里 37 个 `.sql`，
  磁盘上 37 个，`missing: []` / `extra: []`。

## 6. 集成时仍要人工做的

1. **cockpit**（`cockpit_plane.py` + `cockpit_control.html`）：没有 lane 接缝。新 lane 的
   owner 可见输出仍要人工接线。这是计划里就说好由集成 agent 统一做的，Wave 0 没碰。
2. **`deploy/macos/install.sh`**：治理种子块与模型配置块仍是逐条硬写。`argv_fragment` 只管
   writer plist 的参数，**不管**「把 governance 记录种到 `connector-governance/` 里」这件事——
   一条新连接器 lane 仍要在 install.sh 里加种子。这是 Wave 0 明确没有收的第二处。
3. **`register_purpose()` / `register_model_config_name()` 的调用点**：这两个注册表是「可以被
   调用」，但没有任何 lane 现在调用它们。新 lane 要在自己模块的 import 期调用一次；
   `model_configurations` 不在 `LANE_MODULES` 的自动 import 链上（它不是 lane 概念），
   所以调用它的模块必须确保自己被 import 到（lane 模块天然会被）。
4. **mission 版本**：`AUTOMATION_WRITE_SCOPES` 与 `CHECKPOINT_KINDS` 只是加词，live mission
   一个新 scope 都没授予。第一条要写 `market_price` 的 lane 上线前，owner 要发一次新
   mission 版本。测试同时钉住了「manifest 未授予」和「mission 可以授予」。
5. **`deploy/phase9/p9a-us-it-services-mission-v1.json` 未改**。它是 live mission 的清单，
   `tests/p9a_fixtures.py` 与 canary 脚本都读它；按任务要求只改代码词表，不动清单。
   （它没有被哈希钉死，是我选择不改。）

## 7. 值得知道的两件事

**JSON 合同早就漂移了。** `contracts/coverage-mission-version.schema.json` 的 `may_write` enum
少了 `claim_challenge`、`deliverable`、`forecast_reconciliation`、`source_discovery` 四个词——
这四个在代码词表里存在很久了。原因是这个 schema 文件没有任何机器消费者：
`validate_coverage_mission_version` 是手写的 Python，`test_coverage_mission` 只比对 `required`
字段名。我把 enum 补成与代码一致，并加了一条测试钉住，但**它仍然只是文档**：改了代码词表
而忘记改它，除了这条新测试之外没有别的东西会发现。

**`install_lane_operations()` 是为可测性存在的。** writer 的三张表是模块级常量，在 import 期
从 registry 折进来；这对 `LANE_MODULES` 里的 lane 是对的，但对「测试里临时注册一条 lane」
没用——driver 和 launchagent 都在调用时读 registry，唯独 writer 在 import 时读。所以这次折叠
被写成一个具名函数，import 路径调一次，测试可以再调一次。它记得自己上次装了什么，所以
撤销注册的 lane 会把参数合同一起带走。

## 8. 测试结果（原文）

```
Ran 2071 tests in 181.409s

OK (skipped=1)
```

（基线 2,034 / OK / 1 skipped；新增 37 项：lane registry 17、model vocabularies 10、
mission vocabulary 6、packaging 净 +4。）

## 9. 开放问题

1. **`dispatch_coverage_mission_sec_lane` 该不该也进 registry？** 要进的话得给 driver 一个
   「非 lane 调用也有 order」的概念，或者把 `reconcile_forecasts` 也当成一条 order=20 的
   伪 lane。后者更整齐但会让「lane」这个词失去意义。留着没做。
2. **`source discovery` 的三个子源本身像三条 lane。** AlphaEngine / SEC filings index /
   web search 各有 plan、launcher、coordinator 和自己的失败原因，却挤在一个 op 里共享一个
   deadline。Wave 1 A 要加行情源时会再遇到这个形状。拆不拆是个单独的决定。
3. **`argv_fragment` 管不到 install.sh 的治理种子。** 一条连接器 lane 的「开关」其实有两半：
   writer 参数（已收）和 `connector-governance/*.json` 的种子（未收）。要不要给 LaneSpec 加一个
   `governance_seed` 字段，让 install.sh 也从 registry 派生？
4. **ADR-0007 的 figure 路径要给 staging store 一个新句柄。** `CandidateStagingStore` 今天是
   owner-only 的 scratch authority，没有 Ledger 句柄；按草案它要能只读 mission 表来复核数字。
   这是一次实打实的可见范围放宽，实现时该单独过一遍。
5. **ADR-0008 的 `change_reason` 词表冻结在哪一层？** 草案把它写成合同，但没说它住在
   `coverage_mission.py` 的词表里还是各 authority 自己的模块里。Wave 1C 第一个实现它的人
   会替所有人决定这件事。
