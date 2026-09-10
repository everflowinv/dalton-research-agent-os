# 每个环节用哪个模型：选择、自动登记、消失时的回退

**P14-M2 — 2026-09-10 — 分支 `w3-model-selection`（基线 main `86121a3`）**

owner 的话是一句：在 cockpit 上给每个调用环节选模型；模型列表跟着 OpenClaw 对外
展示的 provider 走，新模型自动登记为 Dalton 可用。中途 owner 又加了一条硬要求：
OpenClaw 上消失的模型，Dalton 必须自动回退、并且**绝不能悄悄地回退**。

下面是做成了什么、为什么这样做、以及 owner 要做的事。

---

## 1. 一个选择就是一个 routing policy 版本

没有新增「选择账本」。**选择本身就是一个新的 pinned routing policy 版本**，它的
`purpose_overrides` 里写着某个环节用哪几个模型、按什么顺序。

```json
"purpose_overrides": {
  "plan": {"mode": "explicit", "chain": ["profile:claude-fable-5-1", "profile:gpt-6-astra"]},
  "draft": {"mode": "tier"}
}
```

这一个决定顺手买到四件事，换成一张表就得一件件重新挣：

* **下一次调用就生效，不用重启。** 每条 lane 本来就从自己 pin 的 policy 版本里
  取链；`set_model_selection` 发布新版本后，按 `raise_day_budget_cap.py` 的老办法
  把每个已登记的 model config 文件的 `routing_policy_ref` 指过去。lane 的子进程
  每次启动都读配置文件，cockpit 也会在文件变了以后重新读。
* **回滚 = 把上一版的选择再发布一次。** 旧版本一个字节都没动，仍然可被 hash 校验，
  仍然是六月那条 route decision 指着的那个东西。
* **不会和实际跑的东西对不上。** route decision 记录了它路由时用的 policy hash，
  所以「这条 Claim 产出时 owner 选的是哪个模型」只看 decision 就能答。
* **append-only 是白送的。**

`mode` 只有两个：`tier`（跟随档位）和 `explicit`（自己点名）。「跟随档位」也写进
版本里而不是留空，这样页面能区分「没人看过」和「看过了，决定跟着走」。

### 发布时会被拒绝的三件事（各一句话）

| 情况 | 拒绝理由 |
| --- | --- |
| 选了这台机器没有档案的模型 | `X has no profile on this machine; the catalog lane registers a model before it can be chosen` |
| 未定价的模型排在非最后一位 | `X has no published price, so it can only be the last resort; put it at the end of the chain or leave it out` |
| verifier 环节选了 producer 的家族 | `X is in the <family> family, which is what produces the work this stage checks; a verifier from the producer's own family is not an independent check -- choose a different family` |

第三条在**调用时**还会再说一次，而且是对着**真正跑过的**那个模型的家族说的
（`served_family` 从 producer 自己那条不可变的 decision 里读），因为 producer 可能
本身就回退过。

## 2. 未定价的模型

网关给了模型、但没给 rate card 时，profile 上多一个 `unpriced: true`，路由规则是
**只能当链条的最后一环**。理由不是洁癖：预算日账本是按估算准入的，一个估不出价钱的
调用没法诚实地准入。没有链条时（单发路由）直接拒绝，理由 `unpriced_model_requires_chain`。

配套地，`_provider_models` 不再因为某个 provider model 少一个 `cost` 就把整份目录
读崩——被藏起来的恰恰是最该看见的那一条。

**不写 0（review B2）**：0 不是「不知道多少钱」，是「免费」——按成本升序排的 policy
会把它排第一，日账本会按 0 结算。所以未定价的 profile 登记时带一张**声明的上限
rate card**：`UNPRICED_CEILING_INPUT_PER_MILLION_USD = 25.0` /
`..._OUTPUT_... = 100.0`，比现有目录里最贵那个模型的分档上限（gpt-6-astra 超过 27.2 万
token 后的 20/75）还高。预留会**多留**，这是「未知」该失败的方向；结算也用同一个数，
所以未定价的调用至少会以「它可能花的钱」出现在当天的花费里。

