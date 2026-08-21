# Gate 0 runtime evidence 债清理

日期：2026-08-21

范围：只补独立验证和 review hygiene；不增加研究能力，不部署 live

## 结果

Gate 0 增加两条显式验证路径：

- `scripts/run_hermetic_research_replay_canary.py` 单独运行既有完整 recorded canary，覆盖
  `recorded SEC Company Facts → policy-authorized formal Claim closure → recorded thesis assessment →
  independent recorded verifier → replay`。它使用注入的 SEC response 和 recorded model completion，不访问网络，
  不调用模型 provider，也不修改 Thesis current pointer。
- `scripts/collect_review_evidence.py` 从 closed JSON manifest 收集文档、实现和命令证据。命令只接受 argv 数组，
  不调用 shell；目标文件缺失、文件或命令证据为空、命令非零退出、超时、UTF-8 错误、路径逃逸、证据超限、输出
  路径已存在或试图覆盖输入时全部非零退出。全部检查通过后才原子写入 Markdown，失败不会发布半份或覆盖陈旧证据。

`.github/workflows/ci.yml` 现在在 Python 3.11、3.13 两个独立 runner 的全量测试之后显式运行 hermetic canary；
Python 3.13 runner 还按 `docs/review-evidence/gate0-review.manifest.json` 生成证据包，并用
`if-no-files-found: error` 上传 GitHub Actions artifact。manifest 至少包含方向/状态文档、CI、canary、collector、
collector 故障注入测试和原 closure-to-impact 实现测试，避免 review 再次收到空 evidence block。

## Fail-closed 验证

`tests/test_review_evidence_collector.py` 覆盖：

- 正常生成非空 document、implementation、command 三类 evidence；
- 文档缺失、文档为空、命令失败、命令输出为空；
- 路径逃逸、覆盖 selected input、已有输出导致的 stale artifact 风险；
- manifest 多余字段和空 category。

实际 Gate 0 manifest 本机已生成非空 Markdown，包含 2 份文档、5 份实现文件和 3 条命令。远端 artifact 会按
exact commit 重新生成并在 collector 结果中记录自己的 SHA-256，不复用本机未提交工作区的 hash。

## 验证状态

- 前一开发 HEAD `b81d1cb`：GitHub Actions run
  [32458335552](https://github.com/everflowinv/dalton-research-agent-os/actions/runs/32458335552) 的 Python 3.11、
  Python 3.13、openclaw-broker 三个 job 全部成功；
- hermetic replay canary：1/1，通过，102.7 秒，recorded-only，模型 provider 调用 0；
- review evidence collector：专项 8/8，通过；
- Python 3.13 全量：581/581，通过，2,342.9 秒；只出现既有 SQLite `ResourceWarning`，没有 failure；
- openclaw broker：16/16，通过；
- `python -m build`、`compileall`、`git diff --check`：通过；
- exact commit 的远端 CI 状态不在 push 前预写；以 GitHub checks 和本轮交付记录为准。

## 未改变

- 未访问新的真实 source，未调用真实或付费模型；
- 未部署 live，未修改 cron、credential、policy 或 authority schema；
- 未增加 thesis updater、自动 thesis revision、并发 worker、fleet control 或 connector；
- Gate 1 的五公司复现和 brief 尚未开始。
