# Collie、Prime、Pi、Hermes 同 Opus transport 实测（2026-08-12）

## 结论

在这轮**冻结、适配、探索性**的微基准中，Prime 与 Hermes 并列第 1，都是
`8/8`；Collie 与 Pi 并列第 3，都是 `5/8`。

| 排名 | Harness arm | 总体 | request ID 传播 | circuit breaker |
| --- | --- | ---: | ---: | ---: |
| 1（并列） | Prime | 8/8（100%） | 4/4 | 4/4 |
| 1（并列） | Hermes | 8/8（100%） | 4/4 | 4/4 |
| 3（并列） | Collie | 5/8（62.5%） | 2/4 | 3/4 |
| 3（并列） | Pi | 5/8（62.5%） | 1/4 | 4/4 |

随后针对 Collie 的结构化响应恢复做了实现与同格回归：新鲜采样为 `7/8`
（87.5%），相对原 Collie 8 格是 `+2 resolved / 0 regressed`。这是只重跑 Collie 的
配对回归，其他 arm 没有同时重跑，因此**不改写上表排名**，也不能把 `7/8` 与原表其他
arm 的点估计拼成一张新排名。

这里的排名标签是
`adapted_harness_same_transport_not_native_product_ranking`。结果明确标记为
`exploratory`、`publishable: false`：它比较的是四套 harness loop 经适配后走同一
Opus subscription transport 的表现，不是各产品默认形态的通用能力榜。

## 测试回答了什么

四个 arm 都通过 evaluator-owned sidecar 直接调用 Claude Agent SDK，不调用
`claude -p`，因此没有带入 Claude Code 的 system prompt。共同冻结项包括：

- 模型 `claude-opus-4-8`，reasoning effort `high`；
- 每个 attempt 最多 12 个物理模型请求、900 秒 wall time；
- 2 个 synthetic Python 代码任务，每个任务每臂重复 4 次，共 32 个正式 attempt；
- 每个 attempt 使用 fresh Git workspace；gold implementation 与 hidden grader 在
  agent 容器外；最终工作区一律交给外置 grader，连 harness 自报
  `product_failure` 的结果也照常评分；
- agent 无 Claude credential、无外网；只有每个 attempt 新建的 sidecar 挂只读订阅
  凭据并拥有 egress；无 host port；
- evaluator 交给 harness 的初始 user message 字节一致，正式出场顺序在每个任务内
  轮换，使每臂各占一次 position 1–4；
- admission 只验证四条适配链都能产生可评分终态，不计分、不参与排名。

最终有效 suite：
`886831826162d197cc345f625a619d80a3e7a0ff3c9d2dadc9d7f17e9f9675ac`。
它绑定到源码提交 `a20f3b35d6449f5ebeb0441fd937d2bfdcb6d52c`，镜像为：

- harness：`sha256:37da581944602b254e47b93fe131a589c424af65140fa719bfb142fab8969324`
- sidecar：`sha256:e84f22f75b9cff5f7b6916b9ff2fcbc05cf97df92a725dc81a890a1b5ca8b0f9`

版本固定为 Collie `0.21.23`、Prime `0.7.2`（镜像内验证 commit
`0987c1ba7637cbcb99afe9efe1180b838a0aa958`）、Pi `0.84.1`、Hermes
`0.15.2`、Claude Agent SDK `0.2.136`。Pi 的版本来自镜像内 package metadata；本报告
不把适配器里记录的源码 commit 当作独立验证过的事实。

## 适配边界

统一的是模型 route、订阅 transport、初始 evaluator issue、请求上限、workspace 和评分
协议，不是完整 model-visible prompt 或工具表面：

| Arm | 本轮 loop / 工具面 |
| --- | --- |
| Collie | Collie 自己的 loop；`read/write/edit/grep/glob`，无 shell；评测 identity 与 force-edit 策略 |
| Prime | Prime 原生主循环与 system prompt；主要工具为 IPython，可经 Python/`%%bash` 操作 workspace |
| Pi | Pi 主循环；`read/bash/edit/write`；使用 `pi-native-coding-minus-self-documentation-v1`，删除了与代码任务无关且会触发 SDK transport 异常的自我文档附录 |
| Hermes | Hermes 原生 system prompt；terminal + file toolsets；关闭 delegation、MCP、memory、skills、browser |

sidecar 将不同 harness 的 system/history/tool schema 桥接为统一的单 JSON
`{tool,args}` / `{answer}` 响应协议。格式与 bridge compliance 因而是本轮被测行为的一部分。
只有最初 evaluator user message 相同；后续 system prompt、history、context policy 和工具
schema 均不同。

## 诊断数据

| Arm | 物理请求 | Input | Output | Cache read | Cache creation | 总体中位时长 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Collie | 53 | 106 | 13,321 | 65,080 | 126,149 | 35.668 s |
| Prime | 40 | 80 | 17,978 | 127,603 | 95,443 | 34.206 s |
| Pi | 35 | 70 | 9,697 | 15,620 | 88,354 | 26.297 s |
| Hermes | 61 | 122 | 11,643 | 41,184 | 408,720 | 60.934 s |

