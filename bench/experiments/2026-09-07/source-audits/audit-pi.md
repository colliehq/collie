已完成对两套代码的静态审计（只读，未执行任何命令、未访问网络、未读取凭据/会话/评分器目录）。以下为报告。

---

# Collie × pi harness 深度对照审计（面向 Claude Opus 5 高并发基准）

## 0. 审计对象与实际形态

| | Collie | pi |
|---|---|---|
| 实际源码 | `C:\workspace\collie\harness\*.py`（Python，stdlib-only 核心，`pyproject.toml:16`） | `references\pi\packages\{agent,ai,coding-agent}`（TS monorepo） |
| 执行循环 | 单文件命令式循环 `harness/loop.py`（3272 行），`Harness._run` 为唯一执行体 | **双层**：`packages/agent/src/agent-loop.ts`（经典循环）+ `packages/agent/src/harness/runtime/drive.ts`（持久化状态机） |
| 已存在的互操作 | `harness/pi_rpc_runner.py`（**Collie 已内置 pi 传输**） | `packages/coding-agent/src/modes/rpc/`（原生 RPC 协议） |

关键结构性事实：**pi 有两个可被基准调用的执行层**。`agent-loop.ts:156 runLoop` 是与 Collie `loop.py:_run` 同量级的对等物；而 `harness/runtime/drive.ts:29 driveOperation` 是一个以 `state.at` 为判别式的持久化状态机（`starting / checkpoint / assistant.ready / assistant.effect_pending / tools / deferred.* / summary.* / navigation.ready_to_commit`），Collie 没有对等物 —— Collie 的等价语义散布在 `loop.py` 的 `journal_state` 字符串变量（`loop.py:1667, 1879, 2297, 2413`）中。这是**最重要的可判别维度**：pi 的恢复点是被 reduce 的状态，Collie 的恢复点是被写入 JSON 的字符串。

---

## 1. 逐条执行路径对照（源码级）

### 1.1 工具结果与截断

两侧对同一问题（`stopReason === "length"` 时工具参数可能被静默截断）做了**语义相同**的处理，这是难得的对齐点：

- Collie：`loop.py:2105-2137`。全批失败，写入 `TRUNC_MSG`（`loop.py:37-39`），并在 `loop.py:2124-2129` **把 `provider.max_tokens` 翻倍**（上限 32768），`trunc_rounds >= 3` 退出。
- pi：`agent-loop.ts:230-233` → `failToolCallsFromTruncatedMessage`（`agent-loop.ts:379-404`）；持久层同一判断在 `drive/tools.ts:444-446` → `truncatedOutcome`（`drive/tools.ts:170-181`）。

**差异**：pi 不调整输出上限，Collie 会。Collie 的调整没有任何还原点（见 §4-W1）。

### 1.2 中断与效果不确定性

这是两侧设计哲学最接近、但实现代价差异最大的地方。

pi 用**三态**表达一次工具调用：`planned / effect_pending / outcome_ready`（`session/types.ts` 的 `ToolCall`，用法见 `drive/tools.ts:200-206, 246-251`）。恢复时 `drive/tools.ts:527`：

```ts
if (!cancelled && call.replay === "safe" && tool?.replay === "safe") { ... }
```

**双重确认**：持久化时记录的 `replay` 意图 **与** 当前工具声明的 `replay` 必须同时为 `"safe"` 才允许重放；否则读取 `pendingToolOutput` 的进度快照并合成 `interruptedOutcome`（`drive/tools.ts:158-168`），文本为 `INTERRUPTION_MARKER`（`drive/tools.ts:44-45`）——明确写出"较新的实时输出可能缺失，外部结果未知"。

Collie 的对等物是 `sessions.py:463 _replay_safe_read`，同样坚持"名字与 MCP 只读提示都不构成权威"，白名单硬编码为 `{read_file, glob, grep, memory_search, delegate}`（`sessions.py:474-475`），标志由 `loop.py:2306-2312` 依据**实现类型**（`type(tool) in (ReadFileTool, GlobTool, GrepTool, MemorySearchTool, DelegateTool)`）而非工具名写入。这一点比按名字判断更严格，值得肯定。

**实质差异**：pi 在工具执行**过程中**持续写入持久进度快照 —— `openToolProgress`（`runtime/progress.ts:90-117`）在 `execute` 回调 `options?.checkpoint === true` 时把 `AgentToolResult` 写入 `pendingToolOutput` 地址（`drive/tools.ts:386-389`）。Collie 没有这一层：`loop.py:1184-1227 _close_unanswered_calls` 只能生成纯文本 `INTERRUPTED: ... 其效果 UNKNOWN`，**不携带任何已产出的部分输出**。

需要公平指出：Collie 的 `BashTool` 在**协作式取消**（进程仍存活、`tool.run` 正常返回）路径上确实保留了部分输出并落盘 —— `tools.py:515-521` 的 `CANCELED` 分支、`tools.py:558-586 _interrupted` 会 spill 到文件。所以差距**仅存在于进程被硬杀/宿主崩溃**的场景：那时 Collie 什么也没有，pi 有最后一个 checkpoint 快照。

### 1.3 会话恢复

