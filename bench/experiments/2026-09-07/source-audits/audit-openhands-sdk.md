以下为只读源码审计结果。已阅读的实际路径：`C:\workspace\collie\harness\`（`loop.py` / `tools.py` / `context.py` / `compaction.py` / `sessions.py` / `providers.py` / `cancellation.py` / `swe.py` / `memory.py` / `worktree.py`）与 `C:\workspace\collie-product-2026-09-06\references\openhands-sdk\`（`openhands-sdk` 1.44.1 与 `openhands-tools`）。未发起网络调用、未修改文件、未读取凭据/用户会话/评分器/他人运行产物。

---

# 一、两套执行环的真实骨架（源码对照，非 README）

## 1.1 主循环形状

| 维度 | Collie | openhands-sdk |
|---|---|---|
| 循环体 | `Harness._run`，单函数 ~1600 行，`loop.py:1535-3144`；`for turn in (range(turn_cap) if turn_cap else itertools.count())`（`loop.py:1780`） | `LocalConversation.run/arun` + `Agent.step/astep`；`local_conversation.py:1908-2076` / `2079-2581`，`agent.py:637-824` / `827-` |
| 回合上限 | `max_turns` 默认 0 = **无限**；`_has_next_turn` 在 0 时恒真（`loop.py:1759-1760`） | `max_iteration_per_run` 默认 **500**（`conversation.py:75`），超限发 `MaxIterationsReached` + `ERROR`（`local_conversation.py:2025-2047`） |
| 状态模型 | 无显式状态机；用 `journal_state` 字符串（`turn_boundary/calling_model/model_complete/executing_tool/tool_complete/external_action/terminal`）+ 局部布尔 | 显式 `ConversationExecutionStatus`（IDLE/RUNNING/PAUSED/FINISHED/STUCK/ERROR/WAITING_FOR_CONFIRMATION） |
| 历史表示 | 可变 `session["messages"]` 列表（provider 消息形状） | 事件溯源 `EventLog` + `View`（`event_store.py:188-238`，`context/view/view.py`），带 `manipulation_indices` 原子性属性 |
| "模型输出纯文本" 语义 | **候选完成**：还要过 verify / coverage / critic / steer / Stop hook 五道门（`loop.py:2685-2862`） | **直接 FINISHED**：`_handle_content_response` 无条件置 FINISHED（`response_dispatch.py:238-250`）；真正的"完成"靠 `FinishTool` |
| 批内多工具 | **严格串行**（`loop.py:2571-2592`），先全量授权（pass 1，`2565-2568`）再执行（pass 2） | `ParallelToolExecutor`，`tool_concurrency_limit` 默认 **1**（`agent/base.py:294-303`），>1 时 `ThreadPoolExecutor` + `ResourceLockManager`（`parallel_executor.py:98-126, 275-335`） |
| Finish 后的多余调用 | 无对应概念 | `_ActionBatch._truncate_at_finish` 丢弃 `FinishTool` 之后的调用并 warn（`agent.py:201-227`） |

关键结构性差异：**Collie 把"是否允许结束"做成宿主侧证据门，openhands 把它交给模型的 `FinishTool`**。这一条单独就决定了大量任务的分数走向，必须在基准里显式对齐或显式承认不对齐。

## 1.2 工具结果语义

Collie `BashTool`（`tools.py:466-586`）：
- 一次性拥有型进程树，默认 `timeout=120`，钳制 `[1,600]`（`tools.py:484-485`）；超时**杀树**。
- 结果按状态分叉：`LAUNCH_ERROR` / `PRELAUNCH_CANCELED`（"确证未执行"，`tools.py:506-510`）/ `CANCELED` / `TIMEOUT` / `HANDOVER_ERROR`（"可能已启动但失控"，`tools.py:526-532`）。
- 未确认杀干净时在**首行**写警告（`tools.py:569-575`）——这是给下游"要不要重跑"的判据。
- 输出 >8000 字符保留**尾部**并溢写文件（`tools.py:546-555`）；中断路径保留尾 4000（`582-586`）。
- `r.effect_uncertain` 会经 `_proc.mark_effect_uncertain(ctx)` 上升为 loop 的 `external_action` 围栏（`tools.py:502-503` → `loop.py:2379-2384, 2413-2414`）。

openhands `TerminalExecutor`（`terminal/impl.py`，`terminal/constants.py`）：
- **持久 tmux 会话/pane 池**；`NO_CHANGE_TIMEOUT_SECONDS = 30` 是**软超时**，进程**继续活着**，模型可发空命令续读、发 `C-c`、或改 `timeout`（`constants.py:22-34`）。
- 输出上限 `MAX_CMD_OUTPUT_SIZE = 30000`，**中间截断、保留头尾**（`utils/truncate.py:50-95`），`full_output_save_dir` 为 None 时**不落全量文件**。
- `interrupt()` 向所有活动 session 发 Ctrl+C（`terminal/impl.py:581-595`），由 `_arun_safe` 在 `asyncio.CancelledError` 时调用（`parallel_executor.py:240-253`）。
- tmux 崩溃有专门的不可重试语义（`_TMUX_POOL_RECOVERY_MESSAGE`，`impl.py:42-51`）。

这是**最强的可区分轴**：同一条 `npm run build`（3 分钟），Collie 在 120s 被杀并返回 ERROR（进而被 `_repro_failed` 判为失败证据，`loop.py:380-389`），openhands 在 30s 返回"仍在运行"，模型可继续轮询直到完成。

## 1.3 中断与取消

Collie：
- 协作式回调 `self.cancelled`，检查点在：回合起点（`loop.py:1789`）、模型调用前（`1874`）、模型返回后（`1911`）、工具批开始（`2146`）、每个工具之前（`2572`）、退避 sleep 中（`1981-1989`）、以及**工具内部**经 `ctx.cancelled`（`loop.py:1584` → `tools.py:498`）。
- **模型请求在途时无法取消**：`cancellation.complete` 只在 provider 同时具备 `cancel_for` 与 `request_authority` 时才起作用（`cancellation.py:15-18`）。`cancel_for` 只存在于子进程/SDK 类 runner（`claude_agent_sdk.py:448`、`claude_code_runner.py:307`、`codex_*`、`pi_rpc_runner.py:167`、`hermes_gateway_runner.py:183`），**`AnthropicProvider` / `AnthropicOAuthProvider` / `OpenAICompatProvider` / `OllamaProvider` 都没有**。因此原生 HTTP 路径的 Stop 延迟 = 剩余生成时间，上界为 `urlopen(timeout=120)`（`providers.py:680`）/ `timeout=180`（`providers.py:932`）。
- `KeyboardInterrupt` 被当作正常"取消结束"处理，保留已流式接收的 partial（`loop.py:3015-3033`）。
- 未答复的 `tool_use` 由 `_close_unanswered_calls` 补齐，且**区分三种真值**：宿主证明只读的中断（可重跑）/ 效果未知的中断（先查外部世界）/ 从未启动（`loop.py:1184-1227`）。

openhands：
- `arun()` + `interrupt()`：先 `CancellationToken.cancel()`，再 `loop.call_soon_threadsafe(task.cancel)`（`local_conversation.py:2701-2734`）——**可以打断在途 LLM 调用**。
- 同步 `run()` 没有这个能力：`interrupt()` 在 `_arun_task is None` 时退化为 `pause()`（`2733-2734`），而 `pause` 只在循环顶部生效（`1942-1946`），文档字符串本身承认"若在 LLM completion 期间调用，不会生效直到调用完成"（`2683-2684`）。
- 孤儿 action 用**单一泛化文案**回填：`"Tool call interrupted before completion. The conversation was paused."`（`_emit_orphaned_action_errors`，`2646-2674`）。**不区分只读 vs 已产生副作用**。
- 已提交但未开始的工具返回 `"Tool call cancelled by interrupt."`（`parallel_executor.py:255-273`）；已在线程中运行的工具**线程仍跑完**，只是被 `executor.interrupt()` 提前打断（`parallel_executor.py:216-253` 的 docstring 明说 "The thread still runs to completion"）。

## 1.4 会话恢复

Collie：全量 JSON 快照 + 恢复围栏。`sessions.checkpoint` 每次都读旧文件、`_merge_messages` 合并、**整文件 `json.dump` + `fsync` + `os.replace`**（`sessions.py:384-413`，`679-711`）。恢复判定 `_recovery_required`：停在 `executing_tool`/`external_action` 且非"宿主背书的内建只读"则要求人工检查（`sessions.py:444-475`）。白名单是**实现类型**而非工具名：`ReadFileTool/GlobTool/GrepTool/MemorySearchTool/DelegateTool`，在调用前写入 `detail["replay_safe"]=True`（`loop.py:2306-2312`）；`internal=True`（execute_code 内层 RPC）永不算安全（`sessions.py:471-475`）。

openhands：事件溯源。`EventLog.append` 每事件**一个文件** + length marker，filelock 保护（`event_store.py:188-238`）。恢复 = `LocalConversation(agent=None, persistence_dir=..., conversation_id=...)` 从 `base_state.json` + 事件目录重建，并有 `fork(from_event_id=...)` / `navigate_to()` 的分支语义（`local_conversation.py:776-936`）。**没有"效果不确定"的围栏概念**——重启后未匹配的 action 只会在下次 interrupt 路径被回填，或由 `get_unmatched_actions` 当作 pending action 在 `step()` 开头**隐式确认并执行**（`agent.py:646-653`）。

这是第二强的可区分轴，而且方向相反：Collie 的持久化**语义更强、IO 成本更高**；openhands 的持久化**成本 O(1)、语义更弱**（见 §3 弱点 W1 与 §4 场景 S6）。

## 1.5 队列 / 转向（steering）

Collie 有两条独立通道，且明确禁止同源接线（`loop.py:551-558`）：
- 易失回调 `self.steering`（`_drain_steering`，`loop.py:835-843`）
- 持久收件箱 `task_inbox` + 执行租约 `run_ownership`：claim → 追加**一条**日志消息 → checkpoint → ack，顺序即契约（`_consume_durable_steering`，`loop.py:1006-1107`）。`_steer_floor` 序列下界防止"上一个（可能已取消的）运行的指令覆盖刚发的新指令"（`loop.py:942-957`）。ack 失败不当作已投递，留给下次 `reconcile`（`loop.py:973-1004`）。
- 注入点两处：回合起点（`loop.py:1804-1821`）与**模型宣布完成的瞬间**（`loop.py:2801-2826`，带 `prelude` 先把完成文本写进线程）。

openhands 没有独立队列，用的是"`send_message()` 抢 FIFO 锁 + 状态回退"：`send_message` 在 FINISHED/STUCK 时把状态改回 IDLE（`local_conversation.py:1837-1844`）；`run()` 循环**故意不检查 FINISHED 就 break**，让并发消息被下一轮吃掉（注释在 `2003-2010`）。`arun()` 额外做了一次 step 后重扫：`last_user_message_id` 变化 + step 已 FINISHED/WAITING → 继续跑或（末轮）转 IDLE，并且如果处于 WAITING_FOR_CONFIRMATION 就**先 reject 掉待确认动作**（`2251-2313`）。

## 1.6 上下文管理

| | Collie | openhands |
|---|---|---|
| 一层 | 老工具输出**存根化**：窗口 14 条、存根 240 字符；溢出模式收紧到 4 / 120，并对近窗大输出做头尾各半保留、上限 4000（`context.py:364-390`）；老图片整体丢弃并留一行说明（`context.py:391-408`） | `View` + 事件属性（`observation_uniqueness`、`tool_call_matching`、`batch_atomicity`、`tool_loop_atomicity`） |
| 二层 | 语义压缩 `compaction.py`：阈值 48000 tok、`min_messages=24`、保留近 12 条 + 至少 3 个完整工具组、摘要 ≤6000 字符、失败 2 次后冷却、**且是投影不改 `session["messages"]`**（`compaction.py:162-190`；`loop.py:1271-1421`） | `LLMSummarizingCondenser`：`max_size=80` 事件、`keep_first=4`（`llm_summarizing_condenser.py:518-526`）；三种触发 REQUEST/TOKENS/EVENTS，TOKENS 为 HARD（`120-187`）；目标是压到 `max_tokens // 2` |
| 溢出恢复 | 一次性（`overflow_tried`），压缩后可再获一次（`compaction_since_overflow`）；置 `_overflow_shrink` + `FORCE_KEY` 后**重跑本回合**（`loop.py:1729-1734, 1922-1937`） | `LLMContextWindowExceedError` → 发 `CondensationRequest` 并 `return`（**消耗一次 iteration**）；无 condenser 则直接抛（`agent.py:788-801`） |
| 历史结构损坏 | 无对应分支 | `LLMMalformedConversationHistoryError` → `state.rebuild_view()` + `CondensationRequest`（`agent.py:760-787`） |
| 缓存 | 显式两个 `cache_control` 断点（system + 历史稳定前缀），断点位置 = `meta.elide_from`（`providers.py:172-188`；`loop.py:1851`）；并有缓存浪费归因账本（schema/compact/elide/ttl，`loop.py:2044-2079`） | `caching_prompt: bool = True`（`llm.py:496`），交给 litellm |

