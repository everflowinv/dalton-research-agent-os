# Phase 1 Agenda HTML 控制面实施报告

日期：2026-08-14

## 结论

Agenda 审批已从 Discord reaction 迁到 owner-only HTML 控制面。Discord 以后只发送通知和 durable
delivery receipt；操作入口是 Tailscale Serve 内网地址。已送达 decision 超过 24 小时没有显式人工反馈时，
系统会写入独立的 `auto_accept_timeout` 记录。它表示有效默认接受，但不算人工同意，也不进入人工认可率。

首张万华卡的 Discord ✅ 已于 2026-08-14 11:19:33 UTC 入账，公开看板显示 1 个有人工标签的 decision、
人工认可率 100%、超时默认接受 0。

## 安全边界

- control backend 只监听 `127.0.0.1:8793`，LAN 和公网不能直连。
- Tailscale Serve 在 tailnet 内提供 HTTPS，并在转发前移除伪造身份头，再注入 `Tailscale-User-Login`。
- backend 只接受 owner allowlist 中的登录名，并校验内存 session、`Secure; HttpOnly; SameSite=Strict`
  cookie 和每个 session 的 CSRF token。
- 数据库不保存 Tailscale 登录名，只保存 SHA-256 派生的 `human:tailscale-*` subject。
- `dashboard-control` principal 只能列出 Agenda feedback target 和写 feedback。
- `agenda-timeout` principal 权限相同，但 writer 强制 subject 为 `automation:timeout`、source 为
  `auto_accept_timeout`、verdict 为 `agree`。
- 旧 `feedback-bridge` token 已撤销。OpenClaw bridge 配置中的 Discord feedback allowlist 为空。
- 公开 COS 看板继续只读，不含 writer token、登录名、CSRF、feedback subject 或控制 API。

## 超时语义

- deadline 从 durable delivered event 的时间开始计算，当前为 86,400 秒。
- 任何 `human:*` 显式反馈都会阻止 timeout processor 写默认接受。
- timeout record 是 append-only；重复 sweep 幂等，不会生成多条记录。
- 如果人工反馈晚于 timeout，历史默认接受保留，但有效状态改为人工结果。
- `labeled_decisions`、`agree_count`、`disagree_count` 和 `agreement_rate` 只统计 human subject。
  `auto_accepted_decisions` 与 `auto_accept_count` 单独展示。

## Live 验收

- `dalton.writer`、`dalton.controller`、`dalton.control` 三个 LaunchAgent 均为 running。
- Tailscale Serve 已把 `https://everflowdemac-mini.taild2c767.ts.net:8793/` 代理到 loopback control service。
- 经 Tailscale Serve 的 HTML 与 `/v1/agenda` 请求成功，identity allowlist 生效。Mac mini 本机因
  Tailscale CLI `1.98.8` 与 daemon `1.96.4` 版本不一致，系统 resolver 无法解析自己的 MagicDNS
  名称；本机验收使用显式地址映射通过 Serve 路径完成。
- 错误 CSRF 的 POST 返回 403；Authority 中 feedback 数仍为 1，没有误写。
- public dashboard 显示 `labeled_decisions=1`、`agreement_rate=1.0`、`auto_accepted_decisions=0`。
- OpenClaw outbox 最近一轮 checked/recorded Discord feedback 均为 0。
- Python 195/195；OpenClaw model broker 15/15。
- 部署前快照 `pre-agenda-control-20260814T1135Z` 已建立。

## 当前边界

- control URL 只在已登录同一 tailnet 的设备上可用，不提供公网 fallback。
- 尚未从另一台 tailnet 设备实测页面；Mac mini 本机的 Tailscale client/daemon 版本差异需要单独维护。
- 24 小时是当前 owner 指定方向下的可配置值，不是旧 cron cutover 或研究执行授权。
- 默认接受只影响 Agenda shadow 标签；它不会启动 research worker，也不会写 Evidence、Claim、Thesis。
- 扩到 3 家仍要求 10 个工作日和至少 20 个显式人工标签。timeout 标签不能凑足这 20 个。
