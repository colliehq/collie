# Collie 产品与可操作性审计 — 2026-08-25

## 我对产品目标的理解

Collie 不是另一个聊天壳，也不应等同于某个模型。它是本机长期运行的个人 AI 执行
控制面：用户给出结果，Collie 选择 brain、worker、工具、设备和执行方式，把工作放进
可恢复的 Mission，在真实环境中推进，并用 Leash、Needs You、独立验证和 Receipt 对
权限、成本与结果负责。

面向用户的主语始终是一个 Collie；Pack 是它下面可调度的 worker/device 集合。外部
coding CLI 因而不是第二套产品，而是 Pack 中受 Collie 预算、审计和恢复边界约束的执行
worker。Desktop、Web、CLI、IDE、手机也不是各自拥有状态的产品分叉，而是同一份持久化
Mission/Activity/Receipt 的不同控制面。

## 本轮发现的主要断点与完成情况

| 边界 | 原断点 | 当前行为 |
|---|---|---|
| Worker 选择 | external runner 主要停留在 registry/CLI，Web、Mission、Pack 语义不一致 | `collie`、`codex-exec`、`claude-code` 可由同一 Run Plan 解析；CLI、Web、Pack、普通代码 Mission 都记录冻结后的 worker/billing 路由 |
| Billing/额度 | 登录状态容易被误读成免费；Auto 缺少 plan headroom | billing class 由证据决定；`no_paid_overage` 对 unknown fail-closed；Codex 使用只读 app-server quota snapshot，Auto 将能力、历史、冷却和 headroom 一起排序 |
| External 流式协议 | CLI 文本可能被当成自由格式日志，terminal/usage 不完整仍可能结算 | JSONL/stream-json 有大小、深度、事件数、标准 JSON、唯一 terminal、有限 usage 和最终帧约束；完整原生事件可流到 Web |
| Steering/approval | UI 容易暗示所有 worker 都支持 in-flight steer/approval | capability matrix 驱动控件；phase-one Codex/Claude CLI 明确显示不支持 live steer/Collie approval round-trip，并禁用误导性操作 |
| 外部副作用恢复 | worker/copy-back 与 receipt 之间存在崩溃窗口 | CLI、Web、Pack 使用 durable `external_action` fence；Mission 使用 pre-edit baseline、ownership WAL、slice receipt；不确定执行进入 recovery，不盲重试 |
| Pack | 候选隔离、胜者应用、清理失败、usage unknown 的边界不完整 | 每个候选独立 worktree；host verifier 选胜者；可选择 apply；partial apply/orphan/cleanup failure 可恢复；预算无法度量时停止继续启动候选 |
| Mission | external worker、session resume、累计预算和 TaskTree specialist 未完全贯通 | frozen worker profile、native session locator、逐 slice metering、父子累计预算、specialist 调度/消息/取消/结果折叠均进入持久化状态机 |
| UI | Run Setup 没有充分说明实际执行位置、计费和 Pack 边界；多语言不完整 | Desktop/phone 显示 worker、billing、quota/reset、workspace、隔离方式和限制；Run Plan 在执行前可见；新增中/英及既有 locale 的动态状态文案 |
| Authority JSON | 多处 Python JSON 默认接受 `NaN`，字符串 `"false"` 也可能被当成授权 | Web、Action、Automation、Mission、TaskTree、Job、browser bridge、tool RPC、sidecar、supervisor、remote 等关键入口统一使用标准 JSON和精确布尔/数值类型 |
| 24×7 恢复 | 坏记录可能被空对象替代、锁住事务、启动本应禁用的 worker，或重建 remote identity | 坏 authority 进入 recovery/dead-letter；异常事务显式 rollback；supervisor 时序/开关严格；remote identity 损坏时保留原文件，不旋转 room/key |

## 当前可操作性矩阵

| 能力 | Collie native | Codex exec | Claude Code |
|---|---:|---:|---:|
| CLI 单次代码执行 | 是 | 是 | 是 |
| Web Run Plan + 实时事件 | 是 | 是 | 是 |
| Pack 多候选隔离/验证/可选应用 | 是 | 是 | 是 |
| 普通持久化代码 Mission slice | 是 | 是 | 是 |
| native session 续跑 | 是 | 是 | 是 |
| turn 内实时 steering | 是 | 否；下一次 resume 生效 | 否；下一次 resume 生效 |
| Collie approval 往 worker round-trip | 是 | 否，worker 内审批被拒绝 | 否，且无 shell |
| token usage | 是 | 是 | 是 |
| 精确 marginal cost | provider 可提供时 | 未提供，按 billing evidence 处理 | CLI 提供时 |
| plan quota snapshot | 当前无统一来源 | 是，只读探测 | 当前无可靠标准接口 |
| Overnight native agent loop / specialist | 是 | 暂不作为 phase-one external CLI 路径 | 暂不作为 phase-one external CLI 路径 |

“否”不是静默降级：控制面显示限制并禁止对应按钮。需要这些能力的 Mission 会留在
Collie native lane，而不是把一个不支持的 worker 包装成支持。