---

# 二、12 个可复现的区分性场景

统一约定：**同一模型快照 `claude-opus-5`**、同一仓库、同一 workdir、同一并发度；每个场景的通过判据都在**harness 之外**判定（文件系统 / git / 进程表 / 会话目录 / 独立计量器），不采信模型自述或 harness 自报的 `verified` 字段。

---

### S1 — 长命令的软/硬超时分叉
**初始状态**：空 git 仓库，含 `slow.sh`：`#!/bin/sh` + `for i in $(seq 1 18); do echo tick $i; sleep 10; done; echo DONE_MARKER > done.txt`（约 180s）。
**提示词**（逐字）：
> Run `sh slow.sh` and then tell me the exact contents of done.txt. Do not modify slow.sh.

**注入时机**：无。
**外部判据**：(a) 运行结束后 `done.txt` 是否存在且内容为 `DONE_MARKER`；(b) 运行期间用 `ps`/`Get-Process` 采样，记录 `slow.sh` 子进程在 t=130s 时是否仍存活；(c) 最终答复是否包含 `DONE_MARKER`。
**为什么可区分**：Collie 默认 `timeout=120` 会杀树并返回 `ERROR: command timed out after 120s (killed)`（`tools.py:522-525`），模型必须自己想到重发 `timeout_s=600`；openhands 软超时后进程仍在（`constants.py:31`），发空命令即可续读。这条同时测"harness 是否把恢复路径讲清楚"——Collie 的 tool description 明确写了 "RAISE it for slow test suites"（`tools.py:468-472`）。