- Collie：`sessions.py:384 checkpoint` 每次在 `_locked(p)` 下 `_merge_messages` 后 `_atomic_dump` **整份会话 JSON**。恢复判定 `sessions.py:444 _recovery_required`，人工复核入口 `sessions.py:566 reconcile_recovery`（强制 `confirmed=True`，`sessions.py:573-574`）。
- pi：`lane.command / lane.continueOperation / lane.settleOperation` 提交的是 **writes 列表**（`setValue / deleteValue / appendList`，见 `session/values.ts` 的地址构造 `operationToolArgs / operationToolMemo / pendingEntry / pendingToolOutput`）。

代价明确：Collie 每个工具边界重写整份 transcript（`loop.py:2320-2321, 2425-2426` 每次工具前后各一次），**写放大与消息数成正比**；pi 是增量键值写。在 32 并发 × 长会话下这是可测量的 I/O 差异。

Collie 有一个 pi 没有的**硬正确性栅栏**：`loop.py:2320-2324`，若 `_session_checkpoint` 返回 `False`，工具**不执行**，返回 `ERROR: durability checkpoint failed; tool was not executed because crash recovery could not be fenced`。这是"无法保证可恢复就不产生副作用"的失败关闭策略，pi 的 `drive/tools.ts` 中没有对等的拒绝路径（其提交本身即是持久写，语义不同但不等价）。

### 1.4 队列 / steering

- pi：**两个独立队列** + 两种模式。`agent-loop.ts:168`（循环启动前轮询）、`:194-196`（长耗时 `prepareNextTurn` 后补轮询，注释明确说明 one-at-a-time 下不能重复投递）、`:257`（每轮末）、`:261-266`（本该结束时的 follow-up）。模式由 `RpcCommand` 的 `set_steering_mode` / `set_follow_up_mode`（`rpc-types.ts:43-44`）控制，取值 `"all" | "one-at-a-time"`，另有 `clear_queue`（`rpc-types.ts:26`）。
- Collie：**易失 + 持久双通道**。易失 `loop.py:835-843 _drain_steering`；持久 `loop.py:1006 _consume_durable_steering`，注入点 `loop.py:1804-1821`（轮首）与 `loop.py:2801-2826`（结束拦截）。

Collie 的持久通道在正确性上更强：claim → 追加 journal 消息 → checkpoint → ack 的顺序被显式论证（`loop.py:1011-1015`），且 ack 失败不会伪造"已投递"（`loop.py:973-1004`）。它还有 pi 没有的 **`_steer_floor` 时序底线**（`loop.py:942-957`）：只有序号高于本次运行 floor 的指令才被采纳，防止上一次（可能已取消）运行的指令覆盖新请求。

代价：Collie 无"每轮一条"模式 —— `loop.py:1805` 直接 `"\n".join(steers)` 把队列全部合并成一条 user 消息。**pi 的 one-at-a-time 语义在 Collie 中不可表达**。

### 1.5 上下文管理（最大的行为分歧）

```
Collie: compaction.py:175   threshold_tokens: int = 48_000        # 绝对常量
pi:     compaction.ts:235-237  contextTokens > contextWindow - settings.reserveTokens
        compaction.ts:132-134  reserveTokens: 16384
```

Collie 的 docstring（`compaction.py:165-172`）诚实地说明了原因："Collie 没有可信的 per-model 上下文窗口元数据可读"。但后果是：对 Opus 5 这一级窗口的模型，Collie 在 ~48K 估算 token 处即触发一次额外模型调用做 handoff summary，pi 则要到 `window − 16384` 才动。**这是四倍量级的触发点差异**，直接影响成本、轮次、以及逐字历史保真度。

Collie 的补偿机制是 `context.py:354-393` 的历史省略（老工具输出截断为 240 字符 stub，窗口 14 条），pi 的对应物是 `keepRecentTokens ?? 20000`（`settings-manager.ts:847`）—— 一个按条数，一个按 token。

### 1.6 工作区与验证

- Collie 的验证是**循环内的一等公民**：`verify_gate` / `require_assert` / `_repro_verified`（`loop.py:1458-1473`，委托 `verifier.CodeReproVerifier`），证据判定基于进程退出码而非 `Traceback` 字符串（`loop.py:380-389 _repro_failed`），并有一整套 shell 反规避词法分析（`loop.py:117-137 _shell_unquoted_at`、`loop.py:189-246 _has_unsafe_test_shell_control`：拒绝 `|`、`;`、`||`、`&` 后台，放行 `&&` 与 `2>&1`）。`loop.py:249-313` 还会 `ast.parse` Python `-c` 载荷来判断是否真有 `assert`。
- pi **没有这一层**。pi 的等价物是 `before_tool` / `after_tool` hook（`drive/tools.ts:452-463, 412-427`）与 `result.terminate`（`agent-loop.ts:589-591 shouldTerminateToolBatch`：仅当**全部**调用都 `terminate === true` 才终止）。

对基准的含义：任何"必须证明修复有效"的题目，Collie 自带执行内的证据门，pi 需要外部评分器承担。反过来说，**用 Collie 的 verify_gate 去评 pi 是不公平的**，因为那是 Collie 的产品能力而非模型能力。

---

## 2. 12 个可判别、可复现场景

统一前置：固定 `claude-opus-5`；固定同一 git 仓库快照 `REPO@SHA`；每次运行使用全新 worktree；所有通过标准均由**外部**观察（git 状态、进程表、宿主计时器、provider 侧计费记录），不采信 harness 自述。

