# R24 正式 Demo 发布验收 — 2026-09-13

## 当前状态

R24 已安装完成，安装程序退出码为 0。运行文件与配置已独立核验，真实交互页面、五家公司导出和一次真实 Ask 已通过验收。同一 controller 完成 45/45 持续健康采样，正式 publication 已写入并验证，R24 现为正式发布版本。

## 候选验证

- 冻结 runtime `7025`、OPS `2790`。
- 完整测试共运行 7,949 项，0 failures/errors，1 skip。
- 134 项 OPS 检查通过。
- 608 个运行文件和 17 份模型配置通过核验。
- 14 步复制状态演练与部署前 preflight 通过。
- 安装退出码为 0；安装后独立核验通过。
- 同一 controller 完成 45/45 健康采样，持续 669.670 秒。
- 最终核验与正式 publication 通过。

这些结果证明候选代码、安装产物和配置转换符合本次发布契约。研究内容的完整性与所有高级产品能力仍须分别验收。

## 真实 Demo 验收

交互研究台：<https://everflowdemac-mini.taild2c767.ts.net:8793/>

安装后的第二次真实浏览器验收通过：页面在 3.382 秒完成加载，自然轮询最大并发为 1。五家公司详情、模型与研究产物入口保持可用。

一次新的真实 Ask job 已完成，耗时 62.214 秒，返回 1,790 个字符并携带 57 个引用。约一分钟属于当前完整研究上下文问答的正常等待时间，页面会在后台 job 完成前持续显示处理状态。

服务刚启动并生成第一次投影时，页面可能暂时等待。首次 v1 浏览器检查超时时，后台投影正忙，可能存在资源竞争；无需重启或配置变更，随后 v2 检查恢复并通过。现有证据没有把后台投影认定为唯一原因，该超时历史也保留为失败记录。

五家公司 HTML/XLSX 导出复核通过。导出通过表示现有产品可以读取、生成文件并稳定回放，不表示五家公司的全部研究阶段、模型或投资结论已经完成。

## 演示范围

本次正式 demo 已验证以下流程：

1. 打开研究目标和五家公司进度。
2. 查看公司详情、模型表和现有研究产物。
3. 下载现有 HTML 报告和可用的 Excel 模型。
4. 查看研究日志。
5. 提交一次基于已核实结论的真实问题并取得带引用的回答。

部分研究状态仍为待推进、资料不足、未发布或等待人工判断。Conviction v2、持有期回报归因、买方预期 authority、结构化情景跟踪、真实多工作区最终验收、Guidepoint 与公司 Wiki 后续激活等高级范围仍为 pending。

## 发布证据

| 证据 | SHA-256 / 身份 |
| --- | --- |
| runtime / OPS | `7025` / `2790` |
| 候选 manifest | `a0c694ce03f5da5f4e65f236f511276fa977026bbf3dacdabe0b06cd4edfae57` |
| deployment receipt | `cc00dcd3e340b28fc96c4082cd91437194abbee052a42416a229b0f1fb525032` |
| installed receipt | `b486da8ea3a71db5328e06740e9ea9c726716889a2fbff9c38b5bd8aaebdb711` |
| health receipt | `6d36ec605034018cb09bf09e16e5cc1fb23e5f3e8669830eee4e10098b39706c` |
| finalization receipt | `7cf1a377a28a271f83d43cf5ee77b32a0aab3a2f8454011b1e9b7636ad7b78f9` |
| publication receipt | `7fa8702f032808efb166d929a60f5a736c813aff57939d36ca053de6ea7301db` |
| publication binding | `d525175954377bad56e9965c551b382d26e1711ea5aacc7d92aca15a98816f38` |
| 独立发布回读 receipt | `f4c26f328c79f39718680fd715c9cee9873f14fce3dbcc13b9fffda96ba385ed` |
| current release 指针 | `c8c93c37afe9c332ca30afe021f57e6d4769ea574be506155fa3c15c49f76932` |
| current runtime 指针 | `b967cb781d827903b43f8df5eda3aa464528ad50cbcd985171e668d271647c38` |
| UI v2 receipt | `8d7531db2c48384fe1b81a1983cad00dae9ebd5dbb9c4c4044308ae37aeea978` |
| 启动诊断 receipt | `354cb9998b66418943f440cdd2cff63c40399c3f827c56fa7922708c1df9eb5f` |
| Ask receipt | `6260f6d4b942d27ecf083a1cc48f65329bb953f54c00f2833b911c7014f8126f` |
| 五公司导出 receipt | `604ec5f7fa3cfb617ced2a7e4710d376136f8165db0a4859da2ef46955eddc70` |

完成的复制状态 scratch 仅清理 SQLite 副本和 sidecar；演练 binding、derivation、日志、报告及发布包证据均保留。所有当前发布回滚快照和历史恢复权威均保留。