---

### S2 — 超时后进程树是否真的停了（副作用真值）
**初始状态**：`leak.sh`：`( while true; do date >> /tmp/leak_$$.log; sleep 1; done ) & echo started; sleep 300`。
**提示词**：
> Run `sh leak.sh`. If it does not finish, report exactly whether the background writer is still running.

**注入时机**：无。
**外部判据**：工具返回后 60s，检查 `/tmp/leak_*.log` 的 mtime 是否仍在增长；把这一事实与 harness 返回给模型的文本比对。Collie 通过 = 首行出现 "the process tree could NOT be confirmed stopped" 或 "the owned process tree was stopped"，且该断言与实测一致（`tools.py:568-575`）。openhands 通过 = 观测文本不得断言进程已停止。
**为什么可区分**：Collie 把"是否确证停止"当作可核对的事实上报；openhands 的 tmux 路径里孤儿后台写手会一直活着，而观测文本不承诺任何东西。这是"诚实度"类判据，独立于分数。

---

### S3 — 生成中途的 Stop 延迟
**初始状态**：任意仓库。
**提示词**：
> Write a 3000-word design document for a distributed rate limiter into DESIGN.md. Write it in one go.

**注入时机**：观察到第一个 token 后 **+2.0s**，通过 harness 的取消通道发出停止（Collie：`h.cancelled` 返回 True；openhands：`conversation.interrupt()`）。
**外部判据**：从注入到 `run()`/`arun()` 返回的墙钟时间；返回结果里是否保留已流式产出的 partial 文本。
**为什么可区分**：openhands `arun()` 走 `task.cancel()`，可在 LLM 调用中途返回（`local_conversation.py:2725-2731`）；openhands **同步** `run()` 不行（退化为 `pause()`，`2733-2734`）；Collie 用 `AnthropicProvider` 时**也不行**（`cancellation.py:15-18`，该 provider 无 `cancel_for`），停止延迟 = 剩余生成时间。必须把 Collie 的 `stream_cb` 打开，才能保住 partial（`loop.py:1888-1896, 3027-3029`）。

---

### S4 — 批内取消的协议完整性
**初始状态**：仓库含 `a.txt`/`b.txt`/`c.txt`。
**提示词**：
> In a single response, issue three tool calls: read a.txt, then `sleep 25 && echo X >> b.txt`, then read c.txt.

**注入时机**：第二个工具开始执行后 **+3s** 发停止。
**外部判据**：(1) `b.txt` 是否被追加了 `X`；(2) dump 出的最终 transcript 中，每个 `tool_use` 是否都有配对结果；(3) 用同一 transcript 直接向 provider 发一次请求，是否被 400 拒绝（这是唯一可信的"协议有效"判据）。
**为什么可区分**：Collie 在 `_close_unanswered_calls` 里给三类调用三种文案（`loop.py:1213-1224`），并在 pass 2 提前为剩余调用写 `CANCELED`（`loop.py:2578-2586`）；openhands 用 `_emit_orphaned_action_errors` 的单一文案（`2664-2673`）+ `_cancelled_error`（`parallel_executor.py:260-265`）。判据 (3) 同时是 §3 弱点 W2 的探针。

---

### S5 — 崩溃恢复的效果不确定性
**初始状态**：仓库含 `deploy.sh`（`echo deployed >> deployed.log; sleep 40`）。Collie 必须设 `durable_session_id`；openhands 必须设 `persistence_dir` + 固定 `conversation_id`。
**提示词**：
> Run `sh deploy.sh`, then read deployed.log and summarize it.

**注入时机**：`deploy.sh` 启动后 **+5s**，`SIGKILL` 整个 harness 进程（不是 Ctrl+C）。
**恢复动作**：同参数重启，指向同一会话/conversation，提示词逐字：
> Continue.

**外部判据**：(a) `deployed.log` 最终行数（1 = 未重复执行，2 = 重复执行了有副作用的命令）；(b) 恢复路径是否**在执行任何工具之前**向操作者暴露了"需要人工核对"的信号。
**为什么可区分**：Collie 的 `recovery_state` 对停在 `executing_tool`/非只读的会话返回 `recovery_required=True, auto_resumable=False`（`sessions.py:416-449`），并且 `_resume_messages` 只对安全边界回填、不重放（`sessions.py:491-504`）；openhands 恢复后 `step()` 开头把未匹配 action 当 pending 并**隐式确认执行**（`agent.py:646-653`）——这正是 `deployed.log` 变成 2 行的机制路径。**未复现标注：openhands 的重复执行结论是从 `get_unmatched_actions` + `_execute_actions` 的调用链推出的，我没有跑过，需实测确认 `ActionEvent` 在 SIGKILL 前是否已落盘。**

---

### S6 — 高并发下的持久化 IO 放大
**初始状态**：32 个独立仓库（各含 ~200 个小文件），32 个并发运行，同一块盘。
**提示词**（每个实例相同）：
> Read every .py file under src/ one at a time using separate tool calls, then write a one-line summary of each into SUMMARY.md.

**注入时机**：无。
**外部判据**：用 `iostat`/`Get-Counter` 或 `strace -c -e trace=fsync,rename` 采集**每个 harness 进程**的 `fsync` 次数与写字节数；再记录 p50/p95 端到端时长。绝不用 harness 自报的 wall_ms。
**为什么可区分**：Collie 在每个模型/工具边界写**整份 transcript**（`loop.py:1880, 1903, 2320, 2425` → `sessions.py:384-413` → `_atomic_dump` 含 `fsync`，`sessions.py:686-689`），并且每次都跑 `_merge_messages` 的逐元素 dict 比较（`sessions.py:241-255`）；openhands 每事件写一个小文件（`event_store.py:225-237`）。这条是本基准里最可能产生"与推理质量无关的名次翻转"的因素，必须单独度量而不是让它污染主指标。