### S1 — 输出截断下的工具参数安全
- 初始状态：仓库含 `big.txt`（约 40k 行）。人为把两侧输出上限压到 512 token（Collie: `COLLIE_MAX_TOKENS=512`；pi: 需在 model 配置中限制，见 §5 备注）。
- 提示词：`Rewrite the entire contents of big.txt into big2.txt using a single write tool call. Do not chunk it.`
- 注入：无。
- 外部通过标准：`big2.txt` **不存在或为空**；且转录中该次调用的结果文本包含"未执行/参数可能被截断"语义。**任何部分写入的 `big2.txt` 即为失败**（说明截断的参数被执行了）。
- 判别力：直接测 `loop.py:2105-2137` vs `agent-loop.ts:230-233`。预期两侧均通过；**若不通过，说明流式 JSON 抢救解析器把半个参数当成了合法参数**。

### S2 — 压缩触发点
- 初始状态：仓库含 30 个各 ~3k token 的文件。
- 提示词：`Read every file under ./corpus one at a time using the read tool, and after each one state only the file name. Do not summarize.`
- 注入：无。
- 外部通过标准：记录**第一次出现摘要/压缩模型调用时的累计输入 token**（provider 侧账单条目，非 harness 自述）。预期 Collie ≈ 48K±估算误差，pi ≈ `window−16K`。判为"行为差异确认"而非优劣。
- 二级标准：运行结束后，向两侧提问 `What was the exact first line of corpus/file_03.txt?`，核对与磁盘真值是否一致 —— 测量压缩造成的保真损失。

### S3 — 溢出恢复后的窗口粘滞
- 初始状态：同 S2，但构造一次真实的 `input too long` provider 错误（在 corpus 中放入一个超大文件使单轮必然溢出）。
- 提示词：同 S2。
- 注入：无。
- 外部通过标准：溢出发生后**再运行 10 轮**，测量每轮请求的 prompt token。Collie 预期观察到窗口从 14 收紧到 4 且**不再恢复**（`loop.py:1927` 置位，`context.py:364-367` 读取，全仓库无清除点）。判定：若压缩成功后 prompt token 仍停留在收紧水位，则该弱点复现。**目前标记为未复现。**

### S4 — 工具执行中途的 steering
- 初始状态：仓库含 `slow.sh`（`sleep 25`）。
- 提示词：`Run ./slow.sh with the bash tool, then report its exit code.`
- 注入时机：观察到 `tool_start` / `tool` 事件后 **+3 秒**，注入 `Stop what you're doing and instead just print the contents of README.md.`（Collie：`Harness.steering` 回调或 task_inbox；pi：RPC `{"type":"steer","message":...}`）。
- 外部通过标准：(a) `slow.sh` 的进程在宿主进程表中于注入后 ≤2 秒内消失 **或** 明确运行至 25 秒完成 —— 二者都可接受，但转录必须**如实说明是哪一种**；(b) README 内容出现在最终答案中；(c) 不得出现"已停止"但进程仍在运行的组合。
- 判别力：Collie 的 steering 只在轮边界排空（`loop.py:1804`），因此预期 (a) 走"运行到完成"分支；pi 的 `getSteeringMessages` 同样在轮边界（`agent-loop.ts:257`）。**真正的区分在 (c)**：Collie 的 `tool_process` 有 `tree_terminated` 三态并会在未确认时打出警告（`tools.py:569-575`），pi 依赖 `AbortSignal` 传递给 `tool.execute`（`agent-loop.ts:686-689`）。

### S5 — 收尾拦截
- 提示词：`Add a one-line docstring to src/util.py and then stop.`
- 注入时机：在**模型返回不含工具调用的最终消息之后、下一次提示之前**（Collie 的 `loop.py:2801-2826` 窗口；pi 的 `agent-loop.ts:261-266` follow-up 窗口）注入 `Also add the same docstring to src/util2.py.`
- 外部通过标准：`git diff` 同时包含 `src/util.py` 与 `src/util2.py` 的改动，且**在同一次会话内**（不是第二次调用）。
- 判别力：pi 用独立的 follow-up 队列显式覆盖此窗口；Collie 用 `_drain_steering` + `_consume_durable_steering` 双重覆盖。这是两侧都声称支持、但实现路径完全不同的能力。

### S6 — 硬杀恢复（核心判别场景）
- 初始状态：仓库含 `mutate.sh`：`echo START >> log.txt; sleep 20; echo END >> log.txt`。
- 提示词：`Run ./mutate.sh with the bash tool, then read log.txt and tell me what's in it.`
- 注入时机：`tool_start` 后 **+5 秒**，对 harness 宿主进程发送 `SIGKILL`（Windows：`taskkill /F /PID <host>`，**不带 `/T`**，以模拟宿主死亡而子树存活）。
- 恢复：以同一 session id 重新启动（Collie：`sessions.resume_after_interrupt`；pi：`--session-id <same>`）。
- 外部通过标准：
  1. 恢复后的转录中，该工具调用**必须**被标记为效果未知/需人工核对，**不得**被自动重放；
  2. `log.txt` 最终**恰好包含一个 `START`**（重放会产生两个）；
  3. Collie 侧：`sessions.recovery_state(sid)["recovery_required"] is True`（由 `sessions.py:444-449` 保证，因 `bash` 不在 `sessions.py:474-475` 白名单内）；pi 侧：出现 `INTERRUPTION_MARKER` 文本（`drive/tools.ts:44-45`）。
- 加分项：pi 若在中断结果中携带 `mutate.sh` 已产出的部分 stdout（来自 `pendingToolOutput`），Collie 不携带 —— 记录该差异。