**curated rate card 不被覆盖（review B2）**：网关不再公布某个模型的价格，不等于
Dalton 自己 curated 的那张卡失效。所以 curated 分支保留原卡，也**不**标 unpriced——
我们仍然知道它多少钱，只是不再从 provider 条目上读。

**「最后一环」按活着的环算**：一个未定价的环排在一个已退役的环前面，按声明顺序算会
被判「不是最后一个」而拒绝，而它其实是**唯一剩下的**。

## 3. 每小时的目录同步 lane

`dispatch_catalog_sync`（order 20，tick 里第一个跑，maintenance 池，driver_key
`catalog_sync`）：

1. 读 `openclaw.json` 的两个子树：`models.providers` 与 broker 插件自己的 entry；
2. `sync_openclaw_model_catalog`：broker 有而这里没有的 → 登记为 live profile（有
   rate card 就用它，没有就 `unpriced`）；这里有而 broker 没有的 → **退役，不删除**；
   退役后又出现的 → 追加一个 live 版本回来；
3. 算三个差集，写回 tick 摘要（只写名字与计数，不写目录全量）。

排在第一个是有理由的：这一轮里任何一次模型调用，都该按**现在**的目录准入，而不是
一小时前的目录。

它**从不写网关配置**，**从不调用模型**，**不上网**。同一小时内跑第二次是 `idle`
（连宿主文件都不读）；跨小时但目录没变是 `current`，什么都不写。

开关是一个文件 `state/model-catalog-sync.json`，里面写着要跟哪份 openclaw.json、
同步进哪个 router 库。两条路径都是被指定的，不是猜的——Dalton 的进程不该自己去
翻宿主机的配置。install.sh 在同步目录那一段后面顺手写这个文件（只在
`~/.openclaw/openclaw.json` 在的时候），已存在则不动，所以 owner 改过就保留。

## 4. 三个差集，与 cockpit 的 「模型」 页

| 差集 | 意思 | owner 能做什么 |
| --- | --- | --- |
| `in_openclaw_not_allowed` | 网关有，broker 的 `allowedModels` 没放行 | 点「放行」 |
| `allowed_not_in_dalton` | 已放行，这台机器还没登记档案 | 等下一个整点，lane 自动登记 |
| `dalton_not_in_openclaw` | 这台机器还留着档案、网关上没有了 | 等 lane 退役它（不删除） |

页面上每个环节一行：档位、**实际会走的链**（跟随档位 / 自己点名 / 「你选的都退役了，
暂时按档位走」）、上一次是链上第几个模型服务的与那一次的估算花费、以及一个选择器。
下面是目录三列，只有第一列有「放行」按钮。

三个按钮都经 writer 以 owner 的 Tailscale 主体出去（ADR-0006）：cockpit 进程没有
Core 的写句柄，也没有宿主配置的写句柄——所以「放行」发出去的是**模型名，永远不是
路径**。

## 5. 放行（写 openclaw.json）

`openclaw_allow_patch` 只做最小的 broker 子树补丁：`llm.allowedModels` 追加一个
字符串，`config.profiles` 追加一个对象。顺序是安全的全部：

1. 先写带时间戳的备份 `openclaw.json.bak-dalton-allow-<YYYYMMDDTHHMMSS>`；
2. 写临时文件、rename 覆盖；
3. **把落地的文件读回来**，与「我们打算写的对象」比对，再与「原来那份」比对，确认
   只有 `plugins.entries.dalton-openclaw-model-broker` 变了；
4. 任何一步校验不过 → 把原文写回去（备份保留：会删掉自己证据的还原不叫还原）。

不在 `models.providers` 里的模型一律拒绝——Dalton 读宿主的 provider，不配置它。
profile id 优先用 Dalton 自己 curated catalog 已有的那个（例如
`zai/glm-5.3-flash` → `profile:zai-glm-5-3-flash`），否则按 model id slug，重名时
带上 provider。凭空造一个新 id 会让同步登记新的、**退役 curated 的那个**，正好相反。

