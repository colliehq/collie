# Collie vs. Codex 执行层审计报告

**审计范围**：`C:/workspace/collie`（Python harness，只读）与 `C:\workspace\collie-product-2026-09-06\references\codex`（Rust，pinned）。未执行任何代码、未联网、未读取凭据/会话/评分器。所有结论为静态源码分析，逐条标注可信度。

**pinned codex 树的重要限制**：`codex-rs/Cargo.toml:2-148` 列出 ~140 个 crate，但实际只签出了 5 个（`app-server`、`core`、`exec`、`protocol`、`tui`，见 `codex-rs/*/BUILD.bazel`）。因此 `utils/cli`（`SharedCliOptions`，即 `--sandbox`/`--full-auto` 等）、`rollout`、`utils/pty`（`DEFAULT_OUTPUT_BYTES_CAP` 的数值）、`app-server-protocol`（`TurnSteerParams` 的 serde 定义）**不可验证**。凡涉及这些，我标注为「pinned 树不可见」。

---

## 一、12 个可复现的判别性场景

统一约定：模型固定 Claude Opus 5（`claude-opus-5`）；两侧都走本地录制/回放代理（不真实联网）；仓库固定为一个带 git 历史的 fixture；外部判据只使用 harness 之外可观测的证据（git 状态、文件哈希、进程表、退出码、协议 JSONL、wall clock）。

---

### S1 · 工具执行中被 SIGKILL 后的恢复语义

**初始状态**：git fixture，`AGENTS.md` 存在。Collie 侧必须设置 `h.checkpoint_scope = "session:<sid>"`（否则 `loop.py:681-688` 的 `_durable_session_id()` 返回 `""`，整条日志/租约链路被关闭 —— 见 §2.6）。Codex 侧用 app-server `thread/start`。

**用户提示**（逐字）：
```
Create a file named marker.txt whose only content is the line DONE, then run: bash -lc 'sleep 30; echo appended >> marker.txt'
```

**注入时机**：外部监控进程轮询，一旦观察到 `sleep 30` 子进程出现在进程表中，等待 3 秒，然后 `kill -9`（Windows：`taskkill /F /T`）整个 harness 进程树的**父进程**（保留子进程孤儿态）。

**外部判据**：
1. 重启同一 session 并发送 `continue`。
2. Collie 期望：`data/sessions/<sid>.json` 的 `active_run.state == "executing_tool"`，`detail.replay_safe` 缺失 → `sessions.py:444-449 _recovery_required()` 为 True → `resume_after_interrupt()`（`sessions.py:518-547`）返回 `blocked=True`，**下一轮拒绝执行**，必须调用 `reconcile_recovery(..., confirmed=True)`（`sessions.py:566-575`）。
3. Codex 期望：`normalize.rs:21-138 ensure_call_outputs_present()` 在构建 prompt 时为未配对的 `FunctionCall` 插入 `FunctionCallOutputPayload::from_text("aborted")`，**自动续跑**，不阻塞。
4. 判据本身：`marker.txt` 的最终字节内容 + 「是否出现重复 append」+ 「人工介入次数」。

**为什么判别**：这是两者产品哲学的正面冲突。Collie 更安全（不重放未知副作用），但在无人值守的高并发 benchmark 里会被直接记为失败——除非评测协议显式提供 reconcile 步骤。

---

### S2 · 运行中被接受的用户输入的持久性（崩溃穿越）

**初始状态**：同 S1，Collie 走 TUI 路径（`tui.py:29-133` 的 `_StdinFeed`，`accepting()` 把非斜杠行交给 durable acceptor → `task_inbox.enqueue`，见 `tui.py:516-532`）。

**用户提示**：
- 初始：`Refactor utils.py to use pathlib throughout.`
- 中途注入（第一次 `edit_file` 完成后 500ms）：`Actually stop at 3 files and write a summary to NOTES.md.`

**注入时机**：注入后 **200ms** 内 `kill -9` harness。

**外部判据**：重启后不发送任何新输入，只发 `/queue`（Collie）或 `thread/queue/list`（codex app-server）。Collie 期望第二条指令仍在 inbox 中（`state=claimed` 或 `pending`）；`_inbox_reconcile()`（`loop.py:871-897`）应把「已进入 transcript 的」判为 consumed，其余 released。Codex 期望**丢失**：`turn_suspension.rs:96-98` 明确写着 "Pending accepted input and interactive waiters live only in this process. Handoff intentionally drops that state"。

**这是 Collie 唯一确凿的架构优势**，值得单独计分。

---

