# Owner 操作记录 2026-09-24

- 执行者：Claude（受系统所有者委托："其他问题你按你认为的最佳方案解决"）
- 线上版本：release `41a772dc`（源 `20a0f53c`，2026-09-24 10:42 UTC 部署）
- 状态目录：`L=~/Library/Application Support/Dalton/state/dalton-core`（等同 `/Volumes/EveSSD/Dalton/legacy-state/dalton-core`，同一 inode），`W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core`
- 规则：只走正式入口（writer 的 human governance 操作、仓库 CLI），不直接写 SQLite、不改状态文件、不改代码、不部署、不重启服务。每一条写操作都记在下面。
- 写操作使用的 actor：`human:owner-delegated-ops-2026-09-24`
- 使用的 CLI：线上 release 自带的 `~/.dalton/runtime/releases/41a772dc…/venv/bin/dalton-gov`，与正在运行的 writer 同一版本

## 写操作

| 时间 (UTC) | 命令 | 对象 | 理由 | 结果 |
|---|---|---|---|---|
| 2026-09-24T11:01:14Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:9b4000aa91b195c94f306a1cad0e2b05`（ACN，提案时间 2026-09-10T15:43，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:007ee4af1361bdf208f4a12b6b23dbd1` |
| 2026-09-24T11:01:25Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:0f83539902ab1616fe94fbbe8704dc5e`（EPAM，提案时间 2026-09-10T15:43，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:b84e60b137ebe390bb8cf54e205804ce` |
| 2026-09-24T11:01:26Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:456cdc657bc4b3b202f55a5c3434d585`（IBM，提案时间 2026-09-10T15:43，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:d5137125cfd00013b30feaa3f53c1f05` |
| 2026-09-24T11:01:26Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:6bfbbd64a234b5ac131627c89f6777fa`（DXC，提案时间 2026-09-10T15:43，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:8e607dbbce5c16e47fad4019e198aa99` |
| 2026-09-24T11:01:26Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:07140a4806d5526af46fef023064dbac`（ACN，提案时间 2026-09-10T23:39，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:634a3e7432ba48ba42ad8cec73275bb0` |
| 2026-09-24T11:01:26Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:61108a26b09e50a64f89035120873991`（IBM，提案时间 2026-09-10T23:39，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:706aafd360dd05a63567dc7dfb69aacb` |
| 2026-09-24T11:01:27Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:f5b9c494f2e9d0416d210727bba9d946`（DXC，提案时间 2026-09-10T23:39，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:d82b83ecf2e4245070872bc815f89719` |
| 2026-09-24T11:01:27Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:185deb89278638c72ed90ee3400f38e4`（DXC，提案时间 2026-09-11T01:34，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:a588431fb74468def194a8abcaf5e71c` |
| 2026-09-24T11:01:27Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:fc616ffd8b8747d5db60f89c6afaa9a5`（ACN，提案时间 2026-09-11T02:19，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:a44da1650c873eaef24254fb48403796` |
| 2026-09-24T11:01:27Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:933c51ddb0169a76312794fc74d52991`（EPAM，提案时间 2026-09-11T02:19，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:8f4db25477c397dc825d6f890562667c` |
| 2026-09-24T11:01:28Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:6e366e0c7d0bc36de55166a8e2ecfbd4`（IBM，提案时间 2026-09-11T02:19，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:9b2127d290063bcb90bf4b0ae6c1e77d` |
| 2026-09-24T11:01:28Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:1ae694ec3323f2926b28a64a7b0299bf`（DXC，提案时间 2026-09-11T02:19，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:a275ac25831e99d9358092cf8a908ca6` |
| 2026-09-24T11:01:28Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:505126569598c61b5a3cbb899790d585`（ACN，提案时间 2026-09-11T06:57，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:5426a33fa17709a5677e77c582e97dc8` |
| 2026-09-24T11:01:28Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:d843a0725b7ebb975c3842b1dcde46bd`（EPAM，提案时间 2026-09-11T06:57，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:731a785ba4e4a5c9141f119f448dc6bc` |
| 2026-09-24T11:01:28Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:346ecca7c3c263a44dcefcdd9cbff3da`（DXC，提案时间 2026-09-11T11:08，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:9e4cabc336016354ce081850071d7296` |
| 2026-09-24T11:01:29Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:9cf0e9eb35d132c7888a555f08b7c690`（ACN，提案时间 2026-09-11T17:20，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:bb6149784f8cbfa620a4435c470f6dd5` |
| 2026-09-24T11:01:29Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:1f57c9bda7dbe937cda16f7dd0749771`（EPAM，提案时间 2026-09-11T17:20，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:a2772aab9d69e516379b292a43421397` |
| 2026-09-24T11:01:29Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:073d33f2caee1deeb1d8d34ab24f56f4`（IBM，提案时间 2026-09-11T17:20，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:a964809999339acb89cbff64c13b9a07` |
| 2026-09-24T11:01:29Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:ec671f1e4501c15a83ab019f9364ef84`（DXC，提案时间 2026-09-11T17:20，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:b3c5cd3ea6e3ef5b40ad09476232b312` |
| 2026-09-24T11:01:30Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:34d30a0d6df08e168db5f5e5cb6f5f50`（ACN，提案时间 2026-09-11T18:42，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:908cb417bec00cb5655c153bec5a341a` |
| 2026-09-24T11:01:30Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:9af49d9172b7b7c2ccc6815d1d091e79`（EPAM，提案时间 2026-09-11T18:42，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:9b2f6ab74bb08e1c85fdbd8d23dfb803` |
| 2026-09-24T11:01:30Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:dbb16d56373cf35bf8ff031451b8ed4e`（IBM，提案时间 2026-09-11T18:42，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:81d67fade9df899076f17396741d66ca` |
| 2026-09-24T11:01:30Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:372b5ced331d0f2eb22956f545934764`（DXC，提案时间 2026-09-11T18:42，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:1a986654775f276bd92ff0aba51314c5` |
| 2026-09-24T11:01:31Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:e3c6f79044f1b6ac160f4f32e224f969`（ACN，提案时间 2026-09-11T19:30，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:6851b79c449c79bbf4d0e0fdeb78f61d` |
| 2026-09-24T11:01:31Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:4ef6c585c17c823f7ffe5bc39591a572`（EPAM，提案时间 2026-09-11T19:30，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:c7a859d48d1fb8e1000f9b898e8e0f3b` |
| 2026-09-24T11:01:31Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:50865c4620fb3bb05389a432451923ca`（IBM，提案时间 2026-09-11T19:30，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:070d064ec4c717124cd9ccf87f0e555b` |
| 2026-09-24T11:01:31Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:3aa075a77eb964736efc33aa606f0fff`（DXC，提案时间 2026-09-11T19:30，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:ba1b867f557acdaa4bf784dbbc3d3b6d` |
| 2026-09-24T11:01:31Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:658b73b2c527e036558a5da0673dea1e`（ACN，提案时间 2026-09-11T20:30，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:b85358341373605c4929452ed9000c04` |
| 2026-09-24T11:01:32Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:61fee43a9d605781e22904374b6621c1`（EPAM，提案时间 2026-09-11T20:30，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:bcb690b848a9d3dfa519eda4ef6601bd` |
| 2026-09-24T11:01:32Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:b5e02e47960e209b1bac1eeae23ade4c`（IBM，提案时间 2026-09-11T20:30，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:c55d722ad32db12cc49baf07e8a1c7a3` |
| 2026-09-24T11:01:32Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:f7bba188d81721250c918eb3657bf82f`（DXC，提案时间 2026-09-11T20:30，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:ea67152cc5c07b7ebcc145b4f1b5c0ca` |
| 2026-09-24T11:01:32Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:2e0a927c1efdc05aa4bf9db121789d16`（ACN，提案时间 2026-09-11T23:11，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:bd8d3307a0c7011270d11ae21a49689f` |
| 2026-09-24T11:01:33Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:2d1298a9dbb1eb69e0c669633610bc21`（EPAM，提案时间 2026-09-11T23:11，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:95a3328459b4a99e5c926fa49b1d2f27` |
| 2026-09-24T11:01:33Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:750f53a668deea16892c8393a412856f`（IBM，提案时间 2026-09-11T23:11，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:c770d3b6f5d673559c004421a1682f09` |
| 2026-09-24T11:01:33Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:59f8cd7f4c730f8df1433ecefacfbcae`（DXC，提案时间 2026-09-11T23:11，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:cd9f9e65297dd2943f2d6ae34a00df80` |
| 2026-09-24T11:01:33Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:11099e89dd0d9dda3779f4603c8e6749`（ACN，提案时间 2026-09-12T20:42，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:5a2690d41a9cdda13c7276e77c57fe83` |
| 2026-09-24T11:01:34Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:242af1575acbf1ca32464aaeaa1b3411`（EPAM，提案时间 2026-09-12T20:42，针对 `45e773c2`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:56cd3c10e393b15b575913c9ddc0c4e8` |
| 2026-09-24T11:01:34Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:447b658c8c9ff95c730169c67bad5322`（IBM，提案时间 2026-09-12T20:42，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:414f9639da00f661755390a2f36d1bb4` |
| 2026-09-24T11:01:34Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:2cbc4a57511ac153e9a615dfee5f1120`（DXC，提案时间 2026-09-12T20:42，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:5d89c7ca9e67853bfd1ab5345faa4b4a` |
| 2026-09-24T11:01:34Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:a43b71b2617cbb159daf79064e7f570d`（DXC，提案时间 2026-09-12T22:47，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:7eafb34b5e96d573eea705b27887ebe7` |
| 2026-09-24T11:01:35Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:e8577a38153e6906dbd775bf62fb853d`（DXC，提案时间 2026-09-12T23:59，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:150bc2ce0e24f9b45c573ea1a025f6dd` |
| 2026-09-24T11:01:35Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:d65653302cd2dc41137f24f6e7fd1a6f`（ACN，提案时间 2026-09-13T13:46，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:e6207dd9886e1ced4bbcf915d7c64916` |
| 2026-09-24T11:01:35Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:fe34dae6589160b69b0cd56eea1b6c0e`（ACN，提案时间 2026-09-14T09:07，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:52e9ad556c15c5074ee16b8cc15913f5` |
| 2026-09-24T11:01:35Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:4fa6612b2a6717e49f96e83aaa00d3da`（ACN，提案时间 2026-09-14T09:12，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:e1ca3f964c878417caec29298ff4776e` |
| 2026-09-24T11:01:35Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:83d6090eec378ef4d3270f0c1b0196c5`（IBM，提案时间 2026-09-14T09:57，针对 `e0327e68`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:0c49fdc0d8563d3ee9e0fd1ffdccfb1c` |
| 2026-09-24T11:01:36Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:57bba790635f48f88643cbd11311ad80`（DXC，提案时间 2026-09-14T09:57，针对 `cc6d2b18`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:4ec0718a8822aa4ab542b2b325b6800f` |
| 2026-09-24T11:01:36Z | `dalton-gov … --operation decide_gate_reopen` verdict=decline | `gate-reopen-proposal:527443aa7c13eb6a95b9da8dd3af9f48`（ACN，提案时间 2026-09-14T11:27，针对 `06e126b4`） | 目标通过版本已被重发并再次通过，属已被取代的陈旧提案（见下方说明 3） | fresh，`gate-reopen-decision:a26f740adf97db4bb3437a25bacff374` |