### S7 — 取消的"消亡确认"
- 初始状态：仓库含 `spawn.sh`，它 fork 一个孙子进程 `sleep 300` 后自身退出。
- 提示词：`Run ./spawn.sh with the bash tool.`
- 注入时机：`tool_start` 后 +2 秒发出取消（Collie：`cancelled` 回调置位；pi：RPC `{"type":"abort"}`）。
- 外部通过标准：取消返回后 5 秒，检查进程表中是否仍有 `sleep 300`。**若存在，则 harness 报告的"已停止"必须显式声明未确认**。Collie 有此三态（`tools.py:569-575` 的 `tree_terminated` 分支，及 `tool_process.py:462-471` 关于 `taskkill /T` 只报告投递不报告消亡的注释）；pi 的 `abort` 路径在 `AbortRequested`（`effect-gate.ts:1-10`）之下没有等价的树消亡断言。
- 这是**Collie 预期占优**的场景，应纳入以避免基准单向偏置。

### S8 — 并行 vs 顺序批次
- 提示词：`In one message, call the read tool on a.txt, b.txt, and c.txt, and also call the write tool to create d.txt containing "x". Then tell me a.txt's first line.`
- 外部通过标准：`d.txt` 存在且内容为 `x`；转录中四个 `tool_call_id` **全部**有配对结果（协议合法性）。
- 判别力：pi 在 `agent-loop.ts:417-423` 依据 `tool.executionMode === "sequential"` **或** 全局 `config.toolExecution` 决定并行/串行，并在 `drive/tools.ts:611-653 runParallel` 中用 `scheduleMaterialization` 串行化**结果落库**而并行执行。Collie 的 `loop.py:2571-2588` **只有串行**一条路径。测量墙钟时间差。

### S9 — 队列模式语义
- 注入时机：单轮内快速注入三条 steering 消息 `A`、`B`、`C`。
- 外部通过标准：在 pi 的 `one-at-a-time` 模式下，第一轮上下文中只出现 `A`；`all` 模式下三条都出现。Collie **无此模式**，`loop.py:1805` 恒等于 `all` 并合并为单条消息。
- 结论用途：这不是"谁更好"，而是**基准必须把 pi 显式钉在 `all` 模式**才能对齐（见 §5）。

### S10 — 验证门（必须双向公平）
- 初始状态：一个含已知失败测试的仓库。
- 提示词：`Fix the failing test in tests/test_calc.py. Run the test suite to confirm.`
- 外部通过标准：由**外部评分器**独立执行 `pytest -q` 判定通过与否。**禁止**采用 harness 自述的 `verified` 字段。
- 必须同时记录的对照量：Collie 在此路径上会启用 `verify_gate` 并可能注入 `VERIFY_NUDGE`（`loop.py:53-60`）/ `REPAIR_NUDGE`（`loop.py:66-69`）；pi 无任何注入。若基准目标是**模型能力**，则 Collie 必须以 `self_verify=False, verify_gate=False, force_edit=False` 运行；若目标是**产品能力**，则保留并明确标注。二者混用会直接产生无效排名。

### S11 — 工作区突变会计
- 提示词：`Create a file called out.txt with the text hello, then delete it.`
- 外部通过标准：运行结束后工作区 tree digest 与运行前**相同**；两侧的运行元数据都应报告"发生过突变"而非"无突变"。
- 判别力：Collie 的 pi 传输用 `_mutation(before, after)`（`agent_runners.py:1267-1272`）**仅比较首尾 digest**，因此这个"创建后删除"的场景会被记为**未突变** —— 这是一个可复现的会计漏洞，且它直接喂给 `recovery = not settled and (mutated or not complete)`（`pi_rpc_runner.py:414`），意味着一次中途失败但净零变更的 pi 运行**不会**被标记为需恢复。

### S12 — 高并发家目录争用
- 初始状态：32 个并发运行，每个独立 worktree。
- 提示词：任意 S2 长任务。
- 外部通过标准：(a) 32/32 全部启动成功；(b) 无会话文件互相覆盖（每个 session id 的最终 JSONL/JSON 可独立解析且消息数单调）；(c) 记录 p50/p99 启动延迟。
- 判别力：Collie 的 pi 传输在 `pi_rpc_runner.py:269` 调用 `runner_env.child_env(self.env_policy)` 且**不传 `home=`**，32 个 pi 进程共享真实 `~/.pi/agent`（`config.ts:528-534 getAgentDir`）—— 共享 settings、共享 model catalog、共享 sessions 根目录。Collie 自身则每个运行独立持锁（`sessions.py:397 _locked(p)`）。这是本基准最可能被基础设施而非模型能力污染的一环。

---

## 3. 使朴素排名失效的模型 / 工具 / 认证差异

这一节是本报告最重要的部分：**在不做以下对齐的情况下，任何 Collie-vs-pi 的分数差都主要不是 harness 质量差。**

### 3.1 思考预算：默认值相反（最严重）

```
pi:     core/defaults.ts:3        DEFAULT_THINKING_LEVEL: ThinkingLevel = "medium"
        agent-session.ts:1861     settingsManager.getDefaultThinkingLevel() ?? ... ?? DEFAULT_THINKING_LEVEL
Collie: providers.py:875-876      _think = COLLIE_THINKING not in ("", "0", "off", "false", "no")  →  默认 False
        providers.py:896-897      if _think: body["thinking"] = {"type": "adaptive"}
```

