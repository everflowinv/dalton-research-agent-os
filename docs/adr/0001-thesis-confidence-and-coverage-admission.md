# ADR-0001：Thesis 置信度与首条覆盖准入权限

- 状态：Accepted
- 日期：2026-08-23
- 适用范围：Dalton Core ThesisVersion、行业 Driver Pack、首条公司覆盖准入

## 背景

`ThesisVersion` v0.1 使用 0..1 浮点置信度，但系统没有足够历史样本把该数字校准成概率。继续写 0.6、0.7
会制造不存在的精度。同时，首条 coverage thesis 不能由旧底稿、worker 或模型直接写入 Core；它必须绑定当前
投资 mandate、行业研究方法和明确的人类决定。

## 决定

1. `ThesisVersion` v0.2 的 `confidence` 只接受 `low / medium / high`。v0.1 浮点版本保持只读兼容，不能作为
   新 admission 的输出。
2. v0.2 使用显式 authority union：
   - `human_admission`：首条 coverage thesis 的唯一准入权限；
   - `verification`：保留既有 verified-change 链的兼容性，但不能创建或修改已经由 coverage candidate
     保留的 thesis。
3. 初始 coverage admission 必须同时绑定：
   - active、有效期内的 `MandateVersion` 及其 hash；
   - active `IndustryDriverPackVersion` 及其 hash；
   - pack 内存在的 thesis template、driver refs 和 falsifier refs；
   - 一个不可变 candidate 和一个不可变 human decision。
4. 只有 `human:` principal 可以注册 Driver Pack、提交 admission candidate 或作出 admission decision。writer
   从认证 principal 注入 actor，拒绝 caller 冒充。
5. candidate 一旦保留 `thesis_ref`，旧的 model-verification `commit` 路径必须 fail closed。admit 只生成一个
   append-only ThesisVersion 和 current pointer；reject 不写 ThesisVersion。重复请求按 exact request hash 返回
   duplicate，不重复建版本。
6. 本 ADR 只允许初始准入。coverage thesis 的自动 revision 继续禁止；未来若要更新，必须另行定义 human-reviewed
   revision contract、回滚和 current-pointer 规则。

## 迁移

- 旧 `thesis_versions` 表原位重建，保留 version id、canonical JSON、change/verification binding 和现有引用；旧行
  补成 `authority_kind=verification`、`authority_ref=verification_id`。
- 新表允许 human admission 行的 `change_id`、`verification_id` 为 null，并要求
  `admission_decision_id=authority_ref`。
- `ThesisVersion.from_dict` 同时读取 v0.1 和 v0.2；新 stage/commit payload 只接受 ordinal confidence。

## 影响

- 研究人员能用 ordinal 表达当前判断，但不能把它当作概率。
- 行业差异进入 Driver Pack，Core 继续只负责版本、权限、绑定和重放。
- 旧模型验证测试和历史数据仍能重放；coverage-governed thesis 不会被这条路径绕过。
- 本决定不激活 ACN live mapping，不授权付费模型调用，也不允许自动 thesis 更新。