写后核验（只读）：legacy `gate_reopen_proposals` 中未决数 47 → 0；`coverage_mission_stage_reopens` 仍为 5 行（decline 不写 reopen 标记，研究门状态未变）；`writer-tokens.json` 里没有残留的 `human:` 临时 principal。

## 说明

### 1. 别名（D9）：未执行
没有正式入口能更新**已存在**的 feed plan：
- `scripts/repair_workspace_lane_parity.py` 的 `mission_plan` 动作只在文件不存在时生成，`_write_json` 从不覆盖已有文件（`workspace_lane_parity.py` 的 `_mission_plan_action`、`apply_parity_actions`）。另外 `--apply` 还会重渲染 LaunchAgent，本次不允许。
- 任务 universe 成员的 schema 是封闭的（`coverage_mission.py:501-517`，只允许 `company_ref/ticker/coverage_tier/bootstrap_priority`），`mission_company_names` docstring 说的"universe 成员带 name"这条路实际上走不通。
- legacy 的计划 `p9-us-it-services-feeds-v2.json` 根本没有 `names` 字段，名称来自代码常量 `document_subject.COMPANY_NAMES`，只能改代码。
- cockpit 和 writer 也没有对应操作。
因此没有手改状态文件。建议补的别名见汇报。

### 2. 在途草稿（D3）：无需操作 / 无正式入口
- ws-7d 那 55 条（现在是 59 条）unclassified 路由决策全部来自 `work:cockpit-debate_map-*`，不是出版批次。争议图每一轮都会重新起草、重新核验，并写入新的路由决策，没有缓存草稿可以"污染"。所以 antigravity/claude 部分会随下一轮自然恢复（10:43Z 已经追加了家族正确的 profile 版本）；muse 的 3 条要等 muse 修好。
- "连续失败 6 次不再重试"只对 legacy 出版 worker 的 UI 文本批次生效（`ui_text_discovery.py:35`）。其中 2 批是 verifier_not_independent（`ui-text-batch:0296977a…`、`ui-text-batch:e9fbe811…`）。代码里唯一的重试方式是手动删除 `results/<digest>.json`，这不是正式入口，所以没有做。另外，删除后大概率会复用 `stages/` 里缓存的同一份草稿和同一条 unclassified 决策，照样失败。