开箱即用地跑，是"**无思考的 Opus 5**"对"**medium 思考的 Opus 5**"。而 `pi_rpc_runner._argv`（`pi_rpc_runner.py:246-256`）**从不传 `--thinking`**，也从不发 `set_thinking_level` RPC。这一项单独就足以颠倒任何推理密集型任务的排名。

另注 `providers.py:877`：Collie 在 `_think` 为真时会把 `max_tokens` 抬到至少 32000，即思考开关同时改变输出上限 —— 两个变量被耦合。

### 3.2 默认模型不是同一个

`providers.py:811` 的默认为 `claude-opus-4-8`；pi 的默认 provider 是 `google`（`args.ts:278` 帮助文本）。基准必须在两侧显式钉 `claude-opus-5`，且 pi 侧需通过 `--model` 或 `set_model` RPC，并**记录 `get_state` 返回的实际 `model` 字段**作为收据。

### 3.3 工具集不对等，且 pi 侧被结构性阉割

```python
# harness/pi_rpc_runner.py:37
_TOOLS = "read,edit,write,grep,find,ls"
# 同文件 docstring 第 3-7 行：
#   "Pi's RPC protocol ... does not have a tool approval round-trip, so Collie never
#    enables Pi's bash tool"
```

pi 侧**没有 bash / powershell**。Collie 侧有 `BashTool`（`tools.py:466`）、`RunInEnvTool`（`tools.py:589`）、`execute_code`（含 RPC 内部工具经纪 `loop.py:2455-2563`）、`delegate` 子代理、`code_search`（嵌入检索，`codeindex.related_locations`，`loop.py:2621-2626`）、`memory_search`。

后果：**S10 及一切需要执行测试的题目，pi 在当前传输下是结构性零分**，与模型能力无关。理由（无审批往返）在 pi 源码中确实成立 —— `rpc-types.ts` 的 `RpcCommand` 中不存在 permission/approval 请求类型，`extension_ui_request`（`rpc-types.ts:246-281`）的 `confirm` 方法是扩展 UI 而非工具授权 —— 但这意味着**公平比较必须选择：要么两侧都关掉 shell，要么给 pi 一个非交互的信任模式**（见 §5）。

### 3.4 上下文注入不对等

Collie 的 `composer.build`（`loop.py:1822-1823`）会做记忆预取（`loop.py:1846 res.mem_recalls += meta.prefetched`）、分层提示、项目上下文。而 pi 传输被显式剥光：`--no-extensions --no-skills --no-prompt-templates --no-context-files --no-approve`（`pi_rpc_runner.py:247-249`）。`--no-context-files` 关闭 AGENTS.md/CLAUDE.md 发现；`--no-approve` 令 `projectTrustOverride = false`（`args.ts:221-222`），忽略项目本地文件。

这是"满配 Collie vs 裸奔 pi"。要么两侧都裸奔（Collie 侧需关闭记忆预取与 skills），要么两侧都满配。

### 3.5 认证路径完全不同 —— 且当前配置下 pi 可能根本无法启动

两个独立事实叠加：

1. `runner_env._SENSITIVE_PREFIXES`（`runner_env.py:235-240`）包含 `"ANTHROPIC_"`。`child_env` 按**构建**而非过滤生成子环境（`runner_env.py:337-357`），所以 pi 子进程**拿不到任何 API key**，只能走它自己存储的凭据（这正是 `pi_rpc_runner.probe` 去跑 `pi auth check --provider ... --no-refresh` 的原因，`pi_rpc_runner.py:196-213`）。
2. `pi_rpc_runner._operate:268` 调用 `runner_env.assert_no_billing_override(os.environ, "collie-sidecar")`。`"collie-sidecar"` 不在 `_BILLING_ROUTE_PREFIXES`（`runner_env.py:255-261`）中，故落入 `_BILLING_ROUTE_ANY`（`runner_env.py:265-266`）= `ANTHROPIC_ / AZURE_OPENAI_ / CLAUDE_ / CODEX_ / OPENAI_` 全集。**只要宿主环境里存在 `ANTHROPIC_API_KEY`（即 Collie 原生跑 Opus 5 的标准配置），`PiRpcRunner` 就在启动前抛 `BillingOverrideError` 拒绝运行**（`runner_env.py:425-429`）。

因此：Collie 走 API key 计费，pi 走订阅/OAuth 存储凭据 —— **不同的速率限制、不同的路由、可能不同的模型快照**。而且两者不能在同一个 shell 环境里同时跑。这必须在基准编排层解决（§5）。

### 3.6 重试与预算语义（此处反而基本对齐，值得记录）

```
Collie: loop.py:530-535   RETRIES 默认 3, RETRY_BASE 默认 2  → delay = retry_base * 2**attempts (loop.py:1971)
pi:     settings-manager.ts:30-35   enabled ?? true, maxRetries ?? 3, baseDelayMs ?? 2000
```

数值巧合一致。但 Collie 额外有一次**结构化响应修复**（`max_contract_repairs = 1`，`loop.py:540`，注入 `FORMAT_REPAIR_NUDGE`，`loop.py:47-51`），且该修复消息**刻意不进入持久历史**（`loop.py:1948-1954`）—— pi 无对等物。若某模型偶发协议违规，Collie 会多花一次请求救回来，pi 直接失败。这会在错误率统计上产生系统性偏移，需单列。

### 3.7 pi 的自动重试与自动压缩来自用户设置文件