### S3 · 在飞行中的 turn 上 steer（不重启 turn）

**初始状态**：一个会跑 ~40s 的任务。

**用户提示**：
- 初始：`Read every .py file under src/ and summarize each one.`
- 注入（第 2 个工具调用返回后）：`Only summarize the three largest files. Ignore the rest.`

**注入时机**：Collie 通过 TUI stdin 或 web SSE；codex 通过 `turn/steer`。

**外部判据**：
1. **turn 是否被替换**：codex `spawn_task` 会 `abort_all_tasks(TurnAbortReason::Replaced)`（`tasks/mod.rs:277`），而 `steer_input`（`turn_input.rs:519-602`）在 `active_turn` 锁下追加到 pending queue，**不中断**。协议日志中不得出现 `turn/aborted`。
2. **消费点**：codex 在 `regular.rs:78-98` 的循环里，`run_turn`（`turn.rs:334-345`）只有在 `can_drain_pending_input` 为真时才 drain；Collie 在 `loop.py:1804-1821`（turn 开始）和 `loop.py:2801-2826`（模型想结束时拦截）两处。
3. 外部判据：最终 summary 覆盖的文件数 == 3；且注入到"模型第一次引用新指令"之间的工具调用数 ≤ 2。

**已知缺陷（见 §3.4）**：Collie 自带的 codex runner 发的 `turn/steer` **缺 `expectedTurnId`**，在 pinned codex 上会被 `turn_processor.rs:1020-1022` 直接拒绝。若不修，本场景的 codex 侧会假性得 0 分。

---

### S4 · 默认命令超时（10s vs 120s）

**初始状态**：仓库根放一个 `slow_test.sh`，内容 `sleep 25; exit 0`。

**用户提示**：
```
Run ./slow_test.sh and tell me its exit code. Do not modify it.
```

**注入时机**：无。

**外部判据**：
- Codex：`exec.rs:61` `DEFAULT_EXEC_COMMAND_TIMEOUT_MS = 10_000`；`exec_command.rs:310-318` 只有当模型显式传 `timeout_ms` 才覆盖。默认必超时，`exec.rs:68` `EXEC_TIMEOUT_EXIT_CODE = 124`。
- Collie：`tools.py:484-485` 默认 120s、clamp `[1,600]`。默认成功。
- 判据：命令实际 wall time（外部计时）、模型报出的 exit code、以及**模型是否学会重试并加大 timeout**（这才是真正判别 agent 能力的点，而非 harness 常数）。

**必须同时跑 `timeout_ms=30000` 显式变体**，否则测的是常数不是 agent。

---

### S5 · 巨量工具输出的处理

**初始状态**：仓库含 `gen_noise.py`，打印 2,000,000 字符，最后一行是 `SENTINEL=7f3a`。

**用户提示**：
```
Run: python gen_noise.py
Then tell me the exact value of SENTINEL printed at the very end.
```

**外部判据**：答案是否等于 `7f3a`。
- Collie：`tools.py:546-555` 保留**尾部** 8000 字符并把全量 spill 到 `_SPILL_DIR`（`tools.py:419`），首行给出可 `grep`/`read_file` 的路径 → 应当答对。
- Codex：`exec.rs:79` `EXEC_OUTPUT_MAX_BYTES = DEFAULT_OUTPUT_BYTES_CAP`（**pinned 树不可见其数值**，位于 `codex-utils-pty`）；`exec.rs:718-724` 用 `truncate(max_bytes)`（前缀截断）。需实测。
- 追加判据：Collie 侧 8000 字符阈值在 14 条消息后会被 `context.py:364-366` 再压成 240 字符 stub —— 检查第二轮提问 `What was SENTINEL again?` 是否还能答对（预期 Collie 失败，因为 stub 保留的是**头** 240 字符，见 `context.py:383`）。

---

### S6 · 长会话压缩触发点

**初始状态**：空仓库，脚本化连续 30 轮，每轮让 agent 读一个 ~3KB 的文件并复述其中一行。第 1 轮埋入口令：`The project codeword is ORCHID-91.`

**用户提示**（第 30 轮）：
```
What is the project codeword?
```

**外部判据**：能否复现 `ORCHID-91`，以及压缩发生的时点。
- Collie：`compaction.py:175` `threshold_tokens: int = 48_000` 是**固定策略默认值**，模块注释 `compaction.py:165-174` 自认「Collie 没有可信的 per-model 上下文窗口元数据」。对 200K 窗口的 Opus 5，会在 ~24% 窗口处就开始丢弃逐字历史。
- Codex：`session/context_window.rs:52-119` 从 `model_info.resolved_context_window()` × `effective_context_window_percent` 和 `auto_compact_token_limit()` 推导。
- 判据：口令召回率 + `compaction`/`auto_compact` 事件的首次出现轮次 + 累计 token。**必须同时跑 `COLLIE_COMPACT_TOKENS=160000` 变体**，否则是在测默认常数。

