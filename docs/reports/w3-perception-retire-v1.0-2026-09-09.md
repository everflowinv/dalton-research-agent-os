# W3 · Perception/Agenda 平面退役（D3）

*2026-09-09*  ·  分支 `w3-perception-retire`，基线 main `ebd2ea8`（3,932 tests）

依据：[vision 回顾 D3](vision-review-against-plan-v1.0-2026-09-09.md)、[P14a §10](p14a-daily-tracking-v1.0-2026-09-09.md)、[并行开发计划 v1.0](parallel-development-plan-v1.0-2026-09-09.md) D3、[ADR-0009](../adr/0009-one-event-plane.md)。

裁决在片外已经做完：**退役，不迁移**。`perception.py` 读的是另一个产品（万华那套，09-04 已退役）的 legacy Coverage sqlite，没有可迁移的输入；`ResearchEvent` 是唯一事件平面。本片只做一件事——把它明确关掉、写清楚、并且让它不可能被误开。**没有删除任何模块或测试。**

## 1. 关掉了什么

| 面 | 之前 | 现在 |
|---|---|---|
| service 日跑 | `service.json` 的 `agenda.enabled: true` 就构造 `AgendaCoordinator` 并按 `interval_seconds` 排期 | 还要 `legacy_agenda_plane: true`（默认 `false`）。缺这一个键，`config.agenda` 是 `None`，协调器不构造，executor 不创建，日跑不排期 |
| 启动日志 | 无 | 关闭时 `start()` 向 stderr 写一行 `legacy agenda plane retired (ADR-0009)`（launchd 收进 `~/Library/Logs/Dalton`） |
| heartbeat | `agenda.state: "disabled"` | 未开 opt-in 时 `"retired"`；opt-in 开着但没有 config 时仍是 `"disabled"`；开着并构造成功仍是 `"pending"` |
| `/legacy` | `/v1/agenda` 看起来像活队列 | 同一份 payload 带 `retired: true` / `retirement_ref: "adr:0009-one-event-plane"` / `retirement_note`；页面把说明渲染在议程页签顶部，空列表文案改成「议程平面已退役，没有历史记录」。**路由照旧只读服务历史** |
| 历史 | — | `perception_snapshot_versions`（live 103 条）、`agenda_cycles`（live 112 条）不动、不删、照旧可读 |
| `priority_overrides` | 议程排序的唯一合法通道 | 由 `TrackingCadenceVersion` 取代（带 `change_reason`、证据、版本链、`duplicate` 拒绝）；`active_priority_overrides` 保留为读历史，不再是操纵杆 |

被关掉的**只有构造**。`agenda` 配置块仍然被解析、仍然做闭形状校验——留在机器上的坏块照旧报 `ServiceConfigError`，退役不吞错误。

一次全新安装根本不会写这个键：`bootstrap` 生成的 `service.json` 里没有 `agenda` 块，也没有 `legacy_agenda_plane`，所以新机器默认就是退役态。`install.sh` 未改。

## 2. 怎么开回来

一个布尔值，可逆：

```json
{ "schema_version": "0.1", "...": "...", "legacy_agenda_plane": true,
  "agenda": { "enabled": true, "interval_seconds": 86400, "config": { ... } } }
```

改完重启 controller。开着时行为与从前**完全一致**：同一个块、同一个 interval、同一个单线程 executor，并且不再打那行退役日志。关回去就是删掉这个键（或置 `false`）。

删除模块是另一次决定、另一次改动；本片刻意留成一个布尔值能翻回来的形状。

## 3. 改动清单

- `src/dalton_core/service.py`：新增 `LEGACY_AGENDA_PLANE_KEY` / `LEGACY_AGENDA_RETIRED_LINE`；`ServiceConfig.legacy_agenda_plane: bool = False`（进 optional 键集，非布尔报错）；`agenda_raw["enabled"] and legacy_agenda_plane` 才构造；`start()` 关闭时打一行 stderr；`_agenda_state` 初值区分 `retired` / `disabled` / `pending`。
- `src/dalton_core/agenda_control.py`：`AGENDA_PLANE_RETIREMENT_REF` / `AGENDA_PLANE_RETIREMENT_NOTE`，`AgendaControlPlane.view()` 返回值加三个字段。
- `src/dalton_core/cockpit_control_legacy.html`：议程页签的 section-note 写明退役并由 `data.retirement_note` 覆盖；空列表文案。
- `docs/adr/0009-one-event-plane.md`（新）。
- 测试：`tests/test_service.py`、`tests/test_agenda_control.py`。

## 4. 测试

新增 5 条（3,932 → 3,937）。逐条：

| 文件:行 | 测试 | 钉住的东西 |
|---|---|---|
| `tests/test_service.py:166` | `test_an_enabled_agenda_block_alone_no_longer_builds_the_coordinator` | 活机器上那份 `enabled: true` 的配置不再够：`config.agenda` 与 `agenda_interval_seconds` 都是 `None`，`service._agenda is None`，`start()` 两次只打**一行** `legacy agenda plane retired (ADR-0009)`，executor 没建，heartbeat 里 `agenda.state == "retired"` 且 `last_started_at` 是 `None`，服务本身仍 `running` |
| `tests/test_service.py:198` | `test_the_named_opt_in_restores_the_plane_unchanged` | 加上 `legacy_agenda_plane: true` 之后照旧：interval 86400、`company_ref == "wanhua"`、`perception_source_db` 指向那份 legacy sqlite、`_agenda` 是 `AgendaCoordinator`、state `pending`、`start()` 建了 executor 且**不打**退役行 |
| `tests/test_service.py:224` | `test_the_opt_in_is_boolean_and_the_block_shape_is_still_checked` | `"true"` 这个字符串被拒；退役不吞坏块（缺 `interval_seconds` 仍报 `ServiceConfigError`）；`legacy_agenda_planes` 这种近似拼写作为未知顶层键被拒，opt-in 混不进来 |
| `tests/test_service.py:244` | `test_a_fresh_install_never_writes_the_opt_in` | `bootstrap` 写出的 `service.json` 里既没有 `legacy_agenda_plane` 也没有 `agenda`，读回来 `legacy_agenda_plane` 是 `False` |
| `tests/test_agenda_control.py:652` | `test_retired_agenda_view_still_serves_history_and_says_it_is_retired` | 已送达的决定照旧读得到（`company_ref == "wanhua"`、`decision_ref` 对得上），同一份 payload 带 `retired` / `retirement_ref` / `retirement_note` 且说明里含 `ADR-0009`；`/legacy` 页面确实读了 `data.retirement_note` |

全量：

```
PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .
Ran 3937 tests in 443.961s
OK (skipped=1)
```

## 5. 没做的事

未删除 `perception.py` / `agenda_coordinator.py` / `agenda.py` 及其测试；未动 `coverage_mission`、`bounded_planner_driver`、`macos_launchagent`、`install.sh`、`PROJECT_STATUS`、`tests/test_lane_registry`；未 push、未部署、未对 live 写入。
