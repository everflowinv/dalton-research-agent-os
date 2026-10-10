# 公网共享网关部署

本功能为用户明确授权的“持链接者可查看和编辑全部研究项目”。共享访问使用单独的共享主体，记录为 `human:shared-…`，不会冒充原有负责人。现有 writer 权限、预算、模型路由、暂停规则和审计仍生效。共享主体可以使用管理员设置及新建项目；持有者能改变研究与运行设置，应只向预期的协作者发出链接。

## 路由和配置

公网入口为 `https://<当前 ts.net 主机>/dalton/`，项目入口为 `/dalton/w/<slug>/`。已验证 Tailscale Funnel 路径代理会去掉 `/dalton/`：网关收到 `/w/<slug>/`。不要再配置第二次去前缀。网关只监听 `127.0.0.1:18973`，不替换现有 443 根路径代理，也不改变 8443 私网服务。

准备目录（0700）和两个不同的高熵 URL-safe 密钥文件（0600）：`share-secret` 用于公开分享链接，`backend-secret` 只用于网关与本地 control。用 `secrets.token_urlsafe(48)` 分别生成；密钥不写进 launchd 参数、日志、Git 或环境变量。网关配置必须是当前用户所有的 0600 普通文件：

```json
{
  "host": "127.0.0.1",
  "port": 18973,
  "public_origin": "https://<当前 ts.net 主机>",
  "manager_config": "/绝对路径/workspace-manager.json",
  "legacy_service_config": "/绝对路径/legacy/config/service.json",
  "share_secret_file": "/绝对路径/private-sharing/share-secret",
  "backend_secret_file": "/绝对路径/private-sharing/backend-secret",
  "principal": "public-collaborators",
  "session_seconds": 86400
}
```

在已有各项目 `service.json` 的 `control.config` 与 manager 配置顶层加入完全一致的配置：

```json
"shared_gateway": {
  "secret_file": "/绝对路径/private-sharing/backend-secret",
  "public_origin": "https://<当前 ts.net 主机>",
  "principal": "public-collaborators"
}
```

这里只增加共享授权；不要用旧配置整体覆盖当前配置。特别保留最新输出上限、模型顺序、预算、周报目标及 manifest 绑定。manager 创建的新项目会继承此配置，Tailscale 登录名单仍保留真实原负责人。删除或归档项目不会有公网路由；暂停项目通过现有强只读研究接口查看保存的正文，不能代理写入暂停服务。若没有活跃服务可承接只读接口，会明确暂不可用。

## 启动与验证

用当前正式安装解释器 `env -u PYTHONPATH "$RELEASE_VENV/bin/python" -I -m dalton_core.public_sharing_gateway --config "$GATEWAY_CONFIG"` 先在本机启动。launchd 模板为 `deploy/macos/launchagents/com.dalton.public-sharing-gateway.plist.template`，替换安装解释器、配置和日志目录占位符，渲染时按 XML 转义。只有网关和后端 control 配置均校验通过后才挂载公网路径。

由部署操作者执行既有 Tailscale 路径操作：

```sh
tailscale funnel --bg --https=443 --set-path=/dalton/ http://127.0.0.1:18973
```

执行前后保存 `tailscale serve status --json`，确认根路径和 8443 配置逐字不变。没有 Tailscale 身份的外部浏览器打开私有保存的 `https://<主机>/dalton/#share=<share-secret>`。fragment 会在页面启动时立即从地址栏去除，再经同源 bootstrap POST 换取 Secure、HttpOnly、SameSite=Strict 会话，不进入 HTTP URL/Referer/access log；网关不记录请求路径或正文。不要把完整分享链接放进公开文档或截图。

验证：项目列表、两个标签独立编辑 CSRF、管理员链接、静态模块、原文证据与导出；暂停只读、归档/删除无路由、新项目自动出现；外来身份头、错误 Host/Origin、错误 CSRF、编码越路径和外部重定向均拒绝。真实编辑操作使用隔离项目；网络测试不应产生研究模型调用。最后用正式页面确认所做业务修改有实际 writer 回执，而不是只看到 HTTP 200。

## 轮换和撤销

替换 `share-secret` 文件即可使已有公网会话立即失效，并发出新的完整分享链接。替换 `backend-secret` 时网关与各 control/manager 仍引用相同路径，保留身份配置；密钥读取失败会拒绝共享身份。去掉 `/dalton/` 的 Funnel 路径或卸载专用网关可撤销公网入口，原私网入口继续工作。不要移除其他 Funnel handler。

公开链接本身不提供逐人身份归属；共享审计标明共享主体，既有原负责人、人工判断及历史版本不会被改名。