### 3. gate_reopen 提案（47 条）：全部 decline
逐条核对：47 条都是 `initial_screen / evidence_thicker`，所针对的通过版本分别为 IBM v1 `e0327e68`、EPAM v1 `45e773c2`、ACN v2 `06e126b4`、DXC v1 `cc6d2b18`。这四个版本都已由同批另一条提案批准重发，并且重发后再次通过：IBM `bce92d06`（09-17）、EPAM `41ccbecc`（09-14）、ACN `8f38f664`（09-14）、DXC `c49282bd`（09-14）。所引用的证据（statement-ingest、consensus、market-price）已经被那次重发用掉，当前通过版本之后没有新的缺→有翻转，自 09-17 起 reopen 通道也没有再提案。批准有害：`_assert_reopenable` 不检查提案的 `passed_version_ref` 是否仍是当前版本，批准会在没有新证据的情况下把**当前**通过的 screen 重新打开。decline 只写一条决定记录，不改变任何研究状态。

### 4. document-research hold：未执行，列入"下次部署后执行"
- 当前 legacy 有 18 条 held（15 条 escalated `reentry_failed_after_automatic_rebind`），ws-7d 有 6 条 held（5 条 escalated）。
- 授权本身只写一张一次性 grant，但下一次 lane tick 消费 grant 时，子进程会重新跑 `MissionDocumentModelAuthority` 预检，包括家族独立性检查。brain 链含 `profile:auto-muse-cli-gateway-muse-spark-1-3-…`（unclassified），所以现在一定失败：今天 07:58–10:08 legacy 已失败 14 次。现在授权只会浪费 grant 并再次升级为 hold。
- 只读 dry-run 已确认命令可用：`document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0`，15 条都是 `dry_run`。