`agent-session.ts:725-738 _willRetryAfterAgentEnd` 读 `settingsManager.getRetrySettings()`；`getCompactionSettings()`（`settings-manager.ts:850-856`）读 `this.settings.compaction`（全局 + 项目合并）。由于 §3.5 中 pi 共享真实 `~/.pi/agent`，**评测机上任何遗留的 settings.json 都会静默改变 pi 的行为**。必须隔离 HOME（§5）。

---

## 4. Collie 的源码级潜在弱点

每条标注证据强度。我未执行任何代码，因此"影响"一律标记为**未复现**；仅"代码事实"部分为确定。

### W1 — `provider.max_tokens` 被永久升级，无还原点
**代码确定。** `loop.py:2124-2129`：

```python
cur = int(getattr(self.provider, "max_tokens", 0) or 0)
if cur:
    self.provider.max_tokens = min(32768, cur * 2)
```

全 `loop.py` 中不存在对 `provider.max_tokens` 的还原。对照 `delegate.py:108` —— 该模块**明确**为子运行保存并还原了同一属性：

```python
saved = {key: getattr(parent.provider, key) for key in ("max_tokens", "cache_stable_upto") ...}
```

即代码库知道这个模式，只是主循环没用。

**影响（未复现）**：若基准在多个任务间复用同一个 `Harness`/`ModelProvider` 实例，任务 N 的一次输出截断会把任务 N+1..∞ 的输出上限翻倍 → **结果与任务执行顺序相关，不可复现**。若每任务新建 provider（`swe.py` 路径可能如此，我未逐条追踪构造点），则无影响。**建议在基准中把每任务新建 provider 作为硬约束，并断言 `provider.max_tokens` 在运行前后相等。**

### W2 — `_overflow_shrink` 是单向粘滞开关
**代码确定。** 写入点唯一：`loop.py:1927 session["_overflow_shrink"] = True`。读取点唯一：`context.py:364 shrink = bool(session.get("_overflow_shrink"))`，据此把窗口 14→4、stub 240→120、并对近期窗口加 4000 字符上限（`context.py:365-367`）。全仓库 grep 无任何 `pop("_overflow_shrink")` / `= False`。

**影响（未复现）**：一次溢出后，即便随后的 semantic compaction 已把请求压到阈值以下，历史窗口仍**永久**停在 4 条消息 + 120 字符 stub。对长会话意味着模型此后基本看不到工具输出。这与同文件的语义压缩是两条独立轴（`context.py:368-372` 有说明），二者叠加可能过度削减上下文。S3 即为此设计。

### W3 — 压缩阈值与模型上下文窗口解耦
**代码确定。** `compaction.py:175 threshold_tokens: int = 48_000`，且 `plan()` 的判据是 `compaction.py:499: if not force and total_tokens < policy.threshold_tokens`。pi 的对等判据是 `compaction.ts:235-237` 的 `contextTokens > contextWindow - reserveTokens`。

docstring（`compaction.py:165-172`）已诚实承认这是策略默认而非测量，并提供 `COLLIE_COMPACT_TOKENS` 逃生口（`compaction.py:213`）。

**影响（未复现，但方向确定）**：对 Opus 5，Collie 会显著提前压缩 → 每次多一次模型请求（`loop.py:1349-1351` 明确把该请求计入账本与预算）、更早失去逐字历史。**在高并发基准中这是成本与保真度的系统性偏移，不是随机噪声。**

### W4 — pi 传输不隔离 HOME，且 `PI_*` 无法经 `extra=` 注入
**代码确定，两点。**
1. `pi_rpc_runner.py:269`：`env, self.last_env_receipt = runner_env.child_env(self.env_policy)` —— **未传 `home=`**。而 `runner_env.child_env` 明确支持 `home=` 并会同时改写 `HOME/USERPROFILE/HOMEDRIVE/HOMEPATH` 四者（`runner_env.py:361-375`），docstring（`runner_env.py:324-328`）甚至写明这正是"phase 3 sidecar 的渲染 home"用途。功能已备好，调用点没用。
2. `runner_env.py:238` 的 `_SENSITIVE_PREFIXES` 含 `"PI_"`，而 `child_env` 对 `extra` 中的敏感名**抛异常**（`runner_env.py:379-382`）。因此**无法**通过 `extra={"PI_CODING_AGENT_DIR": ...}` 做隔离 —— 唯一可行路径就是 `home=`。

**影响（未复现）**：32 并发共享 `~/.pi/agent` → 共享 settings.json（§3.7 的行为泄漏）、共享 sessions 根、共享 model catalog 写入。S12 即为此设计。

### W5 — `_mutation` 只比首尾 digest，创建-删除型突变被记为"无突变"
**代码确定。** `agent_runners.py:1267-1272`：

```python
def _mutation(before, after):
    left = str(before.get("tree_digest") or ""); right = str(after.get("tree_digest") or "")
    complete = bool(left and right and before.get("snapshot_complete") and after.get("snapshot_complete"))
    return bool(left and right and left != right), complete
```

该返回值直接决定 `pi_rpc_runner.py:414: recovery = not settled and (mutated or not complete)`。

**影响（未复现）**：一次中途被取消/超时的 pi 运行，若其净效果恰好为零（写后删、或写入后被同名覆盖回原内容），`mutated=False` → `recovery_required=False` → `_validate`（`pi_rpc_runner.py:229-232`）放行续跑，而实际上外部世界（工作区之外：网络、数据库、`log.txt` 之类被 append 的文件若在 `workspace_snapshot` 覆盖范围外）可能已被改变。注意 `workspace_snapshot` 本身有 `snapshot_complete` 三态并会在不完整时置 False（`verification.py:192-262`），这部分设计是稳健的；缺口在于**digest 相等 ≠ 未发生突变**。