写完记一条 append-only 的决定（`model_openclaw_allow_decisions`）：谁、放行了什么、
备份叫什么、动了哪些 key，并把重载指令一起返回。

## 6. 模型在 OpenClaw 上消失时（owner 2026-09-10 的硬要求）

1. **退役**：走既有的 sync（追加一个 `status: retired` 的新版本，旧版本不动）。
2. **自动回退**：
   * 链的**第一环**退役 → 路由本来就会拒绝它这一个候选人并选第二个，所以是白送的；
     这里加了测试钉住（`test_a_retired_first_link_falls_to_the_next_one_by_itself`）。
   * owner 点名的链**整条**都退役 → `resolve_chain` 用该档位自己的链顶上，并带
     `superseded_chain` 说明发生过这件事。**选择本身不改**——它是不可变版本的内容，
     重新选是 owner 的动作。拒绝调用会是「换一种方式的沉默」。
   * **verifier 例外**：独立性是对着**还活着的**链环算的。如果剩下的环全是 producer
     的家族，调用被**拒绝**，理由一句话（哪个家族、哪个模型退役了、去哪里改），而
     不是回退到一个注定会通过的「检查」。
3. **通知**：一条 append-only 的 `model_fallback_notices`，主键就是 (模型, 环节)
   这一对，所以每小时都看见同一个退役也只写一次。

   **提示集是从「现在有哪些是退役的」算出来的，不是从「这一轮退了哪些」（review
   B1）。** 原因是顺序：install.sh 会先跑同一个目录同步，等这条 lane 第一次到整点时
   delta 早就没了——第一次部署就会**静默地**退役 6 个模型；同步提交与写提示之间任何
   一次异常也会丢掉剩下的。按当前状态扫描没有这个窗口：它在 (模型, 环节) 上幂等，所以
   每小时跑一遍，每条只写一次，并且会补上上一轮丢掉的。owner 自己点名的链和它背后的
   档位链都会被扫，因为选择没坏不代表它要回退到的那条链没坏。

   文案：
   > 模型 profile:gpt-6-astra 已在 OpenClaw 消失；环节 决定下一步做什么（plan）
   > 已自动回退到 profile:claude-fable-5-1；如需更改请到 cockpit 模型页选择

   没有可用替代时改说「没有可用的替代模型，现在一次也调不了」。
4. **可见**：同一条提示出现在 cockpit 「模型」页顶部**和** approvals/待办列表里，
   直到 owner 点「知道了」（human-only 治理 op）或重新选。升级前的 router 库没有这
   两张表，页面读成「没有提示」而不是报错（review S4）——owner 的页面不该是发现这件
   事的地方。

**投递（Discord / Feishu）没有接**——owner 已把投递推到最后。留下的是一个有文档的
接缝：`model_selection.notice_delivery(notice) -> None`，默认空实现；tick 摘要写
`notification_channel: "cockpit"`。以后做投递时换掉这一个函数体，lane 不用动。
「说什么、什么时候说、多久说一次」这三个决定已经做完并且有测试了。

## 7. owner 要做的事

1. **部署**：跑 `deploy/macos/install.sh`。它会（a）像现在一样同步一次模型目录，
   （b）多写一个 `state/model-catalog-sync.json`。
2. **放行之后一定要重载网关**：broker 在网关启动时才读它的插件配置。
   ```
   openclaw gateway restart
   ```
   页面上的「放行」按钮会把这句话原样弹出来。在重载之前，那个模型对 broker 来说
   还不存在——链走到它会被拒绝、记一条没服务成的 link、继续往下走，不会丢调用，
   但那一环在重载前是死重量。
3. **这台机器现在的实际情况**（只读扫描，2026-09-10，见第 9 节）：live router 还
   停在 P14-M 同步之前——5 个 broker 给的 profile 这里没有，6 个这里有的网关已经
   没有了。跑一次 install.sh（或让 lane 跑一个整点）就会自动补齐并退役。