正式轮合计 26/32 resolved、189 个已完成物理模型请求。Collie 另有 1 次超过
12-request budget 后被 sidecar 拒绝的请求；该拒绝没有计入 53 个物理模型调用。

速度与 token 只作诊断，不参与排名。时长包含 Docker 网络、容器启动和清理，不是纯
harness latency；相同 request cap 也不等于相同 token/context budget。各 harness 工具
粒度和成功计数口径不同，例如一次 Prime IPython 调用可以完成多步 shell/Python 工作，
因此 `native_tool_calls` 与 `native_edit_calls` 不能横向比较效率。

Collie 的 3 个未解样本都记录为 `response_contract_error`，Pi 的 3 个未解样本都记录为
`model_or_tool_error`；它们都被保留为有效行为结果，并由 hidden grader 检查最终工作区。
Prime 与 Hermes 的 8 个正式 worker terminal state 全部是 candidate，最终 8/8 通过。

## 订阅与额外计费观察

本轮使用 Claude Max 的现有订阅登录，不允许 API key fallback。正式套件启动前的 UI
观察（`2026-08-12T23:59:26.593Z`）显示：Usage credits 关闭、Auto-reload 关闭、
`$0.00 spent`。运行后（`2026-08-13T00:25:37.161Z`）再次观察，三项状态未变，程序才
释放排名。

测试期间当前 session usage 从 6% 上升到 11%，weekly all-model usage 从 19% 上升到
20%，说明订阅额度确实被消耗。`$0.00 spent` 只能说明 UI 没观察到额外 usage-credit
支出；它不是供应商的 metered billing receipt，也不能被表述为“零成本”或“永不额外
计费”。

## Collie 结构化响应恢复与同格回归

实现提交为 `3c3d4811bc01f56e5539440d9712071d7ea6efd9`。关键行为是：

- provider、直接 SDK adapter 与 subscription sidecar 共用严格的单-envelope parser；
  只接受精确的 `{tool,args}` 或 `{answer}`，拒绝额外键、重复键、未知工具、多个
  envelope、非有限 JSON 数值、过深嵌套，以及完整或损坏 wrapper 内的嵌套指令；
- sidecar 用稳定且不含原始 assistant 正文的 `response_contract_error` 返回 HTTP 422，
  同时保留这次已完成物理请求的四类 usage；
- Collie loop 对这种错误最多发出 1 次无 backoff 的格式修复请求；修复仍走正常的
  request authority、token budget 与 physical-call cap。被拒正文和临时 repair nudge 都不
  写入 session、memory 或最终错误正文；
- normalized worker 将 12-turn 上限同时设为 12 个物理模型请求，避免 final synthesis、
  transport retry 或 repair 产生第 13 次调用；recorder 另存 `contract_repairs`。

回归复用了原 suite 的 2 个冻结 task × 4 个 Collie repetition，并逐格绑定历史
Collie cell；使用同一 evaluator prompt、hidden grader、Claude Agent SDK subscription
transport、12-request cap 和 fresh workspace。结果如下：

| 采样 | 总体 | request ID 传播 | circuit breaker | 配对变化 |
| --- | ---: | ---: | ---: | ---: |
| 原 Collie baseline | 5/8（62.5%） | 2/4 | 3/4 | — |
| 修复后 Collie-only regression | 7/8（87.5%） | 3/4 | 4/4 | +2 / −0 |

新 suite 为
`cf4530576464afd1272aeed89dc658753a86a35922654a4584866a4fb4fa6441`，共 65 个已
预留并结算的物理请求，所有 attempt 都未超过 12。证据校验覆盖四类 usage parity、
reserved/settled、patch、冻结 task/fixture/hidden-grader hash、grader success marker 和
额外第 9 个 artifact；最终 `validation_errors: []`、`regression_evidence_complete: true`。
该 suite 始终写入 `ranking: null`、`publishable: false`。

格式恢复在 2 个 attempt 中实际触发，共出现 3 个 contract-error settlement：

- circuit-breaker 第 4 次在一次 contract error 后修复成功，最终 resolved；
- request-ID 第 4 次在修复请求上再次违反 contract，按一次上限停止，最终没有 patch、
  unresolved。说明恢复链已真实生效且有界，但不能保证每次格式失败都能收敛。

跑前 UI 观察时间为 `2026-08-13T04:15:14.562249Z`，跑后为
`2026-08-13T04:21:49.759155Z`：Current session 从 0% 到 2%，weekly all-model 保持
20%，Usage credits 与 Auto-reload 均关闭，额外用量仍为 `$0.00 spent`。这仍只是一项
UI 观察，不是供应商账单保证。

## 对 Collie 的直接判断