### W6 — 易失 steering 通道静默吞异常，与持久通道的诚实性不对称
**代码确定。** `loop.py:835-843`：

```python
def _drain_steering(self):
    if not self.steering: return []
    try:
        return [s.strip() for s in (self.steering() or []) if isinstance(s, str) and s.strip()]
    except Exception:
        return []
```

同一文件的持久通道则相反 —— `loop.py:858-869 _inbox_failed` 的 docstring 明写"我们唯一不做的事就是隐藏它"，并把失败同时 emit **且**挂在 `res.input_failures` 上。

**影响（未复现）**：接了易失回调的宿主（TUI、嵌入式调用方）若回调抛错，用户的指令被静默丢弃，且 `res` 上无任何痕迹。`loop.py:556-558` 的注释已警告"不要把两个通道接到同一份文本"，但没有解决回调失败的可观测性。

### W7 — 批内预授权与执行之间存在状态漂移窗口
**代码确定的设计选择，风险未复现。** `loop.py:2565-2568` 先对整批 `_prepare_tool_call`（含 `_authorize`），`loop.py:2571-2588` 再逐个执行。注释（`loop.py:2160-2163`）明确论证了理由：让人一次看到全部五个调用再决定，而不是在前两个已不可逆地发生后才发现第三个。

**代价**：第 1 个工具的副作用可能使第 3 个工具的授权前提失效（例如第 1 个调用把某路径变成了符号链接，而授权时 `risk.target_for` 解析的是旧目标）。pi 的模型不同 —— `drive/tools.ts:452-463` 的 `before_tool` 在每个调用**即将执行前**才评估。二者各有失效模式；应在基准中作为差异记录而非缺陷评分。

---

## 5. pi 的公平对照传输方案（全部基于其源码，未虚构任何 flag）

Collie 已有 `PiRpcRunner`，方案是**在其基础上做五处最小改动**，而非另起炉灶。所有引用的 flag / RPC 命令均已在本次审计中于 pi 源码逐条核对。

### 5.1 已核实存在、可直接沿用的部分

`pi_rpc_runner._argv`（`pi_rpc_runner.py:246-256`）当前发出的每一个 flag 都在 `args.ts` 中被真实解析：`--mode rpc`（`args.ts:95-99`）、`--tools`（`:137`）、`--no-extensions`（`:169`）、`--no-skills`（`:188`）、`--no-prompt-templates`（`:190`）、`--no-context-files`（`:194`）、`--no-approve`（`:221`）、`--model`（`:106`）、`--session-id`（`:125`）、`--fork`（`:127`）。

握手序列同样正确：`get_state` → `prompt` → 等 `agent_settled` → `get_session_stats` → `get_last_assistant_text`（`pi_rpc_runner.py:334-370`）。**`agent_settled` 的语义已核实可靠**：`agent-session.ts:1105-1118 _runAgentPrompt` 在 `finally` 中调用 `_emitAgentSettled`，而该 `finally` 位于 `while (await this._handlePostAgentRun()) await this.agent.continue()` 之后 —— 也就是说自动重试、溢出压缩后的续跑、以及队列排空（`agent-session.ts:1127-1147`）**全部完成后**才发一次。等第一个 `agent_settled` 是正确的回合终点。

### 5.2 五处必需改动

**(a) 隔离 HOME —— 必须。**
```python
env, receipt = runner_env.child_env(self.env_policy, home=per_run_home)
```
`home=` 已在 `runner_env.py:361-375` 实现且会同时改写四个变量。pi 的 `getAgentDir()`（`config.ts:528-534`）在 `PI_CODING_AGENT_DIR` 未设时回落到 `join(homedir(), ".pi", "agent")`，因此改写 HOME 即可完整隔离 settings / sessions / catalog。**不要**尝试 `extra={"PI_CODING_AGENT_DIR": ...}` —— `runner_env.py:238` 的 `PI_` 前缀会让它抛 `BillingOverrideError`。

**(b) 显式钉住思考等级 —— 必须。**
两条等价路径，均已核实：CLI `--thinking <level>`（`args.ts:147-156`，合法值见 `args.ts:60`：`off|minimal|low|medium|high|xhigh|max`），或握手后发 RPC `{"type":"set_thinking_level","level":...}`（`rpc-types.ts:38`）。推荐**用 RPC 并回读 `get_state.thinkingLevel`**（`rpc-types.ts:98`）作为收据 —— CLI flag 无法自证生效。

**(c) 显式钉住自动重试、自动压缩、队列模式 —— 必须。**
握手后依次发送，全部为已核实的命令：
- `{"type":"set_auto_retry","enabled":<bool>}`（`rpc-types.ts:51`）
- `{"type":"set_auto_compaction","enabled":<bool>}`（`rpc-types.ts:48`）
- `{"type":"set_steering_mode","mode":"all"}` 与 `{"type":"set_follow_up_mode","mode":"all"}`（`rpc-types.ts:43-44`）—— 因为 Collie 侧只有 `all` 语义（`loop.py:1805`），钉在 `all` 才对齐。
`get_state` 的 `autoCompactionEnabled / steeringMode / followUpMode`（`rpc-types.ts:101-106`）可用于回读校验。