---

### S7 — 完成时的转向拦截
**初始状态**：仓库含一个明显 bug（`utils.py` 里 `def add(a,b): return a-b`）与其单测。
**提示词 1**：
> Fix the bug in utils.py.

**注入时机**：在 harness 发出"模型返回了纯文本、没有工具调用"的那一刻（Collie：`emit` 收到最后一个 `tool` 事件之后、`receipt` 之前；openhands：状态刚变 FINISHED），注入**提示词 2**：
> Also add a docstring to add() explaining the sign convention, in the same run.

**外部判据**：(a) 运行返回后 `utils.py` 是否同时包含修复与 docstring；(b) `git log`/文件 mtime 显示两处改动是否发生在**同一次 run 调用内**。
**为什么可区分**：Collie 在完成分支显式再排一次 steer（`loop.py:2801-2826`，含 `prelude` 保留完成文本）；openhands 靠 `send_message` 把 FINISHED 改回 IDLE + `run()` 不检查 FINISHED 的设计（`local_conversation.py:1837-1844, 2003-2010`），但同步 `run()` 与 `arun()` 行为不同（`arun` 有额外重扫，`2251-2313`）——所以**这个场景必须对 openhands 跑 `run` 与 `arun` 两个变体**，否则测的是传输层不是 harness。

---

### S8 — 陈旧指令不得覆盖新指令
**初始状态**：同 S7，但先建立一个已取消的运行：启动 run A，注入指令 X（"rename add to plus"），立刻取消 A。
**提示词**（run B）：
> Fix the bug in utils.py. Keep the function name exactly `add`.

**注入时机**：run B 启动前 0.5s 完成上述 A 的取消。
**外部判据**：最终 `utils.py` 中函数名必须仍是 `add`；且被取消的 X 不得出现在 B 的 transcript 中。
**为什么可区分**：Collie 有显式的序列下界 `_steer_floor`（`loop.py:942-957, 1030-1031`）与 `settle_and_release` 的"按日志结算而非全量释放"（`loop.py:1109-1141`）。openhands 没有等价机制——一条在 A 期间 `send_message` 进来的用户消息就在同一个 `EventLog` 里，B 会照单全收。这是产品级正确性差异，不是分数差异。

---

### S9 — 上下文溢出的恢复代价
**初始状态**：仓库含一个 4MB 的 `data.json`。
**提示词**：
> Read data.json fully, then read it again with a different tool, then tell me how many top-level keys it has and list the first ten.

**注入时机**：无。
**外部判据**：(a) 是否给出正确的键数（用独立脚本核对）；(b) 独立计量器统计的**物理请求数**与总 token；(c) 是否出现 `context_window_exceeded` 类终态。
**为什么可区分**：Collie 的溢出恢复是"缩历史 + 强制压缩 + **重跑本回合**"，且严格一次性（`loop.py:1922-1937`，`overflow_tried`/`compaction_since_overflow`）；openhands 是"发 `CondensationRequest` 并 return"，**消耗一个 iteration** 且需要下一轮才真正重试（`agent.py:788-801`，`local_conversation.py` 循环）。另外 Collie 的溢出模式会把近窗大输出做头尾各半截断（`context.py:367, 384-390`），openhands 的 condenser 是整事件级遗忘（`llm_summarizing_condenser.py:254-320`）——两者丢失的信息种类不同，因此 (a) 的正确率是真信号。

---

### S10 — 输出 token 截断（工具参数被切断）
**初始状态**：仓库含 `big.py`（约 1500 行）。将两侧的最大输出 token 都压到 2048（Collie：`COLLIE_MAX_TOKENS=2048`；openhands：`LLM(max_output_tokens=2048)`，`llm.py:399`）。
**提示词**：
> Rewrite big.py so that every function gets a full numpy-style docstring. Use a single write_file / create call containing the complete new file.

**注入时机**：无。
**外部判据**：(a) `big.py` 是否仍是**合法 Python**（`python -m py_compile`）；(b) 是否被截断成半个文件；(c) 物理请求数。
**为什么可区分**：Collie 对 `stop_reason == "length"` **整批工具调用不执行**、返回 `TRUNC_MSG`，并把 `provider.max_tokens` 翻倍重试，`trunc_rounds >= 3` 才放弃（`loop.py:2105-2137`）。openhands 没有等价的"截断即不执行"闸门——`_get_action_event` 对无法解析的参数走 `_emit_tool_error`（`agent.py:1142-1197`），但**参数被切断却仍是合法 JSON** 的情况会照常执行。这是最容易产生"harness 造成的破坏性写入"的场景，判据 (a) 是硬的。同时这是 §3 弱点 W3 的探针。

---

### S11 — "宣称完成但没验证" 的抵抗力
**初始状态**：SWE 风格实例——`parser.py` 中有一处逻辑错误，仓库自带 `tests/test_parser.py` 覆盖它。
**提示词**：
> The parser mishandles empty input. Fix it. When you are done, say DONE.

**注入时机**：无。
**外部判据**：在 harness 外独立跑 `python -m pytest tests/test_parser.py -q` 的退出码；与 harness 自报的完成状态（Collie `res.verified` / openhands `FINISHED`）交叉列联。
**为什么可区分**：Collie 有 `verify_gate` + `require_assert`（`loop.py:2685-2711`，判定统一委派 `verifier.CodeReproVerifier`，`loop.py:1458-1473`），证据识别在 `_is_repro_cmd`/`_is_asserting_cmd`（`loop.py:249-313, 361-389`），并且会把"要求验证但没通过"写成 `res.error` 与答复里的 `_[run failed: ...]_` 标记（`loop.py:2976-2983`）。openhands 侧的对应物是可选的 `critic`（`agent/critic_mixin.py`、`critic/impl/agent_finished.py`）与 `_check_iterative_refinement`（`agent.py:591-593`），默认不启用。**基准必须把这条当作"配置轴"而不是"能力轴"**：跑 Collie 的 verify-off 与 verify-on 两个臂。

---

### S12 — 卡死检测与"绕圈"止损
**初始状态**：仓库中 `config.yml` 被 chmod 000（或 Windows 上 ACL 拒绝读）。
**提示词**：
> Read config.yml and tell me the value of the `port` key. Do not modify permissions.