---

### S7 · 验证门的假阴性（Collie 特有）

**初始状态**：SWE 风格 fixture（`swe.py` 路径，非 `benchmark_safe`），`COLLIE_ASSERT_VERIFY` 保持默认 → `swe.py:465-470` 强制 `verify_gate=True, require_assert=True`。

**变体 A 提示**：
```
Fix the off-by-one in src/slicer.py::window(). Verify with: python -m pytest -q tests/test_slicer.py 2>&1 | tail -5
```
**变体 B**：设置 `COLLIE_E2E_IMAGE`（使 `swe.py:411-412` 加入 `run_in_env`），提示：
```
Fix the off-by-one in src/slicer.py::window(). Verify your fix using run_in_env.
```

**外部判据**：`git diff` 是否包含正确修复（由独立 grader 用真实测试判定），**与** Collie 自报的 `res.error` / 结尾 marker 是否一致。
- 预期 A：`loop.py:189-246 _has_unsafe_test_shell_control()` 在第 228 行遇到 `|` 即返回 True → 该命令不算证据 → 空转 `verify_max` 轮 → `loop.py:2976-2983` 强制写入 `res.error = "verification required but no executed post-edit assertion passed"`，即使 patch 正确。
- 预期 B：`loop.py:369` `if name != "bash": return False` —— `run_in_env`（`tools.py:594`）产生的证据**完全不计入**，同样强制失败。
- 这是把「正确的补丁」判成失败的机器，属于排名污染源，必须在评测里单独标注。

---

### S8 · 输出 token 上限截断下的工具参数安全

**初始状态**：把 provider 的 `max_tokens` 压到 1024（`COLLIE_MAX_TOKENS=1024`；codex 侧用等效的 model 配置）。

**用户提示**：
```
Rewrite src/config.py entirely: replace the whole file with a version that adds type hints to every function. Use a single write_file call.
```

**外部判据**：`src/config.py` 是否被写成**截断的、语法非法的**内容。
- Collie：`loop.py:2105-2137`——`stop_reason == "length"` 时**整批工具调用一律不执行**，回填 `TRUNC_MSG`（`loop.py:37-39`），并在 `loop.py:2124-2129` 把 `provider.max_tokens` 翻倍（上限 32768）重试，`trunc_rounds >= 3` 放弃。预期：文件永不被写成半截。
- Codex：需实测其 `max_output_tokens` 达限时是否也丢弃整批 tool call（pinned 树里我未找到等价的「整批作废」逻辑；**未复现**）。
- 副作用判据（见 §3.3）：run 结束后再发第二个提示，检查 Collie 的第二次请求是否仍用被翻倍的 `max_tokens`（应用 provider 的实际请求体核对）。

---

### S9 · N=64 并发吞吐与 I/O 放大

**初始状态**：64 个独立 workspace、64 个独立 session id，同一台机器，同一录制代理。任务用 S6 的 30 轮脚本。

**用户提示**：同 S6。

**外部判据**：
1. 每个 run 的 wall time p50/p95；
2. 磁盘写字节总量（`iostat` / Windows 性能计数器）；
3. Collie 侧 `data/sessions/*.json` 的 `fsync` 次数（用 `strace -e fsync` 或 ETW 采样）。

Collie 每个模型/工具边界都做**全量 transcript 重写**：`loop.py` 在 `turn_boundary`/`calling_model`（每次 attempt）/`model_complete`/`executing_tool`/`tool_complete`/`terminal` 各调一次 `_session_checkpoint`（`loop.py:690-711`）→ `sessions.checkpoint`（`sessions.py:384-413`）→ `_merge_messages`（`sessions.py:241-255`，对整段历史做逐元素 dict 比较）+ `_atomic_dump`（`sessions.py:679-711`，含 `os.fsync`，第 689 行）。这是 O(轮数²) 的字节量。Codex 侧是 rollout 追加 + 显式 `flush_rollout()`（`tasks/mod.rs:373`、`959`、`999`），**但 rollout writer 在 pinned 树中不可见**，追加语义属推断，标注「未复现」。

---

### S10 · 同一 session 的双执行者

**初始状态**：一个已有 session id。

**注入时机**：进程 A 开始 run 后 2 秒，进程 B 用同一 sid 发起 run。