4. **`VERIFIER_POLICY_REF` 仍然 pin 着 `profile:gemini-3-7-flash`**，那是上面 6 个
   要退役的之一。同步之后 verifier 相位会以 `profile_retired` 被**拒绝**（fail
   closed，不是静默）。改这个不可变的相位 pin 是 owner 的决定，这一支没有动它。
   现在多了一条路：把 verifier 环节在「模型」页上选成一条活着的链。
5. **选模型**：cockpit → 「模型」→ 每行选「跟随档位」或「自己点名」（按顺序填模型
   id，逗号分隔）→ 保存。不用重启。想回退到原来的，把原来的再选一次。

## 8. 没做 / 待定

* **投递**：见第 6 节，只留接缝。
* **未定价模型的上限价是我们自己定的**：25 / 100（每百万 token）是一个保守的声明值，
  不是真实价格。它保证不会少算，但会让那次调用在当天花费里显得偏贵。owner 若希望
  「未定价 = 干脆不可路由」，把 `unpriced_model_requires_chain` 扩到所有位置即可，
  是一行。
* **`allowed_without_broker_profile`**：网关放行了 `antigravity-cli-gateway/gemini-3.1-pro`
  与 `.../gemini-3.8-flash`，但 broker 的 `config.profiles` 里没有它们，所以 Dalton
  根本叫不出名字。页面上单独列出来了。要不要给它们建 profile 是一次「放行」就能做的
  事（`build_allow_patch` 会补 profile 对象），但两个都没标定过，这一支没有替 owner 做。
* **选择是全库统一的**：一次 `set_model_selection` 会给**每一个**已登记的 model
  config 所 pin 的 policy 各发一个新版本。理由是一个 purpose 是「工作的一个环节」，
  不是「某条 lane 的私产」；分开 pin 会让同一个环节因为是谁调的而跑不同模型。
* **`openclaw_catalog_reconcile.py` 被改了几处**（rate card 可缺、`unpriced` 透传、
  上限价常量、curated 卡不被覆盖）。它不在我这一支的所有权清单里，但改动是纯追加的，
  且是「无 rate card 的标未定价」这条要求的唯一落点。集成时值得看一眼。
* **`ensure_*_policy` 现在把 `purpose_overrides` 往前带**（review S2）。不带的话，
  重跑一次 install.sh 就会把 owner 做过的每一个模型选择悄悄抹掉。
* **`purpose` 只在 pinned policy 真的为它带了选择时才进请求身份**（review S1）。
  无条件加进去会改变**历史上每一次**路由请求的身份，重放一个选择功能出现之前的
  work order 会得到 `conflict`，把一条什么都没做错的 lane 停掉。
* **发布时会检查 pinned 是不是该谱系的最新版**：不是就拒绝（「has moved on」），
  因为新版本是拿 pinned 的内容建的、接在 latest 后面，中间那一版改了什么会被无声丢掉。

## 9. 只读扫描（真实 `~/.openclaw/openclaw.json`，2026-09-10，只取名字）

```
in_openclaw_not_allowed          = []
allowed_not_in_dalton            = ['profile:claude-fable-5-1', 'profile:gemini-3-8-flash',
                                    'profile:qwen-deepseek-v4-flash-0731-low-calibration',
                                    'profile:zai-glm-5-3', 'profile:zai-glm-5-3-flash']
dalton_not_in_openclaw           = ['profile:gemini-3-7-flash', 'profile:gemini-flash-latest',
                                    'profile:glm-5-2', 'profile:gpt-5-5',
                                    'profile:openrouter-ox-alpha', 'profile:qwen-deepseek-v4-pro']
allowed_without_broker_profile   = ['antigravity-cli-gateway/gemini-3.1-pro',
                                    'antigravity-cli-gateway/gemini-3.8-flash']
unpriced_model_refs              = []
in_sync                          = False
```

各环节当前的实际链（对着 live router 的只读副本；这台机器还没 pin 带 override 的
policy，所以都是档位链。`live:` 是其中此刻真的能路由到的）：