## 安全与恢复不变量

1. 任何 route 在启动前完成 billing、quota、workspace 和 capability admission；unknown
   不能在硬成本规则下被 Auto 当作可用兜底。
2. 任何会扩大权限的 durable JSON 必须是对象/规定集合，布尔值必须是 JSON boolean，
   预算和 usage 必须有限、非负，整数额度不能截断小数。
3. 批准绑定 exact payload digest；批准时再次校验 durable HMAC；损坏的已批准 action
   在副作用之前终态拒绝。
4. external worker 的进程退出不是成功证据。协议 terminal、usage、receipt、workspace
   ownership 和 host verification 必须共同收敛。
5. 已发生但未结算的操作可以重放 receipt，不能重放副作用；未知是否发生的操作进入
   recovery，需要检查后 reconcile。
6. 子 Mission/TaskTree specialist 只能缩小父 leash/resources；子 usage 计入每个祖先；
   损坏的祖先 authority 阻止后代 claim。
7. relay、浏览器 extension、child worker、provider 输出和本地 HTTP 都是协议边界，均有
   大小限制、严格 JSON、超时/取消和内容清洗。

## 仍然存在的明确产品缺口

这些是需要新的上游能力或下一阶段设计的项目，不应靠伪装 capability 来“补齐”：

1. **External CLI 的 turn 内 steering/approval。** 当前 Codex/Claude 非交互 CLI 没有与
   Collie gate 等价的双向通道。下一步应接官方长会话/App Server 协议，并为 approval
   request 建立 payload-bound round trip；在此之前 UI 继续 fail-closed。
2. **Claude plan headroom 的可靠只读信号。** 目前没有与 Codex
   `account/rateLimits/read` 同等、稳定、可归属账户的接口。不能用本地缓存或登录成功
   推测剩余额度。
3. **更多 worker adapter。** Prime、Pi、Hermes 等需要各自冻结版本、能力声明、billing
   attestation、协议 conformance 和真实兼容报告后，才能进入 Auto pool。
4. **长时间 soak 与真实崩溃矩阵。** 单元/集成/真实 socket/browser 门禁覆盖了确定性
   崩溃窗口，但仍应做 8–12 小时 daemon soak、sleep/resume、磁盘满、断网、CLI 被杀、
   worktree 部分应用和 relay 重连的组合测试。
5. **真实账户的 opt-in 兼容报告。** 本轮刻意不发模型 prompt、不产生付费调用。发布前
   应由用户显式运行 `collie runners compat --live --report ...`，将具体 CLI 版本、账户
   billing evidence 和协议结果写入报告；测试不能替代供应商现场行为。
6. **Activity 的恢复工作台。** recovery/dead-letter 已经是诚实状态，但还可增加统一的
   diff/receipt/ownership 检查器和“确认未发生 / 已发生 / 接管”向导，减少人工查 SQLite
   或 worktree 的需要。

## 验证方式

本轮测试坚持不调用真实付费模型：external CLI 只做 `--version`、登录/额度等只读元数据
探测；业务路径使用 fake process、stub provider、临时 SQLite/worktree、真实 loopback
HTTP/WebSocket 以及本地浏览器 UI。最终交付前执行：

```text
python -m pytest -q
tests/run_all.sh
git diff --check
```

`run_all.sh` 除 pytest 外还覆盖 CLI smoke、GUI/static UI、真实 socket、E2E crypto、remote
protocol、steering UI 和 release hygiene。测试数字以本次交付消息中的最终一轮为准。

## 2026-08-26 安装态复核

本轮继续从源码、发布包和真实安装三个层面检查，确认并修复了四个可复现断点：

1. Desktop 的 Run Setup 在 Pack 展开 composer 后会覆盖 Send；现在根据 composer 的真实
   边界动态定位，390px、320px 和 phone drawer 也都有交互回归。
2. quiet UI 的后置 CSS 会覆盖窄屏单列布局，造成横向滚动；现在 900px 以下明确恢复单列。
3. Dense recall 会把旧 embedding model 的向量与当前模型比较；现在 dense 只在同一向量
   空间检索，旧行仍保留在 BM25 中等待显式 re-embed。
4. 另一开发 track 留下的 memory delta-sync SQLite triggers 会在重启时早于连接函数注册而
   中断启动；现在迁移前检测并注册稳定 ABI，并在真实记忆库的只读快照上验证了 reopen、
   migration 和 write trigger。

最终证据：Python `1985 passed, 7 skipped`；GUI `59/59`；CLI surfaces `41/41`；真实本地
browser bridge `37/37`；安装后 mock selftest `3/3`。Wheel、sdist、127.6 MB Windows
installer、严格文档构建、依赖一致性和私密浏览器资产排除检查均通过。0.21.30 已覆盖安装，
升级前后的 settings SHA-256 完全一致；Web、jobs、automations、browser bridge、Slack 和
Wallpaper 进程已恢复。当前 `needs_you` 来自一条既有 `recovery_required` Mission，不是
服务故障，也未在审计中擅自清除。