**用户提示**（两侧相同）：`Append the line HELLO to log.txt.`

**外部判据**：`log.txt` 中 `HELLO` 的出现次数、以及 transcript 是否交错。
- Collie：`session_owner.py:446-504 try_acquire()` 用 `msvcrt.locking(LK_NBLCK)` / `flock(LOCK_EX|LOCK_NB)`（`session_owner.py:205-213`），**非阻塞**；B 侧应得 `OwnershipRefused(busy=True)`（`loop.py:1511-1515` 转成 `stop_reason="ownership_refused"`），且 B **不写任何一条消息**（`_refusal`，`loop.py:1517-1533`）。
- Codex：app-server 单进程内 `active_turn` 互斥（`turn_input.rs:340-349` 返回 `NotSubmittedReason::NotIdle`）；跨进程需 `SuspendTurnAndShutdown` 交接（`turn_suspension.rs:100-112` 在关闭 writer 之后才宣告，防止双写）。
- 判据：`HELLO` 恰好一次；A 的 transcript 完整。

---

### S11 · 429 风暴下的退避行为

**初始状态**：回放代理配置为前 3 次请求返回 HTTP 429 + `Retry-After: 30`，之后正常。64 并发同时启动。

**用户提示**：`List the files in this directory.`

**外部判据**：代理侧记录的重试到达时间分布。
- Codex：`util.rs:86-91 backoff()` 有 `rand::rng().random_range(0.9..1.1)` 抖动，且 `responses_retry.rs:105` 优先用 `err.retry_delay()`（尊重 `Retry-After`）。
- Collie：`loop.py:1971` `delay = self.retry_base * (2 ** attempts)`，**无抖动**；`providers.py:236-244` 只把 `retry-after` 头拼进错误**文本**，从不解析成延迟。预期 64 个 run 在 t+2s、t+6s、t+14s 三个尖峰同时重试。
- 判据：重试到达时间的变异系数、以及第一次成功的 p95。

---

### S12 · turn 预算与真实 model call 数的等价性

**初始状态**：两侧都设 `max_turns = 12`（Collie `h.max_turns`；codex 侧无等价开关，需用回放代理硬计数）。

**用户提示**：一个确定需要 >12 轮的任务，例如 `Add a docstring to every function in src/ (there are 40 functions), one edit_file call per function.`

**外部判据**：**代理侧实际计数的 HTTP 请求数**。

Collie 的 12 "turns" 并不等于 12 次请求：`_maybe_compact` 会额外花 1 次（`loop.py:1827-1830`）、critic 额外 1-2 次（`loop.py:2766-2777`）、contract repair 1 次（`loop.py:1941-1966`）、以及**用尽 turns 后仍会再发 1 次 synthesis 请求**（`loop.py:2916-2927`）。上限只有 `max_model_calls`（默认 0 = 无限）真正约束。任何按「轮数」对齐预算的排名都是错的。

---

## 二、会让朴素排名失效的模型/工具/认证差异

### 2.1 认证与配额不对称
Collie 的 Anthropic OAuth 路径带 `subscription_only`（`providers.py:714-720`、`811-840`），且 `loop.py:353-357 _spend_exceeded()` 在 `subscription_only` 为真时**跳过 `$` 上限**（只剩 token 上限）。这意味着：订阅臂在 64 并发下会撞订阅侧速率限制（触发 S11 的无抖动风暴），而 API-key 臂不会；同时两臂的「预算耗尽」定义不同。**必须要求两侧都用同一个 API-key/回放代理**，并在报告中声明。

### 2.2 工具面不同，不是同一个 agent
Collie 的 SWE 配置显式裁剪工具集（`swe.py:406-413`）：`benchmark_safe` 下**没有 shell**，只有 `read_file/write_file/edit_file/grep/glob`；非 safe 下才有 `bash`、可选 `code_search`、可选 `run_in_env`。Codex 的 `exec_command`/`unified_exec` 是持久 PTY 会话（`tools/handlers/unified_exec/exec_command.rs:310-318` 区分 `Interactive`/`OneShot`），Collie 的 `bash` 是一次性进程（`tools.py:497`）。**"能否保持 shell 状态"本身就是能力差**，不该混入模型分数。