1. **结构化响应恢复的第一版已经落地并通过同格回归。** 点估计从 5/8 到 7/8，且一次
   contract failure 被修复后成功解题；连续两次不合约时也按上限停止。下一步应在更大
   hidden task 集上验证改善是否稳定，而不是继续从这 8 个新鲜样本外推。
2. **多文件传播任务仍是下一优先级。** 原排名中 request-ID 只有 2/4，修复后单臂回归
   是 3/4，仍低于 Prime/Hermes 原采样的 4/4；需要强化跨文件影响面搜索、修改后验证
   以及 budget 即将耗尽时的收敛策略。
3. **可借鉴 Prime 的通用执行面与 Hermes 的稳定终态。** 这不等于照搬 shell 权限，
   而是让 Collie 在受控沙箱里拥有可组合的验证动作，并保证工具结果、错误与最终状态
   始终能被 loop 消化为下一步。
4. **overnight 能力仍需单独建设和测试。** 本轮最长只允许 900 秒，完全没有验证
   checkpoint/resume、daemon crash recovery、心跳/lease、上下文压缩、配额窗口切换、
   幂等重试或 8–12 小时自治。要达到 Codex 大任务跑一晚的使用预期，需要另建 endurance
   suite，而不是从本轮 8 个短样本外推。
5. **扩大任务面再决定产品优先级。** 两个任务不是八个独立问题；每个 arm 的一次波动
   就是 12.5 个百分点，而且适配器已经用这些题做过调试。下一轮至少应加入更多未见过的
   repo-level 任务、故障注入和长时恢复场景。

## 限制与不可声称的内容

- 不能称为 Prime、Pi、Hermes 或 Collie 默认/原生产品排名；后三者在这里也没有获得
  “原生 subscription integration”，而是接到 evaluator-owned compatibility sidecar。
- 不能声称 system prompt、完整 prompt、context、tool surface、loop policy 或 token
  budget 相同，也不能把差异归因给模型；四臂使用的是同一个模型。
- 不能把这两个 synthetic task 外推为通用 SWE 能力、统计显著性或 overnight
  reliability。
- 带 shell/IPython 的 arm 理论上可直接访问内部 sidecar；ledger 能证明请求与 usage，
  但不是对抗性环境下的逐请求 harness provenance 证明。
- 本 suite 的 manifest 对 `harness/swe.py` 记录的是 Windows 混合换行 worktree SHA，
  而提交 blob 为 LF、镜像内文件为 CRLF；三者移除 CR 后内容逐字节一致，Python 语义
  相同，且 exact image digest 锁定了实际运行时，因此不影响计分。但该单项
  `source_sha256` 不能被当作 commit/image 的逐字节证明。runner 已在 suite 封存后改为
  哈希 `git archive` 的实际构建输入，供未来测试使用。
- admission 顺序固定，且只跑第一题；它仅是基础设施 gate。正式轮在每题内部轮换顺序，
  但第一题整体先于第二题。
- 所有结果均 `publishable: false`；如需公开排名，应扩充任务集、预注册协议并用全新
  hidden tasks 重跑。
- 修复后的 7/8 是只重跑 Collie 的新鲜随机采样，未同时重跑 Prime、Pi、Hermes，不能
  更新跨 harness 排名，也不足以证明统计显著的因果提升。
- Collie-only regression runner 本身不支持断点续跑；本次受监控运行完整结束，但若
  中途进程崩溃，必须人工审计已消费格，不能用新 suite 直接从第 1 格盲目重跑。

## 证据文件

Git 中提交的是不含解法正文的最小证据集：

- `bench/results/normalized-harness-v1-886831826162/manifest.json`
- `bench/results/normalized-harness-v1-886831826162/summary.json`
- `bench/results/normalized-harness-v1-886831826162/post-run-billing.json`
- 同目录 36 个 `runs/*/result.json`

每个 `result.json` 已内嵌 grader 摘要，并绑定 reservation、patch、ledger、task、prompt
与 suite SHA。完整本地证据包另含 reservation、grader、worker、patch 和逐请求 ledger，
共 625 个文件、443,570 bytes；敏感性扫描未发现 Claude credential、Bearer/OAuth token
或原始 evaluator prompt。它不提交 Git，因为 `patch.diff` 与 `worker.json` 会公开 benchmark
解法，而 442 个 ledger shard 会制造无必要的小文件噪声。旧 suite（包括被主动中止的
`78dda328c4e4…`）均未拼入最终结果。

修复后的最小回归证据集位于：

- `bench/results/normalized-collie-regression-v1-cf4530576464/manifest.json`
- `bench/results/normalized-collie-regression-v1-cf4530576464/summary.json`
- `bench/results/normalized-collie-regression-v1-cf4530576464/post-run-billing.json`
- 同目录 8 个 `runs/*/result.json`

完整本地目录还保留逐请求 ledger、patch、grader、worker 与 reservation；提交到 Git 的仍
只是没有解法正文与 prompt 正文的最小证据。