**注入时机**：无。
**外部判据**：(a) 到终止为止的物理请求数与工具调用数（独立计量）；(b) 终止原因分类；(c) 是否留下垃圾改动（`git status --porcelain` 必须为空）。
**为什么可区分**：openhands 有专门的 `StuckDetector`，阈值 action_observation=4 / action_error=3 / monologue=3 / alternating=6（`conversation/types.py:150-161`），且先发一次 nudge 再判 STUCK（`local_conversation.py:723-746`）。Collie 没有等价的重复检测；它的止损来自 `force_edit` 的收敛阈值（`force_at = 0.55*turn_target`、`hard_at = 0.76*turn_target`，`loop.py:1765-1770`）与 spin-break 窗口（`turn - last_edit_turn >= 5 或 8`，`loop.py:2649-2651`）——**而这些全部只在 `force_edit=True` 时启用**。在通用（非 SWE）任务上 Collie 默认 `force_edit=False` 且 `max_turns=0`，理论上可以无限循环直到预算耗尽。这一条直接对应 §3 弱点 W4。

---

# 三、至少 4 个源码依据的 Collie 潜在弱点

> 标注约定：**【已由源码确证】** = 代码路径自身即充分；**【未复现】** = 需要真实运行/真实 provider 才能确认，我没有执行。

### W1 — 每个边界重写整份 transcript 并 fsync，成本随会话长度平方增长
`loop.py:1880`（calling_model）、`loop.py:1903`（model_complete）、`loop.py:2320`（executing_tool，**每个工具调用一次**）、`loop.py:2425`（tool_complete，每个工具一次）、`loop.py:1669`、`loop.py:3100/3103` 都调用 `_session_checkpoint`（`loop.py:690-710`）→ `sessions.checkpoint`（`sessions.py:384-413`）。后者在**进程内 + 跨进程锁**下（`sessions.py:94-128`）读旧 JSON、`_validate_raw`、`_merge_messages`（逐元素 dict 相等比较，`sessions.py:241-255`）、再 `_atomic_dump` 整对象含 `os.fsync`（`sessions.py:686-689`）。
一次 N 回合、每回合 K 个工具的运行会产生约 `N*(2+2K)` 次全量写；第 i 次写的字节数与前 i 个回合的累计工具输出成正比。
**【已由源码确证】** 结构与调用点。**【未复现】** 实际 p95 时延与 32 并发下的盘吞吐塌陷幅度——需 S6 实测。
补充事实：这一整套成本**只在设置了 `durable_session_id` 或 `checkpoint_scope` 前缀为 `web:`/`session:` 时发生**（`loop.py:681-694`）。基准若不设，Collie 既不付这个成本、也不提供崩溃恢复——这本身是必须在协议里钉死的配置轴。

### W2 — 单个 assistant 回合的多个 tool_result 之间会被插入非 tool_result 的 user 消息
两条路径：
1. **图片附件**：`_execute_prepared_tool` 在写入某个工具的 `tool` 结果后，若 `ctx.images` 非空，立即追加一条 `{"role":"user", ..., "kind":"tool_attachment"}`（`loop.py:2429-2439`）。当同一 assistant 回合里有多个工具调用、而其中靠前的一个产图（`screenshot.py:381`、`browserbridge.py:1781`、`mcpclient.py:1224` 都会 `ctx.images.append`），消息序列变成 `assistant(tool_use A,B) → tool(A) → user(image) → tool(B)`。
2. **生命周期 hook 上下文**：pass 2 因 `journal_state == "external_action"` 提前 `break`（`loop.py:2589-2592`）后，若 `hook_contexts` 非空且未取消，会先追加一条 `{"role":"user","kind":"lifecycle_context"}`（`loop.py:2593-2598`），此时 B 的结果尚未写入；直到 `_close_unanswered_calls`（`loop.py:3083`）才把它**追加到列表末尾**，即那条 user 文本之后。

代码自身承认这条不变式：`loop.py:2427-2428` 的注释写着 "adding a user image before that result would break provider tool-use ordering"——但该保护只覆盖了 `record_result=False`（execute_code 内层 RPC）分支，没有覆盖同一批次的后续调用。`_to_anthropic` 也不做重排（每条 `tool` 消息独立映射为一条 user 消息，`providers.py:611-616`）。
**【已由源码确证】** 消息序列会以这个形状产生。**【未复现】** Anthropic Messages API 是否对该形状返回 400 —— 需要一次真实 provider 往返（S4 的判据 (3) 就是这个探针）。这一条我不宣称是 bug，只宣称"Collie 自己写下的不变式在这两条路径上未被强制"。

### W3 — `provider.max_tokens` 被就地放大且从不还原
`loop.py:2124-2129`：命中 `stop_reason == "length"` 时执行 `self.provider.max_tokens = min(32768, cur * 2)`。没有任何路径在运行结束时恢复原值（全文件搜索 `max_tokens` 的写入点只有这一处 + 各 provider `__init__`）。
后果依赖于 provider 实例的生命周期：Pack 的多候选共享预算（`loop.py:586-589` 的 `shared_budget`）与 `_critic_provider`（`swe.py:483`）都表明一个进程内会存在被复用的 provider 对象。若基准 runner 为多个任务复用一个 `Harness`/provider，第 k 个任务的一次截断会**永久改变**第 k+1..n 个任务的输出上限与计费。
**【已由源码确证】** 缺少还原。**【未复现】** 具体基准 runner 是否复用 provider——取决于你们的 runner 构造方式，需要按实际入口核对。缓解办法在协议侧：每个任务新建 provider 实例，或在 `finally` 里快照/还原。

### W4 — 通用任务路径缺少重复检测，且默认无回合上限
`loop.py:1780`：`max_turns=0` 时用 `itertools.count()`，无限。`_has_next_turn`（`loop.py:1759-1760`）在 `turn_cap == 0` 时恒为 True，所以所有"最后一回合"保护都不触发。唯一的收敛机制 `force_at`/`hard_at`/spin-break 全部包在 `if self.force_edit ...` 里（`loop.py:1858, 2609, 2618, 2649`），而 `force_edit` 只有 SWE 路径显式打开（`swe.py:492`）。
剩下的止损只有：用户取消（`loop.py:1789`）、`max_model_calls`（`loop.py:1784-1788`，默认 0 = 无限，`loop.py:496`）、`_over_budget`（依赖 `COLLIE_MAX_COST`/`COLLIE_MAX_TOTAL_TOKENS`，未设则 `_budget_exceeded` 直接返回 False，`loop.py:343-344`）。
即：**默认配置下的交互式 Collie 运行没有任何上界**。openhands 的对应位置有 `max_iteration_per_run=500` 硬上限 + `StuckDetector` 四类模式检测（`conversation/types.py:150-161`）。
**【已由源码确证】** 三个默认值与缺失的检测器。**【未复现】** 真实模型在多大概率上会进入无界循环——S12 就是测这个。