### 2.3 Collie 会向对话注入 harness 自己的 user 消息，codex 基本不会
默认 `self_verify=True`（`loop.py:498`）会在任何一次编辑后注入 `VERIFY_NUDGE`（`loop.py:53-60`），SWE 模式还会注入 `EDIT_FORCE_NUDGE`/`COVERAGE_NUDGE`/`ROLLBACK_NUDGE`/critic 反馈。这些都是 harness 作者写的 prompt engineering，**测的是 Collie 的提示工程而不是 Opus 5**。若目标是"harness 对比"这没问题；若目标是"模型对比"则必须关闭（`COLLIE_SWE_VERIFY=0`）。Codex 侧最接近的只有 stop hooks（`hook_runtime.rs:376`）和 `TurnAborted` 标记（`context/turn_aborted.rs:10-11`），默认不注入。

### 2.4 `benchmark_safe` 是一个被削弱的 Collie
`swe.py:414-429` 在 benchmark 模式下强制 `max_retries=0`、`retry_base=0.0`、`overflow_recovery=False`、`hooks=None`、`auto_prefetch=False`、无 project rules / skills。这是为了预算公平，但意味着**基准里的 Collie 不是产品里的 Collie**：一次瞬时 503 就直接判负。报告必须同时给出 product-arm 数字。

### 2.5 上下文管理默认值差一个数量级
见 S6：48K 固定阈值 vs 从模型窗口推导。在 Opus 5 上这是最大的单点系统性偏差，且与模型能力无关。

### 2.6 Collie 有两条完全不同的执行路径
`run_ownership.hold()`（`run_ownership.py:112-130`）在 `session` 为假值时 **yield None**，随后 `_durable_session_id()`（`loop.py:681-688`）返回 `""`，于是 `_session_checkpoint` 直接 return True（`loop.py:692-694`）、`_inbox_ready()` 返回 None（`loop.py:846-856`）。**即：不设 `checkpoint_scope`/`durable_session_id` 的 benchmark 跑法会绕过日志、租约、durable inbox、恢复围栏、verification_context 的全部逻辑。** 要测 S1/S2/S10 就必须显式设 `h.checkpoint_scope = "session:" + sid`（参照 `cli.py:687`、`tui.py:604`）。

### 2.7 时间与超时常数不可比
Collie `bash` 默认 120s / 上限 600s（`tools.py:484-485`）；codex one-shot 默认 10s（`exec.rs:61`）。同一个 `npm test` 在一侧成功、一侧 124。必须成对跑「默认」与「显式 timeout」两个变体。

---

## 三、Collie 的源码级弱点（含可信度标签）

### 3.1 验证门与自家工具/自家提示互相矛盾 —— **代码可确定**
`loop.py:361-377 _is_repro_cmd()` 第 369 行：`if name != "bash": return False`。而 `RunInEnvTool.name == "run_in_env"`（`tools.py:594`），其 description（`tools.py:595-602`）和 `swe.py:528-570` 的系统提示都明确要求模型**用 `run_in_env` 来 REPRODUCE 和 VERIFY**，还写着本地检查"meaningless"。同时 `swe.py:465-470` 默认打开 `verify_gate=True, require_assert=True`。

后果链：模型照做 → `last_repro_turn` 永不推进 → `_repro_verified()`（`loop.py:1458-1473`）恒为 False → 消耗 `verify_max`（默认 3）轮无效 repair → `loop.py:2976-2983` 把 `res.error` 设为 "verification required but no executed post-edit assertion passed" 并在答案里追加 `_[run failed: …]_`。**正确的补丁被系统性判负**，同时多烧 3 轮 token。

叠加放大：`_has_unsafe_test_shell_control()`（`loop.py:189-246`）把含 `\n`、`;`、`|`、`$(`、裸 `&` 的命令一律判为非证据（第 228-244 行）。而 `BashTool` 只回传 8000 字符（`tools.py:546`），这恰恰**诱导**模型去 `| tail`。两个机制互相为敌。

*不确定部分*：真实触发率取决于模型行为，**未复现**。

### 3.2 无抖动、无 `Retry-After` 的退避 —— **代码可确定**
`loop.py:1971`：`delay = self.retry_base * (2 ** attempts)`，`retry_base` 默认 2.0、`max_retries` 默认 3（`loop.py:530-536`）。`providers.py:236-244` 把 `retry-after` 只并入 `detail` 字符串。对照 codex `util.rs:86-91`（±10% 抖动）+ `responses_retry.rs:105`（`err.retry_delay()` 优先）。

后果：N 个并发 run 的重试完全同相，在 429/529 场景下自我加剧。`loop.py:1978` 的 `if not self.cancelled: time.sleep(delay)` 还会整段阻塞该 run 的线程。

*不确定部分*：实际 collapse 阈值取决于服务端配额，**未复现**。