### 5. DIG：无需操作（未发现代码缺陷）
- DXC 的 `returned_redraft_already_attempted` 不是按时间等，而是按证据指纹等：指纹由 dossier 的 id/hash、争议图版本、问题 hash 算出（`deep_insight_gate.py:648-673`）。DXC 的 dossier 最新版是 v5（09-16），争议图最新版是 v5（09-11），问题和标准都没变，所以没有新证据，也没有正式的强制重写入口。之所以没有新 dossier，是因为 DXC 的 dossier 运行一直因 `MODEL_ROUTE_REJECTED` 失败（最近一次 09-24 08:48，部署前），部署修好家族后，下一次 dossier 发布成功就会自动解锁。
- CTSH 的前提不成立：`companies_at_or_past` 是单调的，CTSH 在 09-14 17:10 有 `gate_passed`（v18），09-07 那条 `gate_failed` 不会把它排除。它是候选，最近一次 DIG 运行（09-24 11:00）就是跑 CTSH，结果被 `returned_redraft_already_attempted` 挡住，原因与 DXC 相同（note 写于 09-18 10:56，晚于 dossier v5 09-18 10:08）。

### 6. 41 条 unreadable claim：无需操作
- 文档是 `cognizant-artificial-intelligence-and-generative-ai-services-peak-matrix-assessment-2025.pdf`（`public-web-document:url-sha256:1575757c…`），渲染器是 `pdf-pypdf-6.17.0:0.1:empty-password:0.1`，与当前一致，并不是"旧 pypdf"。
- 用线上 release 离线重渲染（只读），得到的文本 hash 与 claim 的 `source_content_hash` `1b0a8d7a…` 完全一致。unreadable 的原因是 v1 检测器不读网页渲染文本；41a772dc 的 v2-span 检测器会重新渲染并按 hash 校验，还会把 v1 的 unreadable 当作未审过的 claim 重新审。
- 截至 11:00，v2 已审 4419 条，还剩 4846 条待审。这 41 条会自动变为可读，不需要重新抓取或重新抽取。

## 下次部署（muse-spark 家族修好）后执行

前置检查（只读）：`.venv/bin/python scripts/check_model_family_independence.py`，三个环境的 `document_verifier_preflight.status` 都应为 `passed`，`unclassified_profile_ids` 为空。

```zsh
cd ~/Projects/dalton-research-agent-os
PY=~/.dalton/runtime/releases/<NEW>/venv/bin/python
L="$HOME/Library/Application Support/Dalton/state/dalton-core"
W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core
# legacy：先 dry-run，确认列表后 apply（reentry 门不花钱，cap 0 即可）
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$L" --max-total-cost-usd 0 --apply --actor human:<owner>
# ws-7d
export DALTON_WORKSPACE_MANIFEST=$HOME/.dalton/workspaces/ws-7d894366d1132e2930475a60/workspace.json
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$W" --max-total-cost-usd 0
$PY -m dalton_core.document_recovery_cli authorize-all-escalated --state-dir "$W" --max-total-cost-usd 0 --apply --actor human:<owner>
unset DALTON_WORKSPACE_MANIFEST
# 约 10–20 分钟后复查
$PY -m dalton_core.document_recovery_cli holds --state-dir "$L"
```