**(d) 解决工具集不对等 —— 二选一，必须明示。**
- 路线 A（测模型推理，推荐用于大多数题目）：两侧都无 shell。Collie 侧从 `ToolRegistry` 移除 `bash`/`run_in_env`/`execute_code`，并设 `self_verify=False, verify_gate=False, force_edit=False, critic=False`。
- 路线 B（测端到端产品能力）：给 pi 加回 `bash`，即 `_TOOLS = "read,edit,write,grep,find,ls,bash"`（`bash` 是 pi 的内置工具名，`args.ts:439` 帮助文本已列出）。`pi_rpc_runner.py` docstring 拒绝这么做的理由是"无审批往返"—— 该理由在 pi 源码中成立（`rpc-types.ts` 的 `RpcCommand` 无任何 permission 类型），所以路线 B **只能在一次性丢弃的沙箱工作区**中使用，且必须在收据中标注"pi 以无审批模式运行"。

**(e) 离线与启动确定性 —— 建议。**
`--offline`（`args.ts:223`）等价于 `PI_OFFLINE=1`，关闭启动期网络操作（模型目录刷新等）。由于 §5.2(a) 的隔离 HOME 会让每次运行都面对一个空的 catalog 缓存，加 `--offline` 前需先在隔离 home 中预置 catalog，否则模型解析可能失败。**这是本方案唯一需要实测确认的环节，我标记为未验证。**

### 5.3 不要做的两件事

1. **不要用 `--print`/`-p`**（`args.ts:157-163`）替代 rpc 模式。`-p` 是一次性非交互，没有 steering/abort/compact，S4/S5/S7 全部无法执行。
2. **不要用 RPC 的 `{"type":"fork","entryId":...}`**（`rpc-types.ts:62`）替代 CLI `--fork <id>`。二者语义不同：前者按 transcript 条目分叉，后者按会话分叉。`pi_rpc_runner.py:250-253` 当前用 CLI 形式并在 `pi_rpc_runner.py:378-384` 断言返回的 session id 与父不同 —— 这是正确的。

### 5.4 编排层必须解决的认证冲突

因 §3.5 的 `assert_no_billing_override` 会在宿主存在 `ANTHROPIC_*` 时**拒绝启动 pi runner**，基准编排必须让两侧在**不同的进程环境**中运行：Collie 侧的 worker 持有 `ANTHROPIC_API_KEY`，pi 侧的 worker 环境中该变量必须不存在（pi 自身从隔离 home 的 auth 存储取凭据）。在同一 shell 中顺序跑两侧会稳定失败，且失败信息是计费类文案，容易被误判为配额问题。

---

## 6. 修复优先级（按产品影响排序）

**P0 — 直接产生错误的基准结论**

1. **对齐思考等级与模型 ID**（§3.1、§3.2）。改动量最小、影响最大。在 `pi_rpc_runner._argv` 或握手序列中加 `set_thinking_level`，并把 `COLLIE_THINKING` 在基准配置中显式设定而非依赖默认。两侧都必须把实际生效值写进运行收据。
2. **隔离 pi 的 HOME**（W4）。单行改动：`child_env(self.env_policy, home=...)`。不做这一步，32 并发下的一切数据都可能被共享 settings 与共享 sessions 目录污染。
3. **明示工具集路线并使 Collie 的干预可关闭**（§3.3、§3.4、§5.2d）。`verify_gate` / `VERIFY_NUDGE` / `EDIT_FORCE_NUDGE` / `COVERAGE_NUDGE` / `ROLLBACK_NUDGE` / `critic` 是 Collie 的产品价值，但在"测模型"模式下它们是对 pi 的不对称加成。

**P1 — 影响 Collie 自身的可复现性**

4. **修复 `provider.max_tokens` 泄漏**（W1）。照搬 `delegate.py:108` 的 save/restore 模式包住 `loop.py:2124-2129`，或在 `_run` 结束时还原。这是顺序依赖型不可复现的直接来源。
5. **给 `_overflow_shrink` 一个清除条件**（W2）。最小修法：在 `_settle_compaction` 成功后（`loop.py:1381`）或在连续 N 轮未再溢出后 `session.pop("_overflow_shrink", None)`。

**P2 — 影响长任务成本与保真度**

6. **让压缩阈值跟随模型窗口**（W3）。即便无法从所有 provider 读到窗口大小，也可以为已知 provider/model 建一张表并回落到 48K 常量 —— 对 `anthropic` / `anthropic-oauth` 这两条 Collie 已特殊对待的路径（`loop.py:2037`）尤其值得做。

**P3 — 正确性收尾**

7. **`_mutation` 增加"是否发生过写"的独立信号**（W5），而不是仅比首尾 digest。
8. **易失 steering 失败上报**（W6）：把 `loop.py:835-843` 的 `except Exception` 改为走 `_inbox_failed` 同款路径，挂到 `res.input_failures`。

---

## 附：审计边界声明

- 全程只读。未执行 Collie、pi 或任何测试；未访问网络；未修改任何文件。
- 未读取凭据文件、用户会话、评分器或其它运行产物。`C:\workspace\collie\.bench-tmp\` 下的 `current-product-v1-*` 与 `vendor-inspect-main` 目录被识别为其它运行与 vendored 副本，**未进入审计**；pi 一侧只使用了指定的 pinned 树 `references\pi`。
- 所有"影响"结论均为静态推断，已逐条标注**未复现**。§2 的 12 个场景即为将这些推断转为实测的最小方案。
- 未采信任一侧 README 的能力声明；本报告的每条断言都锚定到具体文件与行号。