### 3.3 provider 实例被当作可变全局状态 —— **代码可确定，产品影响未复现**
`loop.py:1851` 每轮写 `self.provider.cache_stable_upto = meta.elide_from`；`loop.py:2124-2129` 在输出截断时永久执行 `self.provider.max_tokens = min(32768, cur * 2)`，**run 结束后从不还原**。`swe.py:415` 还会写 `h.provider.subscription_only = True`。

证据表明这是已知问题：`delegate.py:108-117` 在子 run 前后显式 save/restore 了**恰好这两个字段**（`max_tokens`、`cache_stable_upto`），但主 run 自身没有对称处理。

后果：长驻 Harness（TUI/web/REPL 复用同一 provider 跨多轮）在一次截断后，之后每一轮都按翻倍后的 `max_tokens` 计费；若某个 embedder 让两个 Harness 共享一个 provider 对象，`cache_stable_upto` 会跨 run 串扰导致缓存断点错位（表现为 `loop.py:2044-2071` 的 cache-miss 账本报 "unexplained"）。

*未复现*：Pack 走 `make_harness` 逐 member 构建（`pack.py:433`），因此 Pack 路径大概率安全；风险面是长驻会话与自定义 embedder。

### 3.4 自带的 codex app-server transport 与 pinned codex 协议不兼容 —— **代码可确定**
`codex_app_server_runner.py:403-407` 发送：
```python
{"method": "turn/steer", "id": request_id,
 "params": {"threadId": thread_id, "input": [...]}}
```
缺 `expectedTurnId`。pinned codex 的 `app-server/src/request_processors/turn_processor.rs:1020-1022`：
```rust
if params.expected_turn_id.is_empty() {
    return Err(invalid_request("expectedTurnId must not be empty"));
}
```
无论 `TurnSteerParams.expected_turn_id` 是 `#[serde(default)]` 的 `String`（→ 空串被拒）还是必填（→ 反序列化失败），这个请求都会失败。而 runner 的 `steer_current()` 在 `except Exception: return False`（第 409-410 行）里吞掉异常，并且它**根本不等响应**（`transport.send` 后立刻 `return True`）——所以调用方会看到 "steer 成功"而实际没送达。

对比：`cancel_current()`（第 424-427 行）发的 `turn/interrupt` 参数 `{threadId, turnId}` 与 `turn_processor.rs:1571` 的 `TurnInterruptParams { thread_id, turn_id }` 一致，是对的。

**影响**：任何用这个 runner 做的 steering 对比，codex 侧都会假性得 0。

（`app-server-protocol` crate 不在 pinned 树内，故 `expectedTurnId` 的确切 serde 属性**不可见**；但 handler 的空值检查是决定性的。）

### 3.5 全量重写 + fsync 的检查点是 O(n²) —— **代码可确定，规模影响未复现**
见 S9。`sessions._merge_messages`（`sessions.py:241-255`）对完整历史做逐元素 `dict ==` 比较（其中 `content` 可能是数 MB 的工具输出），`_atomic_dump`（`sessions.py:679-711`）序列化整个 transcript 并 `os.fsync`（第 689 行），每个模型/工具边界各一次。

叠加故障模式：`_atomic_dump` 的 Windows `PermissionError` 重试只有 7 次 × 指数退避（`sessions.py:698-705`，累计约 0.63s）。一旦超出，`_session_checkpoint` 返回 False，而 `loop.py:2320-2324` 会因此**拒绝执行工具**（`"ERROR: durability checkpoint failed; tool was not executed"`）。在 Windows + 杀软 + 高并发下，磁盘争用会直接表现为工具调用失败。

*未复现*：具体的争用阈值需实测。

### 3.6 收敛比例的环境变量解析未加保护 —— **代码可确定，影响小**
`loop.py:1765-1768`：
```python
_fr = float(getattr(self, "force_ratio", None) or os.environ.get("COLLIE_FORCE_RATIO", "0.55"))
_hr = float(getattr(self, "hard_ratio", None) or os.environ.get("COLLIE_HARD_RATIO", "0.76"))
```
这两行在 `try:`（第 1779 行）**之前**，且无 `try/except`。对照紧邻的 `turn_cap`/`turn_target` 解析（`loop.py:1750-1757`）是有保护的。一个手滑的 `COLLIE_FORCE_RATIO=0,55` 会让 `_run()` 抛 `ValueError` 穿出，绕过整个 finalize 路径（不写 terminal checkpoint、不发 receipt、不 `finish_run`），只剩 `run()` 的 `finally` 做 inbox settle（`loop.py:1504-1509`）。

