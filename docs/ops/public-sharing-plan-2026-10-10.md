# 所有研究环境的持久公网共享方案

2026-10-10。用户选择：持有分享链接即可查看、编辑所有工作环境；后续再收紧权限。本轮先完成研究系统修复与发布。公网路由尚未更改。

## 已核实的现状

- 工作台只监听本机，由 Tailscale Serve 提供 HTTPS。当前活跃的主环境和 Hyperscaler 环境分别使用8793、8795端口；原8794空环境已删除，新环境由 manager 分配端口。
- 后端通过 Tailscale-User-Login 识别用户，并检查 Host、会话与 CSRF。换成公网转发后，匿名访问不会自动获得编辑权限；仅转发端口不足以跑通。
- 工作环境列表和切换链接目前生成 ts.net 加独立端口的地址。统一公网入口需要同时改变这些地址，不能只共享首页。
- 本机已有 tailscale、cloudflared。Cloudflare 本地未登记命名隧道凭据。现有 Funnel 的443、10000已有其他服务，8443也有私网服务；不能直接覆盖这些入口。

## 建议实现

固定域名 + 命名 Cloudflare Tunnel + Dalton 公网入口。公网入口用可轮换的高熵分享凭据建立会话，持链接者取得用户指定的查看和编辑权限。分享凭据不进入应用日志或研究证据，也不作为某个已登录个人的身份；操作记录标为共享访问。

同一站点的 /w/<workspace-slug>/ 路由到已登记的对应环境。环境列表、API、静态资源、导出、证据窗口、管理员入口和创建后跳转均使用这个公开前缀。注册表变化自动更新路由；新建环境可进入分享，暂停保持既有只读规则，归档/删除不路由到已停用服务。一个浏览器同时打开两个环境时，会话与CSRF按环境隔离，避免编辑串到另一项目。

公网入口验证分享会话后以明确的共享访问主体交给后台。外来身份头先清除；后端增加专门的受信入口认证，不把访客伪装成现有Tailscale所有者。已有授权及每个研究任务的预算、内容核验与操作记录继续执行。

cloudflared 和入口进程由 launchd 持久管理，开机恢复、自动重启；不采用每次启动地址变化的临时隧道。原有 Tailscale 入口继续可用。持久地址不代表主机离线时仍能服务；若需要离线仍可读写，后续迁到持续在线服务器。

## 实施所需信息与验收

需要选定域名及对应DNS/隧道账号授权。若优先沿用现有ts.net，可在既有443增加独立/dalton/路径，再转发到同一公网入口；需要实测该路径的前缀行为，并保留当前根路径服务。它同样需要多环境路径和分享会话实现。

验收使用未连接Tailscale的浏览器：打开分享链接、主环境与其它环境切换、双标签页隔离、问答、真实可逆编辑、证据阅读与导出、新环境出现、归档/删除移除、入口重启后同链接可用、分享凭据轮换后旧链接失效。公网配置的具体变更在部署前保存可回退快照。

## 官方依据

- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)：公网发布；支持443、8443、10000，同端口不能同时作为仅私网Serve及Funnel。
- [Cloudflare Tunnel 路由](https://developers.cloudflare.com/tunnel/concepts/routing/)：将公开域名映射到本地服务，一个隧道可承载多个应用。
- [Cloudflare Access 应用](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)：后续可在公网入口增加邀请用户登录。
- [验证 Access JWT](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)：后续接入独立用户身份时，服务端核验签名、受众及签发方。

以上路由、共享主体和工作环境前缀属于结合当前代码提出的实现方案，尚未部署。
