# 所有研究环境的持久公网共享方案

2026-10-10。用户选择：持有分享链接即可查看、编辑所有工作环境；后续再收紧权限。已选用现有 Tailscale 公网地址的独立路径，公网连通性测试通过；共享登录与多项目入口正在实现。

## 已核实的现状

- 工作台只监听本机，由 Tailscale Serve 提供 HTTPS。当前活跃的主环境和 Hyperscaler 环境分别使用8793、8795端口；原8794空环境已删除，新环境由 manager 分配端口。
- 后端通过 Tailscale-User-Login 识别用户，并检查 Host、会话与 CSRF。换成公网转发后，匿名访问不会自动获得编辑权限；仅转发端口不足以跑通。
- 工作环境列表和切换链接目前生成 ts.net 加独立端口的地址。统一公网入口需要同时改变这些地址，不能只共享首页。
- 本机已有 tailscale、cloudflared。Cloudflare 本地未登记命名隧道凭据。现有 Funnel 的443、10000已有其他服务，8443也有私网服务；不能直接覆盖这些入口。

## 建议实现

现有固定 Tailscale 公网地址 + `/dalton/` 路径 + Dalton 公网入口。入口进程监听本机 `127.0.0.1:18973`，由既有 443 端口增加独立路径转发，根路径继续服务原应用。公网入口用可轮换的高熵分享凭据建立会话，持链接者取得用户指定的查看和编辑权限。分享凭据通过链接的片段部分交给浏览器，浏览器建立会话后从地址栏移除；操作记录标为共享访问。

同一站点的 /w/<workspace-slug>/ 路由到已登记的对应环境。环境列表、API、静态资源、导出、证据窗口、管理员入口和创建后跳转均使用这个公开前缀。注册表变化自动更新路由；新建环境可进入分享，暂停保持既有只读规则，归档/删除不路由到已停用服务。一个浏览器同时打开两个环境时，会话与CSRF按环境隔离，避免编辑串到另一项目。

公网入口验证分享会话后以明确的共享访问主体交给后台。外来身份头先清除；后端增加专门的受信入口认证，不把访客伪装成现有Tailscale所有者。已有授权及每个研究任务的预算、内容核验与操作记录继续执行。

入口进程由 launchd 管理，开机恢复、自动重启，Funnel 使用后台持久配置。原有 Tailscale 入口继续可用。当前方案由这台主机提供服务，主机需要保持联网运行。

## 实施所需信息与验收

现有地址已具备公网访问条件，无需新增域名或隧道账号。通过公网 DNS 返回的 IP 强制访问临时测试路径，已确认 Funnel 会移除转发路径前缀：外部 `/dalton-preflight/w/probe` 到达本机时为 `/w/probe`。测试路径已移除，Tailscale 配置与测试前完全一致。正式入口仍待共享会话和多项目功能验收后部署。

验收使用未连接Tailscale的浏览器：打开分享链接、主环境与其它环境切换、双标签页隔离、问答、真实可逆编辑、证据阅读与导出、新环境出现、归档/删除移除、入口重启后同链接可用、分享凭据轮换后旧链接失效。公网配置的具体变更在部署前保存可回退快照。

## 官方依据

- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)：公网发布；支持443、8443、10000，同端口不能同时作为仅私网Serve及Funnel。
- [Cloudflare Tunnel 路由](https://developers.cloudflare.com/tunnel/concepts/routing/)：将公开域名映射到本地服务，一个隧道可承载多个应用。
- [Cloudflare Access 应用](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)：后续可在公网入口增加邀请用户登录。
- [验证 Access JWT](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)：后续接入独立用户身份时，服务端核验签名、受众及签发方。

以上路由、共享主体和工作环境前缀属于结合当前代码提出的实现方案，尚未部署。