### 3.7 无条件的历史剪裁 —— **代码可确定**
`context.py:364-367`：`window = 14`、`stub = 240`，与上下文压力、模型窗口大小**完全无关**，每次 build 都执行（`context.py:373-409`）。对 Opus 5 而言，第 15 条消息之前的所有工具输出被砍到 240 字符（且保留的是**头部**，第 383 行），而 `BashTool` 的重要信息在尾部（`tools.py:542-555` 明确保留尾部）。二者方向相反：bash 保尾、elision 保头 → 一次 `pytest` 失败的 traceback 在 14 条消息后**只剩表头**。

### 3.8 cancellation 三处调用点不一致 —— **代码可确定，影响小**
`loop.py:1896` 传 `cancelled=self.cancelled`（可能是 None）；`loop.py:1443`（critic）与 `loop.py:2925`（synthesis）传 `cancelled=self._cancel_requested`（绑定方法，恒为真值）。`cancellation.py:17` 的短路条件 `if not cancelled or ...` 因此在后两处永不命中，只要 provider 有 `cancel_for`/`request_authority` 就会额外拉起一个 50ms 轮询的 watcher 线程（`cancellation.py:24-48`），即便调用方压根没配置取消源。

---

## 四、公平的 codex 对比传输方案（基于源码，不臆造 flag）

### 4.1 结论：用 `codex app-server --stdio`，不用 `codex exec`
`codex exec` 的 CLI 面（`exec/src/cli.rs`）只提供 `--json`（第 59-65 行，别名 `--experimental-json`）、`-o/--output-last-message`（第 68-74 行）、`--output-schema`（第 47-49 行）、`--ephemeral`（第 36 行）、`--skip-git-repo-check`（第 32 行）、`--ignore-user-config`（第 40 行）、`--ignore-rules`（第 44 行），子命令 `resume`/`fork`/`review`（第 149-159 行）。它是**单向的**：没有中途注入用户消息的入口，也没有 interrupt RPC。用它去对比 Collie 的 steering/queue/cancel 等于不测。

app-server 在 pinned 源码中已验证存在的原语：
- `submission_loop` 处理 `Op::Interrupt`、`Op::TurnInput{StartOrSteer|StartIfIdle|Steer}`、`Op::RecoverTurn`、`Op::SuspendTurnAndShutdown`、`Op::Compact`、`Op::ThreadRollback`（`core/src/session/handlers.rs:529-700`）。
- RPC 层：`turn_start`（`turn_processor.rs:167`）、`turn_steer`（第 247 行）、`turn_interrupt`（第 258 行）、`thread_inject_items`（第 185 行）、`thread_settings_update`（第 195 行）。
- 方法名 `thread/start | thread/resume | thread/fork | turn/start` 在 `app-server/src/message_processor.rs:121` 出现（该处是 `permissionProfile` 的拒绝校验，顺带证实了方法名字符串）。

### 4.2 具体方案
以 Collie 已有的 `harness/codex_app_server_runner.py` 为基线，做四处修正：

1. **补 `expectedTurnId`**（`codex_app_server_runner.py:403-407`）。runner 已在 `state["turn_id"]` / `self._active_turn_id`（第 554-559 行）里持有 turn id，直接带上即可。同时应改为**等待响应**并区分 `Steered` / `NotSubmitted{reason}`，而不是 `send` 后立刻 `return True`。

2. **改为长驻进程**。当前 `_invoke()`（第 476-568 行）每一轮 spawn 一次 app-server、`thread/start` 或 `thread/resume`，再 `turn/start`。这付出了每轮的进程启动 + MCP/skills 初始化成本，并且**绕过了 codex 的 `regular.rs:78-98` 跨 turn pending-input 循环**（`turn.rs:509-549` 的 mid-turn auto-compact 也测不到）。应保持一个 app-server 进程，`thread/start` 一次，之后连续 `turn/start`。

3. **对齐配置面**。当前 `_argv()`（第 455-468 行）用 `-c mcp_servers={}` / `plugins={}` / `web_search="disabled"` / `project_doc_max_bytes=0` / `features.hooks=false` / `features.memories=false` / `features.multi_agent=false` / `features.apps=false` 关闭扩展面 —— 这是正确的做法，与 Collie 的 `benchmark_safe`（`swe.py:414-429`）在精神上对齐。但要补齐两侧的 **auto-compact 阈值**：codex 侧用 `-c model_auto_compact_token_limit=<X>`（该 key 在 `context_window.rs:72` 被读取为 `config.model_auto_compact_token_limit`），Collie 侧用 `COLLIE_COMPACT_TOKENS=<X>`（`compaction.py:213`），设成同一个数。