| 环节 | 档位 | 链 | 现在活着的 |
| --- | --- | --- | --- |
| ask / goal / steer / draft / plan / model_spec / event_judgement / thesis_reflection / dossier / debate_map / deep_insight_gate / industry_framework / earnings_preview / earnings_calibration / conviction_call | brain | `profile:gpt-6-astra` → `profile:claude-fable-5-1` | 只有 `profile:gpt-6-astra` |
| claim_index / quality / street_estimate | cheap | `profile:deepseek-v4-flash` → `profile:zai-glm-5-3-flash` → `profile:gemini-3-5-flash-lite` | `profile:deepseek-v4-flash`、`profile:gemini-3-5-flash-lite` |

没有做任何模型调用、没有联网、没有重启网关、没有写过 live 的 `openclaw.json`。
放行的写入只在临时副本上跑过（测试里）。

## 10. 测试

全量（`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`），
**合入 main（`ba99ef9`，含 prior-research）之后**：

```
Ran 5445 tests in 559.863s

OK (skipped=1)
```

合 main 之前、review 修完时同一条命令是 `Ran 5108 tests in 446.985s / OK (skipped=1)`。

新模块单独跑（`... -m unittest tests.test_model_selection`）：

```
Ran 64 tests in 0.877s

OK
```

新文件 `tests/test_model_selection.py`：64 项，全部离线。覆盖：override 解析
（跟随档位 / 自己点名 / 未定价只能垫底 / verifier 同家族被拒）、policy 版本追加与
回滚（旧版本 hash 不动、重复发布不追加、三个版本的版本链）、fixture openclaw.json
上的三个差集、临时副本上的补丁应用（备份、只动子树、读回校验、按两次不重复、
不在 providers 里的拒绝）、lane 四处登记与开关文件、三个治理 op 的鉴权、Cores 有表
和没表两种情况下的 cockpit 「模型」页，以及第 6 节的四条回退路径 + 去重 + 确认。

review 之后补的十项（`ReviewFindingTests` 与三条散落的）：install 先退役再跑 lane 仍
会通知；同步与写提示之间抛异常后下一轮补齐且不重写；owner 选中的模型退役后提示带
`superseded_chain`；选择功能出现前的 work order 重放仍是 `duplicate`；有选择时同一份
work 是不同请求；重跑 `ensure_planner_policy` 不会抹掉选择与 `actor_ref`；对着已经不是
最新版的 pinned 发布被拒；未定价的环排在已退役的环前面仍可路由；未定价按上限价而不是 0
登记、且高于目录里最贵的那个；网关撤价不会抹掉 curated 卡；放行读回失败时文件逐字节还原、
备份仍在、临时文件不留；升级前的 router 库上「模型」页读成「没有提示」。

## 11. 集成时要接的线

* `LANE_MODULES` / `REGISTRY_LANE_LABELS` / `LANE_POOLS` 三处已登记（`dispatch_catalog_sync`
  本来就在 LANE_POOLS 的「还没登记」区，已移到线上方）。
* 没有新的 `*_schema.sql`：两张通知表与一张放行决定表追加进了既有的
  `model_router_schema.sql`（`CREATE TABLE IF NOT EXISTS`，开库即迁移），所以
  `bootstrap.py` 与 `scripts/rehearse_deploy.py` 的迁移清单不需要新行。
* `scripts/rehearse_deploy.py` 的 `LANE_SWITCHES` 加了一条（`model-catalog-sync.json`），
  `tests/test_rehearse_deploy.py` 里那条「每个播种的开关都要能追到写它的那一行」的
  字面量从两个改成三个。
* writer 多了三个 human-governance op：`set_model_selection`、`allow_openclaw_model`、
  `acknowledge_model_fallback_notice`。第三个是 owner 中途加的要求带来的。
* cockpit 多了一个 GET 路由 `/v1/cockpit/models` 与三个 POST action
  （`model_select` / `model_allow` / `model_notice_ack`），都在 `agenda_control.py`
  的既有分发表里各加一行。