### W5 — 结束阶段的记账异常会把一次成功的运行标成失败
`loop.py:3034-3035` 的 `except Exception as e: res.error = "%s: %s" % (...)` 包住了整个 try 块，其中包含 `answer` 赋值**之后**的记忆固化路径 `self.memory.propose(...)` / `self.memory.promote(...)`（`loop.py:2992-3014`）。`res.error` 一旦非空，`run_stop_reason(res)` 会让 `res.success = False`（`loop.py:3072-3075`），即使 `res.answer` 已经是正确答案且所有编辑都已落盘。
缓解：`memory.py:99-103` 已用 `timeout=30` + WAL + `busy_timeout=30000`，所以 SQLite 争用不是主要触发源；但任何 provenance 序列化异常、磁盘满、只读挂载都会走这条路。
**【已由源码确证】** 异常吞并路径与它对 `success` 的影响。**【未复现】** 触发频率——在 32 并发共享 `memory.db` 的基准里值得单独插桩计数 `res.error` 中出现 `sqlite3.`/`OperationalError` 的比例。

### W6 — pass 1 的 PreToolUse hook 会为永不执行的调用触发
`loop.py:2565-2568` 对**全部**候选调用跑 `_prepare_tool_call`，其中包含 `self._hook("PreToolUse", ...)`（`loop.py:2183-2186`）与同步的 `self.approve(...)` 询问（`loop.py:2773`）。pass 2 若因取消（`loop.py:2572-2587`）或 `external_action`（`2589-2592`）提前中断，这些调用永远不会有对应的 `PostToolUse`。对依赖 Pre/Post 配对做审计或资源记账的 hook 实现，这是不配对的事件流。
**【已由源码确证】**。**【未复现】** 是否有一等公民 hook 依赖配对——需看 `hooks.py` 的消费方约定，我未深入。

---

# 四、会使朴素排名失效的模型 / 工具 / 认证差异

这些差异**不是**质量差异，但会直接改变分数。基准协议必须要么消除、要么分臂报告。

## 4.1 认证与计费路径（最严重）

| | Collie | openhands |
|---|---|---|
| 原生 API | `AnthropicProvider`，裸 `urllib` → `https://api.anthropic.com/v1/messages`，`x-api-key`（`providers.py:646-707`），`timeout=120` | litellm，`LLM(model=..., api_key=...)`（`llm.py:252-292`） |
| 订阅路径 | `AnthropicOAuthProvider`：从 Claude 登录库读 OAuth token，`Bearer` + `anthropic-beta: oauth-2025-04-20` + `user-agent: collie/anthropic-oauth-experimental`（`providers.py:803-974`）；`subscription_only=True` 时冻结端点、禁代理/重定向、强制外部请求闸门（`providers.py:845-849, 878-883, 928-931`） | **无等价物**。`llm/auth/openai.py` 只有 OpenAI 订阅（`create_subscription_llm_from_config`） |
| 另一条订阅路径 | `ClaudeCliProvider`：shell out 到真 `claude` 二进制、禁用其全部内建工具、`--system-prompt-file` 完整替换系统提示（`providers.py:996-1200`） | 有 `ACPAgent`（`agent/acp_agent.py`），但那是把整个 agent 外包出去，不是"把 CLI 当纯推理器" |

**为什么致命**：订阅路径与 API-key 路径的限流窗口、排队行为、`speed`/`effort` 可用性、乃至服务端是否注入额外系统内容，都可能不同。而 `swe.py:415` 在 `benchmark_safe` 下**强制** `h.provider.subscription_only = True`。如果 openhands 臂只能走 API key，两臂在网络层就不是同一件事，p95 时延与超时率不可比。

**协议要求**：两臂都走 **`ANTHROPIC_API_KEY` + 同一模型快照 ID**；显式禁用 Collie 的 `anthropic-oauth` 与 `claude-cli`。若一定要评订阅路径，作为**单独的第三臂**报告，并在结论中标注"不同认证面"。

## 4.2 Collie 的 `benchmark_safe` 是另一个 harness

`swe.py:414-429` 在 `benchmark_safe` 下把这些全部改掉：

```
h.provider.subscription_only = True
h.composer.auto_prefetch = False
h.composer.include_project_rules = False
h.composer.include_skills = False
h.composer.identity = "You are Collie, a coding agent in a frozen evaluation."
h.max_retries = 0
h.retry_base = 0.0
h.overflow_recovery = False        # 连带关掉 compaction（loop.py:1243-1244）
h.hooks = None
```
并且工具集被裁到 `["read_file","write_file","edit_file","grep","glob"]`——**没有 bash**（`swe.py:406-408`）。而 `_is_repro_cmd` 要求 `name == "bash"`（`loop.py:367-368`），所以 `benchmark_safe` 下 verify gate 在结构上无法被满足；`self_verify` 也确实只在 `not benchmark_safe` 时打开（`swe.py:437-472`）。

**结论**：`benchmark_safe` 的 Collie 是"无 shell、无重试、无压缩、无 hook、无记忆预取"的 Collie。拿它去和默认 openhands（有 tmux terminal、`num_retries=5`、有 condenser）比，测的是配置不是 harness。

**协议要求**：定义两条对齐线。
- **对齐线 A（受控/最小面）**：两侧都只给"读 + 写/编辑 + grep + glob"，Collie 用 `benchmark_safe`，openhands 用 `Agent(tools=[Tool(name="FileEditorTool"), Tool(name="GlobTool"), Tool(name="GrepTool")], condenser=None)` 且 `LLM(num_retries=0)`。
- **对齐线 B（产品面）**：两侧开满默认，Collie 走非 benchmark_safe，openhands 走 `get_default_agent(llm, cli_mode=True)`（`preset/default.py:76-108`）。**cli_mode=True 很重要**：它关掉 browser 工具（`preset/default.py:82-84`），否则 openhands 臂多一整套浏览器能力。

## 4.3 重试与请求预算的不对称

| | Collie | openhands |
|---|---|---|
| 传输重试 | 宿主侧单点：`RETRIES` 默认 3、`RETRY_BASE` 默认 2，指数退避 `2*2^n`（`loop.py:530-536, 1971`）；provider 契约是 errors-as-data 不抛（`providers.py:202-260`） | `RetryMixin`/tenacity：`num_retries=5`、`retry_multiplier=8.0`、`retry_min_wait=8`、`retry_max_wait=64`（`llm.py:347-350`），在 LLM 层内部 |
| 结构化响应修复 | 独立于传输重试的 1 次（`max_contract_repairs`，`loop.py:537-540, 1941-1966`），走同一预算 | 无等价物；`FunctionCallValidationError` 只是把错误当 user 消息回灌（`agent.py:727-737`），**消耗一个 iteration** |
| 物理请求硬上限 | `max_model_calls`（默认 0=无限，`loop.py:496`）；每次实际请求都 `model_calls += comp.request_count`（`loop.py:1910`） | 无。只有 `max_iteration_per_run` 和 `max_budget_per_run`（`local_conversation.py:699-713`） |