4. **审批策略显式化**。runner 现在硬编码 `"approvalPolicy": "on-request", "sandbox": "workspace-write"`（第 522-523、532 行）。Collie 侧 benchmark 常常 `gate=None`（完全不设门，`loop.py:723-724`）。这不对等 —— 要么两侧都无人值守拒绝（Collie 设 `gate` 但 `approve=None`，走 `loop.py:755-765`；codex 用 `never` 类策略），要么两侧都自动放行。**注意 pinned 树里 `--sandbox`/审批枚举的合法值定义在 `utils/approval-presets` 与 `app-server-protocol`，均不可见**，需以 `codex app-server generate-json-schema` 的实际输出为准，不要照抄我这里的字符串。

### 4.3 判据采集
两侧统一从**协议流**取指标，不取 harness 自报：
- codex：`item/started` / `item/completed` / `turn/completed`（含 token usage）的 JSONL。
- Collie：`h.emit`（`loop.py:641-652`）的 NDJSON，事件名已覆盖 `tool`/`gate`/`repro`/`compaction`/`retry`/`cache_miss`/`receipt`。
- 请求数与 token 以**回放代理侧计数**为准（理由见 S12）。

---

## 五、修复优先级（按产品影响）

| 优先级 | 项 | 依据 | 影响 |
|---|---|---|---|
| **P0** | `_is_repro_cmd` 接受 `run_in_env`（并考虑接受管道后仍以退出码为准的形式） | `loop.py:369`、`tools.py:594`、`swe.py:411-412,465-470,528-570` | 目前把**正确补丁判为失败**，且白烧 3 轮修复。这是唯一会直接颠倒正确性判定的缺陷 |
| **P0** | 补 `expectedTurnId` 并让 `steer_current` 等待响应 | `codex_app_server_runner.py:403-410` vs `turn_processor.rs:1020-1022` | 对手臂的 steering 能力被静默清零；任何基于它的对比结论都不成立 |
| **P1** | 压缩阈值改为按模型窗口推导（保留 48K 作为未知模型的兜底） | `compaction.py:165-186` vs `context_window.rs:52-119` | 在 Opus 5 上系统性丢弃 ~76% 可用窗口的逐字历史；长任务质量的最大单点损失 |
| **P1** | 退避加抖动 + 解析 `Retry-After` | `loop.py:1971`、`providers.py:236-244` | 高并发下自我加剧的重试风暴；直接决定 N=64 场景能否跑完 |
| **P1** | run 结束后还原 `provider.max_tokens`（复用 `delegate.py:108-117` 的模式） | `loop.py:2124-2129` | 长驻会话在一次截断后永久多花钱，用户不可见 |
| **P2** | 历史剪裁改为压力驱动，且保留尾部而非头部 | `context.py:364-367,383` vs `tools.py:542-555` | 两个模块的截断方向相反，导致 traceback 尾部丢失；影响调试类任务 |
| **P2** | 检查点写入降频/增量化（例如只在 `executing_tool` 与 `terminal` 强制 fsync） | `sessions.py:241-255,679-711`；`loop.py:1880,1903,2320,2425` | O(n²) 字节量 + Windows 上争用导致工具拒绝执行；纯并发/规模问题，不改变正确性 |
| **P3** | `COLLIE_FORCE_RATIO`/`COLLIE_HARD_RATIO` 加 try/except，与 `turn_cap` 对齐 | `loop.py:1765-1768` vs `1750-1757` | 配置手滑会绕过整个 finalize 路径；触发概率低但失败形态很脏 |
| **P3** | 统一三处 `cancellation.complete(cancelled=...)` 的实参 | `loop.py:1443,1896,2925`；`cancellation.py:17` | 无谓的 watcher 线程；语义不一致易在后续改动中埋雷 |

---

## 六、审计边界声明

- 全部结论来自静态阅读，**未运行任何代码**；标注「代码可确定」的项指控制流在源码中是无歧义的，标注「未复现」的项指其真实发生率/量级需实测。
- codex 侧 5/140 crate 可见，涉及 `rollout` 写入语义、`DEFAULT_OUTPUT_BYTES_CAP` 数值、`SharedCliOptions` 的 flag 集合、`TurnSteerParams` 的 serde 属性，我均未做断言。
- 我按要求忽略了 README 层面的宽泛表述；`app-server/README.md` 中的方法清单仅在能被 `turn_processor.rs` / `handlers.rs` / `message_processor.rs` 交叉验证时才被引用。
- 未读取任何凭据文件、用户会话、评分器或历史 run 目录。