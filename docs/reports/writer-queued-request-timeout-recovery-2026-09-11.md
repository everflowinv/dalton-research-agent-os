# Writer 排队请求超时恢复

2026-09-11，基础运行修复；不改变已开始写入的完成语义。

R7b 正常调度的 `company_dossier` 报 `unavailable:RemoteError`，writer 同期在 `_store_executor` 的 `Future.result(timeout=STORE_REQUEST_TIMEOUT)` 报超时，未出现新 Dossier 子进程结果。这是执行队列可用性问题，不能解释为研究内容拒绝。旧客户端 10 秒、服务端 30 秒 deadline 均未调整。

服务端超时后，旧实现仍保留所有已提交 Future。现在只调用标准 Future.cancel()：尚未开始的请求从队列取消，正在运行的请求保持完成未知语义并允许原执行完成，不强制终止或重试。日志加入经封闭词表验证的 operation 和 queued_cancelled，未加入参数或凭证。

真实 socketpair + 单 store executor 回归覆盖排队写取消、后续请求可用、运行中写超时后仍恰好完成一次；25 项相关测试在作者和独立复核均通过。与档案快照复用等基础改进合并的 184 项检查通过。尚待统一 R8a 冻结验收及部署后的正常队列验证，不能仅以这些测试证明全部操作均小于客户端 deadline。

慢路径之一是 Dossier dispatch 在 store 线程逐候选重建 company source fingerprint，R8 已把逐单元重复快照改为单次操作共享；真实输入重建总耗时 118.56→13.22 秒。两项改动联合减少慢工作和过期队列积压，运行验收仍须看实际调度结果。