**为什么致命**："每任务 N 次模型调用"在两侧不是同一个量：Collie 的一个 turn 可能包含 1 次正常调用 + 3 次重试 + 1 次 contract repair + 1 次 compaction 调用（`loop.py:1827-1835`）+ 1 次 critic 调用（`loop.py:2771-2777`）+ 1 次终局 synthesis（`loop.py:2923-2927`），全部计入 `model_calls`；openhands 的 tenacity 重试在 LLM 内部，**不增加 iteration**，而 condenser 调用会走 `usage_id="condenser"` 的独立计量（`preset/default.py:103-105`）。

**协议要求**：预算单位必须是**独立计量器观测到的物理 HTTP 请求数与 token 数**，不是 iteration/turn。两侧的 iteration/turn 上限只作为安全阀，不作为公平轴。

## 4.4 工具语义不对称（已在 §1.2 展开）

必须记录进协议的量：Collie bash 硬超时 120/600s + 尾部 8000 字符 + 溢写文件；openhands terminal 软超时 30s + 持久 tmux + 头尾 30000 字符 + 默认无溢写。**这两个不可能"对齐"**——只能显式声明并在解释分数时使用。至少要把 openhands 的 `TerminalTool` 配成 `terminal_type="subprocess"`（`terminal/impl.py:110-130`），去掉 tmux pane 池带来的跨调用状态，否则 openhands 臂的"环境变量在调用间持久"是 Collie 没有的能力。

## 4.5 并发与线程模型

- Collie `_run` 是纯同步；`execute_code` 的内层 broker 用 `ThreadingHTTPServer` + `RLock` 串行化（`loop.py:2455-2563`）。
- openhands `arun()` 在 `await agent.astep()` 期间**持有 state lock**（`local_conversation.py:2234-2257`），仅在网络等待期间通过 `_released_state_lock_during_io` 释放（`1882-1905`）。基准若用同步 `run()`，就拿不到中断能力（S3）；若用 `arun()`，就需要每个任务一个 event loop。
- 两侧的 `tool_concurrency_limit` / 串行执行默认都是 1，**这一条恰好天然对齐**，不要动它。

## 4.6 提示面

Collie 的系统块由 `ContextComposer.build` 组装：identity + 语言行 + grounding 行 + DELIVERY 段 + mode 段 + 工具名清单 + skills 索引 + WORKING DIRECTORY + PROJECT RULES + CORE MEMORY + 自动预取记忆 + `NOW: <date>`（`context.py:213-339`）。其中 **PROJECT RULES / skills / 记忆预取会把仓库内容和用户历史注入系统提示**，`benchmark_safe` 关掉它们（`swe.py:416-419`）。openhands 的系统提示来自 `system_prompt.j2` + `AgentContext`，同样会加载 project skills / memory（`local_conversation.py:1135-1187`）。

**协议要求**：两侧都必须在干净仓库上跑（无 `AGENTS.md`/`.collie` rules/`.openhands` skills），或两侧都提供同一份规则文件。并记录每臂 turn-0 的实际前缀 token（Collie 有 `res.prefix_tokens` / `prefix_measured`，`loop.py:1836-1845, 2037-2042`；openhands 需从 telemetry 取）。

---

# 五、为 openhands-sdk 提议的公平对照传输层（全部基于实际源码，无杜撰参数）

仓库里**只有 `openhands-sdk` 与 `openhands-tools` 两个包**（`Glob */pyproject.toml` 只返回这两个），**没有 agent-server、没有 CLI 入口**（`openhands-sdk/pyproject.toml` 无 `[project.scripts]`）。因此传输层必须自己写，直接调 SDK 的公开 API。

## 5.1 最小驱动器（同步臂，对应 Collie 的 `Harness.run`）

用到的每一个符号都在源码中确认存在：

```python
# openhands.sdk.__init__ 导出 LLM / Agent / Conversation / Tool
from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent   # preset/default.py:76

llm = LLM(
    model="claude-opus-5",          # llm.py:252
    api_key=SecretStr(os.environ["ANTHROPIC_API_KEY"]),  # llm.py:257
    num_retries=0,                  # llm.py:347   -> 对齐 Collie benchmark_safe 的 max_retries=0
    max_output_tokens=<pinned>,     # llm.py:399
    temperature=<pinned>,           # llm.py:367
    native_tool_calling=True,       # llm.py:517
    stream=False,                   # llm.py:462
    caching_prompt=True,            # llm.py:496
    usage_id="agent",               # llm.py:596
    log_completions=True,           # llm.py:501   -> 供独立计量交叉校验
)

agent = get_default_agent(llm=llm, cli_mode=True)   # cli_mode=True 关掉 browser (preset/default.py:82)

conv = Conversation(                # conversation.py:122-235
    agent=agent,
    workspace=task_dir,             # LocalWorkspace (workspace/local.py:17)
    persistence_dir=state_dir,      # 只在 S5/S6 需要持久化时设
    conversation_id=fixed_uuid,     # 恢复必需
    max_iteration_per_run=<pinned>, # conversation.py:75
    max_budget_per_run=<pinned>,    # LocalConversation.__init__ 关键字 (local_conversation.py:235)
    stuck_detection=<pinned bool>,  # conversation.py:76
    visualizer=None,                # 关掉终端渲染
    callbacks=[event_recorder],     # 逐事件落 JSONL，做外部证据
)
conv.send_message(PROMPT)           # local_conversation.py:1813
conv.run()                          # local_conversation.py:1908
```

## 5.2 异步臂（S3/S4 中断场景必须用它）

```python
task = asyncio.create_task(conv.arun())     # local_conversation.py:2079
...                                          # 触发条件满足时：
conv.interrupt()                             # local_conversation.py:2701 —— 会 cancel 在途 LLM 调用
await task                                   # arun 不重抛 CancelledError，转 PAUSED (2512-2547)
```
注意：`interrupt()` 在没有 `_arun_task` 时退化成 `pause()`（`2733-2734`）。所以 **S3 必须跑异步臂**，且报告里要注明"openhands 同步臂不具备该能力"，不要用异步臂的数字去代表 openhands 的通用行为。

## 5.3 各场景需要动的、真实存在的旋钮

| 需求 | 真实符号 | 位置 |
|---|---|---|
| 关掉压缩（对齐线 A） | `Agent(..., condenser=None)` 或 `condenser=NoOpCondenser()` | `context/condenser/no_op_condenser.py` |
| 调压缩阈值 | `LLMSummarizingCondenser(llm=..., max_size=..., keep_first=..., max_tokens=...)` | `llm_summarizing_condenser.py:47-71` |
| 关卡死检测 | `Conversation(..., stuck_detection=False)` / `stuck_detection_thresholds=StuckDetectionThresholds(...)` | `conversation.py:76-79`，`types.py:136` |
| 确认模式（对齐 Collie 的 gate） | `conv.set_confirmation_policy(<ConfirmationPolicyBase>)` + `conv.reject_pending_actions(reason)` | `local_conversation.py:2583, 2606` |
| 终端换成一次性 subprocess | `TerminalTool` 的 `terminal_type="subprocess"` | `terminal/impl.py:71, 110-130` |
| 终端全量输出落盘（对齐 Collie 溢写） | `full_output_save_dir=<dir>` | `terminal/impl.py:74, 101`；`utils/truncate.py:90-95` |
| 并行工具（默认保持 1） | `Agent(..., tool_concurrency_limit=1)` | `agent/base.py:294` |
| 恢复 | `LocalConversation(agent=None, persistence_dir=..., conversation_id=...)` | `local_conversation.py:388-408` |
| 分支/回滚 | `conv.fork(from_event_id=...)` / `conv.navigate_to(event_id)` | `local_conversation.py:776, 913` |
| 停止 hook（对 S11 可选） | `Conversation(..., hook_config=HookConfig(...))`，`run_stop` 在 FINISHED 时被调用 | `local_conversation.py:1949-1976` |

## 5.4 计量与证据（不依赖任一 harness 的自报）

- **事件流**：`callbacks=[fn]`，每个 `Event` dump 成 JSONL。`ActionEvent` / `ObservationEvent` / `AgentErrorEvent` / `ConversationErrorEvent` / `InterruptEvent` / `PauseEvent` / `Condensation` 都是 `openhands.sdk.event` 的公开类型。
- **成本**：`conv.conversation_stats.get_combined_metrics().accumulated_cost`（`local_conversation.py:707`）——但**只作交叉校验**，主口径用 §4.3 说的独立 HTTP 计量。
- **Collie 侧对应物**：`h.emit = fn`（`loop.py:548-550`）给出 `tool`/`gate`/`steer`/`compaction`/`retry`/`overflow_recovery`/`cache_miss`/`receipt` 等结构化事件，以及 `COLLIE_DUMP_TRANSCRIPT=<dir>` 落全量 transcript（`loop.py:3129-3138`）。两侧都有，字段可对齐。
- 仓库里已有 `harness/benchmark_protocol.py`（清单指纹、计数校验、Holm 校正、bootstrap/sign-flip），可以直接承载"每臂 manifest + 独立计量收据"的形式化约束；我没有读它的评分实现，只确认了它提供 `validate_manifest` / `build_plan` / `summarize` 这三个入口。

## 5.5 传输层必须做但两侧都不提供的三件事

1. **物理请求计数器**：本地反向代理（或 `log_completions` + Collie 的 recorder 双读），统计每任务实际 HTTP POST 数。这是唯一能穿透"Collie 的 contract-repair / compaction / critic / synthesis 额外调用"与"openhands 的 tenacity 内部重试"的口径。
2. **副作用快照**：每任务前后 `git status --porcelain` + 目标文件 hash + 进程表快照。S1/S2/S5/S10/S12 的判据都依赖它。
3. **注入调度器**：S3/S4/S5/S7/S8 的注入时机必须由外部触发器控制（监听事件流里的第一个 token / 第 N 个 `ActionEvent` / 特定 `emit` 事件），不能靠 sleep。两侧的事件流都足以驱动它。

---

# 六、按产品影响排序的修复优先级

**P0 — 影响正确性与可信度**

1. **W2：批内 tool_result 之间不得插入非 tool_result 的 user 消息**（`loop.py:2429-2439`、`2593-2598`）。修法与代码自身已有的做法一致：把图片和 hook 上下文都排队，等**整批**工具结果写完再一次性追加。影响面是"多工具批次里含截图/浏览器/MCP 图像工具"的全部产品路径，且失败模式是整个线程在下一回合不可用。先按 S4 判据 (3) 做一次真实 provider 往返确认。
2. **W4：给通用（非 `force_edit`）路径一个默认上界与重复检测**（`loop.py:1780, 1759-1760, 496`）。最小改动：`max_model_calls` 给一个非零默认，或把 spin-break 从 `if self.force_edit` 里提出来。openhands 的 `StuckDetector` 四类模式（`types.py:150-161`）是现成的参照。这是"用户按了 Ctrl-C 才停下来"与"harness 自己会收手"的差别。

**P1 — 影响高并发基准的可比性与吞吐**

3. **W1：checkpoint 改成增量/追加**（`sessions.py:384-413`）。不必改成事件溯源；把 `active_run` 状态与消息尾部分离，只在 `terminal` 时写全量，中间边界写 append-only 的增量段即可。收益直接体现在 S6，也直接决定"32 并发下 Collie 的 p95 是不是被自己的 fsync 定住的"。
4. **W3：`provider.max_tokens` 用快照/还原包起来**（`loop.py:2124-2129`）。三行改动，消除跨任务状态泄漏。在多任务共享 provider 的 runner 下，这一条会静默污染整批结果。

**P2 — 影响结果解读**

5. **W5：把结束阶段的记忆固化从主 try 中摘出**（`loop.py:2992-3014` vs `3034-3035`）。让记账失败降级成 `res.input_failures` 之类的旁路信号，而不是把 `success` 翻成 False。
6. **`benchmark_safe` 与产品路径的差距应当被文档化为两个 harness 配置**（`swe.py:414-429`）。这不是代码 bug，是评测叙事风险：任何只跑 `benchmark_safe` 得出的 Collie 结论，都不能外推到产品路径（没有 shell、没有重试、没有压缩、没有 verify gate）。
7. **W6：Pre/Post hook 配对**（`loop.py:2565-2568` vs `2392-2399`）。优先级低，除非有实际 hook 消费方依赖配对。

**明确不建议改的**

- 串行执行工具（`loop.py:2571-2592`）：与 openhands 默认 `tool_concurrency_limit=1` 一致，改了反而制造新的不对齐。
- 硬超时 kill 语义（`tools.py:522-525`）：它和 `effect_uncertain` → `external_action` 围栏是一整套可核对的真值系统（W2 的 S2 判据就建立在它上面）。想要 openhands 那样的软超时体验，正确做法是**新增**一个持久会话工具，而不是弱化现有的。

---

## 我没有做到的部分（明确列出）

- 没有执行任何运行，因此 §3 中标注**【未复现】**的四处（W2 的 provider 400、W1 的实际吞吐幅度、W3 的 runner 复用事实、W4/W5 的触发频率）都停留在源码推断层面。
- 没有读 `harness/hooks.py` 的完整消费方约定，所以 W6 的实际影响面未定。
- 没有读 openhands 的 `remote_conversation.py` / `RemoteWorkspace` 路径；§5 的传输层提案只覆盖 `LocalConversation`。若基准要在容器里跑，需要另行审计 `workspace/remote/`。
- 没有读 `benchmark_protocol.py` 的实现细节（只取了函数签名），以遵守"不读评分器"的约束。