已完成只读审计。下面是报告。

---

# Collie × opencode 深度执行语义审计报告

**审计范围**:`C:/workspace/collie`(Python,`harness/` 为核心)与 `C:/workspace/collie-product-2026-09-06/references/opencode`(pinned,`packages/opencode/src`)。
**已遵守的约束**:未发起任何网络调用;未修改任何文件;未读取 `~/.codex/auth.json`、`~/.claude/.credentials.json` 等凭据文件本体;未读取 `bench/`(评分器)与任何 run 产物;README 与文档中与实现不符的宣称一律以源码为准。
**证据边界(必须先说明)**:opencode 的 pinned 树是**稀疏检出**——只有 `packages/app` 与 `packages/opencode`(见 `packages/*/package.json` 仅两条)。`@opencode-ai/core`、`@opencode-ai/llm`、`@opencode-ai/sdk` 不在树内。因此凡涉及 `Flag.*`、`LLM.stream`、`SessionRetry.policy`、`MessageV2.filterCompactedEffect` 内部实现的结论,我只能依据 `packages/opencode/src` 中的**调用点**推断,已在下文逐条标注。

---

## 0. 一句话结论

两者不是同类物。**opencode 是进程内 agent 环路**(`SessionPrompt.runLoop`,AI SDK + Effect fiber);**Collie 是外部 agent 的编排器 + 证据层**,其 `RUNNER=collie` 原生环路(`harness/loop.py:Harness._run`)与其外部 worker 适配层(`harness/runner_slice.py`)是两套完全不同的执行语义。把「Collie」当作单一被测对象直接与 opencode 排名,在方法论上是无效的——必须先声明被测的是哪条路径。

---

## 1. 执行环路:逐路径对照

### 1.1 opencode:进程内、DB 驱动、步进式重读

`packages/opencode/src/session/prompt.ts:1081-1341` 的 `runLoop`:

- `1088` `while (true)`,每轮 `1092` 从 SQLite **重新读取**消息(`MessageV2.filterCompactedEffect(sessionID)`),`1096` 取 `MessageV2.latest(msgs)`。
- 退出条件 `1111-1116`:`lastAssistant.finish` 存在、不属于 `["tool-calls","unknown"]`、无未处理 tool part、且 `lastAssistant.parentID === lastUser.id`。
- `1178-1179` `maxSteps = agent.steps ?? Infinity`;到达上限时在消息尾部追加 `MAX_STEPS_PROMPT`(`1281`),而不是硬停。
- `1149-1159` compaction 作为一个 pseudo-task 参与同一环路;`1161-1168` 上一轮 `tokens` 溢出则 `compaction.create({auto:true})` 后 `continue`。
- `1213-1219` `processor.create(...)`,`Effect.onInterrupt(() => finalizeInterruptedAssistant)`。

**关键结构性事实**:每轮重读 DB,意味着**任何在运行期间写入的新 user message 会在下一步被自然采纳**。这就是 opencode 的 steering 机制,不需要额外协议。

### 1.2 opencode 的并发闸门:`Runner`

`packages/opencode/src/effect/runner.ts:115-138` `ensureRunning`:

| 状态 | 行为 |
|---|---|
| `Running` / `ShellThenRun` | `awaitDone(st.run.done)` —— **不排队新 work**,只等待现有 fiber(`122`) |
| `Shell` | 转 `ShellThenRun`,把 work 挂为 `PendingHandle`(`123-130`) |
| `Idle` | `startRun`(`131-135`) |

`startShell`(`140-169`)在非 `Idle` 时直接 `Effect.fail(new Busy())`。
`cancel`(`171-202`)对 `Running` 做 `Fiber.interrupt` + `Deferred.fail(Cancelled)`。

因此 opencode 的 "queue" 语义是:**prompt 的 work effect 被丢弃,但 user message 已落库**(`prompt.ts:1052-1071`:先 `createUserMessage`,再 `loop()`)。这是一个很容易被误读的设计——它不是队列,而是「写库 + 复用同一 fiber」。

### 1.3 Collie 原生环路

`harness/loop.py:1780` `for turn in (range(turn_cap) if turn_cap else itertools.count())`。与 opencode 的差别在**中断粒度**:

- `1789`、`1874`、`1911`、`2146`、`2572`、`2590`、`2791` 共 7 个显式 `self._cancel_requested()` 检查点。
- `2146-2159`:模型已返回 tool_calls 但**尚未执行任一工具**时若取消,为**每个** call 追加 `{"role":"tool", "content":"CANCELED: run stopped before execution"}`,再 `break`。协议合法性由宿主保证。
- `1184-1227` `_close_unanswered_calls`:区分三种结局——正在执行且 `replay_safe`(`"INTERRUPTED: ... host-attested as effect-free"`)、正在执行且非安全读(`"INTERRUPTED: ... effect is UNKNOWN"`)、未开始(`"CANCELED"`)。

对照 opencode `session/processor.ts:553-611` `cleanup()`:对每个未完成 tool 等待 `Deferred` **250ms**(`587`),超时后统一写成 `status:"error"`, `error:"Tool execution aborted"`, `metadata.interrupted = true`(`601-603`)。**不区分该工具是否有副作用**。

### 1.4 Collie 外部 worker 环路(`codex-exec`)

`harness/agent_runners.py:791-825` / `827-859`:一次 `resume`/`start` = **一次完整子进程生命周期**。prompt 走 stdin,`SubprocessRunner.run` 阻塞到进程消亡(`agent_runners.py:557-633`),然后解析全量 stdout。

`_START_GATE_SCRIPT`(`166-195`)是一个值得强调的设计:先起一个 `sys.executable -I -c <gate>` 阻塞在 `stdin.read()`,父进程完成 Job Object / process group 归属登记后(`on_process(proc)` → `register`,`agent_runners.py:980-990`)才下发真实 argv。`register` 返回 `False` 即「启动期取消」,目标进程从未存在。

**这条路径没有 turn 内环路**:没有 steering、没有 fork、没有 compact。

---

## 2. 能力矩阵(源码声明值,非文档)

| 能力 | opencode | collie(原生) | codex-exec | claude-code | codex-app-server | pi-rpc |
|---|---|---|---|---|---|---|
| 声明位置 | 实现即声明 | `runner_registry.py:85-117` | `:119-148` | `claude_code_runner.py:94-117` | `runner_registry.py:154-167` | `:183-191` |
| steer | 是(写库) | `steer=True` | **`steer=False`** `:127` | **`steer=False`** `:102` | `steer=True` | `steer=True` |
| follow_up | 是 | True | **False** `:128` | **False** `:103` | — | True |
| session_fork | `POST /session/:id/fork` | `session_fork=False` | False | False `:99` | — | True |
| 审批回环 | `permission.ask` → `POST .../permissions/:id` | `approval_round_trip=True` | **False** `:133` | **False** `:105` | True `:158` | False |
| cancel | fiber interrupt | `native+process-tree` | `process-tree` | `process-tree` `:104` | `native+process-tree` | `native+process-tree` |
| shell/bash | 有(`ShellID.ToolID`) | 有 | 有(sandbox 内) | **无** `claude_code_runner.py:87` | 有 | **无**(`:337`) |
| compact | `SessionCompaction` | `harness/compaction.py` | 无 | 无 | 无 | True |

`codex-exec` 的 `steer=False` 会被 `webapp.py:5502-5519` 读成 `worker_caps["steer"]`,并在 `5824` 处直接跳过整个 steering drain,同时 `5516-5519` 的 `_run_mark(..., can_steer=...)` 会通知前端。这一点是**诚实声明**的,不是隐藏缺陷。

---

## 3. 八至十二个可判别、可复现场景

统一前置(所有场景):

- 工作区 `W` = 全新 `git init` 仓库,`git commit --allow-empty -m base`,记 `BASE=$(git rev-parse HEAD)`。
- 模型固定 `claude-opus-5`。
- 判据一律**外部可观测**:`git status --porcelain`、文件内容、进程退出码、结构化事件流(`opencode run --format json` 的 stdout;Collie 的 `emit` 事件 / `runs.db` 行 / `RunnerReceipt`)。禁止采用 agent 自述。
- 每场景 N≥20 次,记录 pass rate 与方差;所有涉及注入时机的场景以**外部 sentinel 文件**触发,不用 wall-clock。

---

### S1 — 工具批中途取消 · 副作用归属

**初始态**:`W/` 含 `a.txt`(内容 `A`)、`b.txt`(`B`)、`c.txt`(`C`)。
**prompt**:
```
Rewrite a.txt, b.txt and c.txt so each file contains exactly the single word DONE. Issue all three edits, then run `git status --porcelain` and report it.
```
**注入时机**:外部 watcher 轮询 `W/a.txt`;一旦其内容 == `DONE`,立即发出取消(opencode:`POST /session/:id/abort`;Collie web:`Handler._run_cancelled` 置位;Collie CLI:SIGINT)。
**外部判据**:
1. 取消后 60s 内进程组内无残留后代(POSIX `pgrep -g`,Windows Job `ActiveProcesses==0`)。
2. 持久化会话中,`c.txt` 对应的那次 tool call **必须**有配对结果记录。
3. 记录 `c.txt` 是否被写。

**预期判别**:Collie `loop.py:2146-2159` 会把整批未执行的 call 标记为 `CANCELED: run stopped before execution`;若已在执行 `write_file` 则 `sessions.py:444-449` 判定 `recovery_required=True`。opencode `processor.ts:591-607` 把它们统一记为 `"Tool execution aborted"` + `interrupted:true`,而 `prompt.ts:96-100 isOrphanedInterruptedTool` **明确把这类 part 视为「非待办」**——下一轮不会 prefill 追问。这是最锐利的一个判别点。

---

### S2 — 中断后续跑(session recovery)

**初始态**:S1 结束后的同一会话。
**prompt(第二轮,逐字)**:
```
Continue. Tell me which of the three files you already changed and finish the remaining ones.
```
**判据**:
1. 第二轮是否被**允许启动**。Collie:`sessions.recovery_state` 若 `recovery_required=True`,`sessions.resume_after_interrupt`(`:518-547`)返回 `blocked=True`,surface 应拒绝推进。opencode:无对应闸门,必然启动。
2. 最终 `a/b/c.txt` 内容与 `git diff --stat` 是否与「已完成部分 + 未完成部分」一致,有无重复施加的副作用。

**这是一个"Collie 会故意得低分"的场景**——见 §4 的排名失效分析。

---

### S3 — 副作用性工具中断 vs 只读工具中断

**初始态**:`W/` 含脚本 `touch_marker.sh`(`echo hit >> W/marker.log; sleep 30`)与一个 200MB 文本 `big.txt`。
**prompt A**:`Run ./touch_marker.sh and then tell me what it printed.`
**prompt B**:`Read big.txt and summarize its first 50 lines.`
**注入时机**:两者都在 `W/marker.log` 出现 / 读取开始后 2s 取消。
**判据**:两次中断在持久化状态里是否被**区分**。
Collie:`sessions.py:463-475` `_replay_safe_read` 只对 `{"read_file","glob","grep","memory_search","delegate"}` 且 `detail.replay_safe is True` 且 `not detail.internal` 放行 → B 可自动续跑,A 被围栏。
opencode:两者都是 `"Tool execution aborted"`,无区分。
**外部判据**:重跑一次后 `wc -l W/marker.log` 是否变成 2(重复副作用)。

---

### S4 — 运行中转向(steering)的采纳边界

**初始态**:空 `W`。
**prompt 1**:
```
Create files step01.txt through step12.txt, each containing its own name. Create them one at a time, checking with ls after each.
```
**注入**:watcher 检测到 `W/step03.txt` 存在的瞬间,注入第二条 user message:
```
STOP creating numbered files. Instead create exactly one file called override.txt containing the word OVERRIDE, and create nothing else.
```
注入方式(必须用各自源码里真实存在的通道):
- opencode:`POST /session/:sessionID/prompt_async`(`server/routes/instance/httpapi/groups/session.ts:96`,handler `handlers/session.ts:311-329`,返回 204 并 `Effect.forkIn(scope, {startImmediately:true})`)。
- Collie web(原生):`Handler._steer_push`(`webapp.py:1119-1129`)→ `h.steering = _drain_steer`(`:6244-6254`)→ `loop.py:1804 / 2802`。
- Collie → codex-exec:**无通道**。此格必须记为 `N/A (declared steer=False)`,不得记为 fail。

**外部判据**:注入生效后 `ls W/` 中 `stepNN.txt` 的最大编号 `M`。`M` 就是「转向延迟」的物理度量。opencode 预期 `M` ≈ 注入时刻所在那一步 + 当前 tool batch;Collie 原生预期在 `loop.py:2802` 或下一 turn 顶部 `1804` 采纳。**必须同时记录 `override.txt` 是否存在**。

---

### S5 — 转向的持久化保证(崩溃窗口)

**初始态**:同 S4。
**注入**:同 S4 注入 steering,然后在注入后 200ms **SIGKILL 宿主进程**。
**恢复**:重启并续跑同一 session。
**外部判据**:重启后的 transcript 里,那条 override 指令是否 **恰好出现一次**(既不丢失也不重复)。

Collie 原生有专门为此设计的四步协议:`loop.py:1006-1024` 的注释明确规定 `claim → journal(一条一条) → checkpoint → ack`,并在 `_ack_input_entry`(`:973-1004`)里要求 `checkpointed is False` 时**拒绝 ack**。webapp 的 worker 路径有等价物 `journal_before_transport` → `ack_delivered`(`webapp.py:5855-5889`)。
opencode 侧:`prompt_async` 的 user message 由 `createUserMessage` 写入 SQLite(`prompt.ts:1046-1047`),持久性来自 DB 事务,但**没有 claim/ack 两阶段**——需要实测它在 kill 窗口内的行为。

---

### S6 — 上下文溢出恢复

**初始态**:`W/` 含 40 个各约 60KB 的 `.md` 文件。
**prompt**:
```
Read every .md file in this directory one at a time, then write a file summary.md containing one bullet per source file with its title.
```
**判据**(全部外部可观测):
1. run 是否终止于 provider 的 context error(退出码 / `session.error` 事件),还是完成。
2. `summary.md` 的 bullet 数是否 == 40。
3. compaction 事件计数与其前后 token 估计。

opencode 有两条溢出路径:**预测式** `prompt.ts:1161-1168` + `overflow.ts:22-34 isOverflow`(阈值 `usable()` = `limit.input - reserved`,`reserved` 默认 `min(20000, maxOutputTokens)`,`overflow.ts:8,14-19`);**反应式** `processor.ts:621-631`——捕获 `ContextOverflowError` 后设 `ctx.needsCompaction=true`,`Stream.takeUntil` 截流(`658`),`process` 返回 `"compact"`。
Collie 只有一条:`loop.py:1271-1379 _maybe_compact`,且**受 `overflow_recovery` 开关门控**(`:1236-1245`),并且在 `1309-1317` 有四道放弃闸门(取消、`max_model_calls`、shared budget、`_over_budget`)。**任一闸门命中即完全不压缩,直接把全量 thread 发出去**,从而必然触发 provider 端溢出。这是一个真实的判别点。

---

### S7 — 工具输出体积的处理

**初始态**:`W/huge.json`,单行,50MB。
**prompt**:`Read huge.json and tell me the value of the top-level "version" key.`
**判据**:峰值 RSS、是否 OOM、返回给模型的内容是否被截断且截断被显式标注。
opencode 有 `Truncate.Service`(`session/tools.ts:56, 196, 464`),截断时在 metadata 里给出 `outputPath`(`:204, 468`)——即溢出内容落盘可回取。
Collie 外部路径的边界在传输层:`SubprocessRunner.max_output_chars` 默认 16MiB(`agent_runners.py:552`),`drain()` 用 `stream.readline(65_536)` **带尺寸上限**(`agent_runners.py:660`,注释明确说明这是防单条超长记录一次性分配的);超限置 `output_truncated=True` → `_invoke` 里变成 `settled=False` + `"Codex output exceeded Collie's bounded capture limit"`(`:1089-1090`)。**即整个 turn 判失败**,而非截断继续。

---

### S8 — 审批/权限拒绝后的续跑

**初始态**:`W/` 为 git repo,含 `secrets.env`。
**prompt**:
```
Delete secrets.env, then create cleaned.txt containing the word CLEAN.
```
**配置**:两边都把「删除/写」置为 deny。
- opencode:`OPENCODE_PERMISSION='{"bash":"deny","edit":"ask"}'`(`config/config.ts:559-565`),非交互 `run` 下 `permission.asked` 事件被自动 reject(`cli/cmd/run.ts:809-820`,除非 `--auto`)。
- Collie 原生:`gate.py` 决策 → `loop.py:2284-2286` `out = "DENIED: %s" % denied`,`res.denied_calls += 1`。

**外部判据**:
1. `secrets.env` 必须仍存在。
2. `cleaned.txt` 是否被创建 —— 即**拒绝后 agent 是否继续做剩余工作**。
opencode 有显式开关 `experimental.continue_loop_on_deny`(`processor.ts:647`:`ctx.shouldBreak = cfg.experimental?.continue_loop_on_deny !== true`),默认 `shouldBreak = true` → **默认拒绝即中断**。这一开关必须在 benchmark 里被显式固定,否则两边不可比。

---

### S9 — 工作区隔离与还原

**初始态**:脏工作区——`W/tracked.txt` 有未提交修改,`W/untracked.tmp` 未跟踪。
**prompt**:`Refactor tracked.txt: replace every "foo" with "bar".`
**随后**:请求还原到 run 之前状态。
- opencode:`POST /session/:sessionID/revert`(`groups/session.ts:99`),底层是**影子 git 仓**——`snapshot/index.ts:71` `gitdir = <Global.Path.data>/snapshot/<project.id>/<Hash.fast(worktree)>`,`:75` `--git-dir <gitdir> --work-tree <worktree>`,`:318-343` `track()` 做 `write-tree`,`:349` `patch(hash)`。
- Collie:`worktree.prepare`(`worktree.py:75`)开真实 git worktree 分支 + `handoff_to_local`(`:138`)/ `release`(`:233`);外部 worker 路径另有 `verification.workspace_snapshot`(`:351`)前后摘要 → `_mutation`(`agent_runners.py:1267-1272`)。

**外部判据**:还原后 `git status --porcelain -z` 与 run 前**逐字节相同**,且 `untracked.tmp` 仍在。opencode 的 `snapshot/index.ts:270` 有 `"removing gitignored files from snapshot"` 日志——需实测 `.gitignore` 命中的文件在 revert 后是否被删。

---

### S10 — 「成功但零变更」的检测

**初始态**:`W` 为只读挂载(POSIX `mount -o remount,ro` / Windows ACL 拒写),内容为普通 git repo。
**prompt**:`Add a file called proof.txt containing the word PROOF.`
**外部判据**:进程退出码 + 结构化结果里 `success/settled` 字段 + `proof.txt` 是否存在。

这是 Collie 唯一明确处理过的失败形态:`agent_runners.py:1103-1115` —— 当 `settled and complete and not mutated and _policy_refusal(stderr)` 时,把 `settled` 翻成 `False`,理由是「Codex 在 stderr 报告策略拒写,却仍然 exit 0 + `turn.completed`」。匹配串见 `_POLICY_REFUSALS`(`:85-93`)与 Windows 专门分支(`:110-113`,要求同时命中 `apply_patch verification failed` 和 `access is denied`/`os error 5`)。
opencode 侧无等价检查——tool 层的写失败会作为 tool error 回给模型,模型可能仍然给出「已完成」的收尾文本,而 `run` 的退出码只在 `session.error` 事件出现时才置 1(`cli/cmd/run.ts:781-791, 841-843`)。**这个场景会让 opencode 的自述型评分虚高**,是必须纳入的反测。

---

### S11 — 并发同会话请求

**初始态**:空 `W`。
**注入**:向同一 session 并发发送两条 prompt,间隔 100ms。
```
P1: Create ten files n01.txt .. n10.txt, one at a time.
P2: Create a file second.txt containing SECOND.
```
**外部判据**:`ls W/` 的最终集合;是否有 busy 错误;两条指令是否都被执行。
opencode:`prompt` → `ensureRunning`(`runner.ts:120-122`)在 `Running` 时**不排队 work,只 await**——但 P2 的 user message 已落库,会被环路第 N+1 步吃掉。所以预期两者都执行,但 P2 的返回值是 P1 的 `lastAssistant`。这个"返回值语义"是一个真实的 API 陷阱,值得单独记一格。
`startShell` 则相反,非 Idle 直接 `Busy` 失败(`runner.ts:144-146`)。
Collie 外部 worker:`CodexExecRunner._invoke` 的 `self._run_lock.acquire(blocking=False)` 失败即 `RuntimeError("this Codex runner already has an active turn")`(`agent_runners.py:965-966`)——**硬拒绝**。

---

### S12 — 高并发下的进程树清理(这正是"高并发 benchmark"的核心风险)

**初始态**:32 个独立 `W_i`,各自一个 session。
**prompt**(全部相同):
```
Start a long-running background process by writing and executing a script that appends a timestamp to bg.log every second for 600 seconds, then immediately tell me it is running.
```
**注入**:全部启动后 30s,同时取消全部 32 个。
**外部判据**:取消返回后 60s,系统内是否还有任何写 `bg.log` 的进程;各 `W_i/bg.log` 是否停止增长。

Collie 对此有**可证伪的强承诺**:`_terminate_owned_process`(`agent_runners.py:504-546`)不接受「kill 已投递」作为完成证据——Windows 走 Job `terminate_and_wait`,POSIX 走 `_terminate_posix_group`(`:474-501`)轮询 `os.killpg(pgid, 0)` 直到 `ProcessLookupError`;`:580-583` 在 gate leader 仍存活时抓取 pgid(注释明确指出 leader 退出后 `getpgid` 无法再发现后台后代);`SubprocessRunner.run` 的 `finally`(`:619-629`)在未确认灭绝时**把成功路径也改写成 RuntimeError**;`:733-734` 若两个 drain 线程在进程灭绝后仍存活,直接 `raise`。
opencode 的 shell 侧是 `ChildProcess.make(..., forceKillAfter: "3 seconds")`(`prompt.ts:564`)+ fiber interrupt。**是否覆盖孙进程,在 pinned 树内无法确认**(`ChildProcessSpawner` 来自 `effect/unstable/process`,不在树内)。此格必须实测,并标注为源码不可判定。

---

### S13(可选)— usage / 成本口径

**判据**:同一 prompt 下,两边报告的 input/output/cache-read/cache-write 四项与 provider 端账单的偏差。
Collie 的口径极为保守:`_USAGE_KEYS`(`agent_runners.py:70-78`)显式包含 `cache_write_input_tokens`,注释说明「漏掉这一项会让预热缓存的一轮看起来免费」;`_merge_usage`(`:1275-1301`)对**缺失的键保持缺失而非填 0**(`:1288-1292`),且对同时出现 `usage` 与 `token_usage` 两种拼写的情况只取其一并 `break`(`:1297-1300`)。
opencode 的 `--format json` 只在 `step_finish` part 上暴露(`cli/cmd/run.ts:749-751`),字段形状由不在树内的 `MessageV2` 决定 —— **需实测,不得假设**。

---

## 4. 使 naive 排名失效的模型 / 工具 / 鉴权差异

这一节是本报告最重要的部分。以下每一条都足以单独让「谁分高谁赢」的结论无效。

### 4.1 模型可达性:大多数 Collie runner 根本跑不了 Opus 5

`runner_registry.py:222-394` 的 `_SPECS` 共 9 个 key。按 `credential_family` 分:

- `codex` 家族(`codex-exec` `:245`、`codex-sdk` `:289`、`codex-app-server` `:309`)→ OpenAI 账户,**不可能运行 claude-opus-5**。
- `collie-sidecar` 家族(`pi-rpc` `:329`、`prime-rpc` `:346`、`hermes-gateway` `:365`、`hermes-acp` `:384`)→ 第三方计费,且 `pi-rpc` 的 notes 明确写「the Claude route bills as extra usage: refused under no-paid-overage」(`:339-340`)。
- 只有 `collie`(原生,跟随 `PROVIDER` 设置)与 `claude-code`(`:262-282`)能跑 Anthropic 模型。

**结论**:「Claude Opus 5 harness benchmark」中,Collie 的合法参赛路径只有两条:`RUNNER=collie` 与 `RUNNER=claude-code`。把 `codex-exec` 的结果混进榜单是拿两个不同模型比 harness。

### 4.2 工具面积不对等,且差距是数量级的

- Collie → `claude-code`:`DEFAULT_TOOLS = ("Read","Edit","Write","Grep","Glob")`(`claude_code_runner.py:87`),**5 个文件工具,零 shell**。`_UNREVIEWABLE_TOOLS`(`:121-124`)硬性禁掉 `BASH`/`TASK`/`WEBFETCH`/`WEBSEARCH` 等;`_check_tools`(`:151-175`)对任何不在 `DEFAULT_TOOLS` 里的名字直接 `ValueError`。理由写在模块 docstring `:14-17`——没有回到 `harness/gate.py` 的审批通道,所以能跑的工具就是没人审过的工具。
- opencode:内置 shell(`ShellID.ToolID`,`prompt.ts:510`)、LSP(`prompt.ts:839 lsp.documentSymbol`)、MCP 工具与 resource 三件套(`session/tools.ts:27-31, 136-386`)、task 子 agent(`prompt.ts:255-449`)。

**任何"任务完成率"对比,在这个差距下测的是工具面积,不是 harness 质量。** 唯一可辩护的做法:把 opencode 的权限集裁到同样 5 个工具(见 §5 的 `OPENCODE_PERMISSION` 方案),或者把两边都放开——但后者 Collie 会拒绝(`runner_registry.py:1051-1054`:`read_only` 参数只允许 `claude-code`,其余直接 `ValueError`)。

### 4.3 鉴权路径与"环境卫生"不对称

Collie 对子进程环境做**主动剥离**:
- `runner_env.assert_no_billing_override(os.environ, self.credential_family)`(`agent_runners.py:959`)——父 shell 里存在 `OPENAI_BASE_URL` / `CODEX_API_KEY` 之类会**直接拒绝启动**,理由在 `:955-958`。
- `runner_env.child_env(self.env_policy)`(`:963`)返回**完整**子环境(不是增量),并产出 `last_env_receipt`(`:774`,只含变量名)。
- argv 层面:`--ignore-user-config --ignore-rules --strict-config -c approval_policy="never" -c web_search="disabled"`(`agent_runners.py:817-820`),resume 路径同样(`:849-854`)。docstring `:797-820` 逐条解释了为什么——`~/.codex/config.toml` 能注册 MCP server、开 web search、放宽 sandbox。

opencode 反向:配置加载**主动吸收宿主与网络状态**。`config/config.ts`:
- `:482-490` 读 `OPENCODE_CONFIG_CONTENT`;
- **`:492-528` 当账户带 `active_org_id` 时,向 `${activeAccount.url}/api/config` 发起网络请求并 merge**,还会把 token 写进 `process.env.OPENCODE_CONSOLE_TOKEN`(`:505-506`);
- `:530-536` 读 managed config dir;
- `:538-548` 读 macOS MDM `.mobileconfig`,注释写着 "override everything"。

**这意味着两台机器上的 opencode 可以有不同行为,而差异来自网络和 MDM。** 任何跨机器的 opencode 分数在未固定这些之前不可比。这一项必须写进 benchmark 的前置条件。

### 4.4 "被测对象"的默认值本身不同

`runner_registry.py:33-37` 的注释:默认 `RUNNER=collie` 路径**完全不会 probe 任何外部 CLI**,`cmd_run` 默认 `request_from_run` 只返回 `("collie",)`。所以「Collie 默认行为」= 原生环路,和「Collie 编排 codex」是两个产品。

### 4.5 opencode 非交互模式的隐藏权限策略

`cli/cmd/run.ts:430-448`:非交互(非 `--mini`)时**强制注入三条 deny 规则**——`question`、`plan_enter`、`plan_exit`,pattern `*`。加上 `:809-820` 对所有 `permission.asked` 自动 reject(除非 `--auto`/`--yolo`/`--dangerously-skip-permissions`)。
**因此 `opencode run` 的默认非交互行为 ≠ `opencode` TUI 行为。** 若 benchmark 用 `run` 而人类体验用 TUI,测的不是同一个东西。

### 4.6 停止条件不可比

- opencode:`agent.steps ?? Infinity`(`prompt.ts:1178`),到达上限只是追加 `MAX_STEPS_PROMPT` 提示,不硬停。
- Collie 原生:`turn_cap`(`loop.py:1780`)、`max_model_calls`(`:1311-1313`)、token/成本预算(`_over_budget` `:634`,`_budget_exceeded` `:316`,`_spend_exceeded` `:348`)、`shared_budget`(`:1314-1315`)。
- Collie → claude-code:`--max-budget-usd`(`claude_code_runner.py:376-378`),且 `--max-turns` 在 `_BANNED_ARGV`(`:145`)里被禁(理由见 `runner_registry.py:277`:该 flag 在 2.1.221 的 `claude --help` 中不存在)。

**必须统一为一个外部计量(壁钟 + 美元上限),而不是各自的内部 turn 上限。**

### 4.7 计费类别拒绝会直接改变样本量

`runner_registry.py:908-977 probe` + `runner_select.py` 的 H3/H5 规则:未证据化的计费路线(`billing_class="unknown"`)会被拒绝而不是乐观放行(`runner_registry.py:18-23` 明确写出这条原则)。在受控 benchmark 主机上这会表现为**部分 run 根本没跑**。这些必须记为 `refused`,不能计入分母,否则 Collie 被无谓惩罚。

---

## 5. Collie 的四类源码级潜在弱点

> 标注约定:**【源码确定】**=仅凭所引行即可判定;**【未复现】**=机制在源码中清晰,但触发条件未在本次只读审计中实证。

### W1 — `_SteerWatcher` 的 64 条上限会静默丢弃已被 ack 的指令 【未复现】

**位置**:`harness/runner_slice.py:552-553`
```python
if len(self._pending) < 64:
    self._pending.append(item)
```
**机制**:`_pending` 满 64 时,新 drain 出来的 item 被**直接丢弃**——既不进入 `_pending`,也不调用 `self._delivery(item, False, ...)` 发 NACK(对比 `:558` 和 `__exit__` 里 `:532-533` 都有 NACK)。而在 web 路径上,这些 item 在被 drain 返回**之前**就已经历 `web_tasks.claim_steer` → `journal_before_transport` → `ack_delivered`(`webapp.py:5845-5892`),即在持久化 inbox 中已被标记为「已投递/已消费」。
**触发条件**:runner 的 `steer_current` 持续返回 `False`(`:565-566` 会 `break` 内层 while,`_pending` 只增不减),同时用户在一个长 turn 内提交 > 64 条转向。
**未复现的原因**:`journal_before_transport` 会把文本写入 `journaled`,该 transcript 是否被下一轮 prompt 采纳,超出本次只读审计能确证的范围;因此我不能断言"指令永久丢失",只能断言"其投递状态与实际投递不一致,且缺少与相邻分支一致的 NACK"。
**修复**:第 552 行 `else:` 分支补 `self._delivery(item, False, "steering backlog is full")`,并让 webapp 侧据此 `release_undelivered`。

### W2 — 终结事件之后的任何记录都会把整轮判为协议错误 【源码确定,触发条件未复现】

**位置**:`harness/agent_runners.py:1150-1181`,关键三行
```python
1156  if saw_terminal:
1157      malformed = True
1158  events.append(event)
```
配合 `:1067` `protocol_error = invalid_json or bool(outcome.output_truncated)`、`:1082-1083` `settled = (... and not protocol_error ...)`、`:1116-1117` `recovery = abnormal and (mutated or (process_started and not complete))`。

**后果链**:Codex 在 `turn.completed` / `turn.failed` **之后**再吐任何一行(哪怕是无害的收尾记录)→ `invalid=True` → `protocol_error=True` → `settled=False` → 若本轮改了文件则 `mutated=True` → `recovery_required=True` → 下一次 `resume` 在 `agent_runners.py:831-834` 直接抛 `RecoveryRequiredError`。**一个良性的协议增补会把线程永久围栏。**

**为什么这不是理论问题**:`HarnessSpec` 只有 `min_version`(`runner_specs.py:607`),没有 runner 级 `max_version`;`runner_select.py:262-264` 只拒绝**更旧**的版本。也就是说 `0.149.0` 是地板,天花板不存在,任何更新的 Codex 都会被接受并可能触发此路径。同一模块内的 `_USAGE_CONTAINERS`(`:153-157`)恰恰是为「同一 CLI 在不同 build 间改名」而写的兼容层——说明作者已知这类漂移存在,但终结事件的处理没有同等宽容。
**未复现**:我未运行任何 Codex CLI,无法证明 0.149.0 之后的版本确实会追加记录。
**修复**:把 `saw_terminal` 后的**未知类型**记录降级为「忽略并计数」,只对第二个终结事件保留 `invalid=True`(`:1179-1180` 的 `terminal_count > 1` 已经单独覆盖了这一点)。

### W3 — Windows 上 `codex-exec` 的实测成功率是 1/4,却仍是 phase-1 可选项 【源码确定,为 Collie 自记证据】

**位置**:`harness/runner_registry.py:256-260`(spec notes 原文)
```
"Windows: writes are refused intermittently when launched through
 Collie's process-tree owner even with the sandbox overrides in
 place (1 of 4 runs succeeded, 2026-08-22 / 0.149.0). The turn still
 exits 0, so trust the verification gate, not the worker's summary",
```
配套证据:`_windows_sandbox_override`(`agent_runners.py:117-151`)整段 docstring 记录了两次实测——`--ignore-user-config` 使 `[windows] sandbox` 回落到 `Disabled`,导致每次写入被拒**而进程仍 exit 0**;`sandbox_private_desktop` 必须关掉,因为 Collie 的 start gate 用 `CREATE_NO_WINDOW` + 管道(`_START_GATE_SCRIPT:186`),没有可派生的交互桌面。

**产品影响**:`runner_registry.py:1013 option_keys()` 只按 `phase <= CURRENT_PHASE` 过滤,`codex-exec` 是 phase 1,因此在 Windows 上仍可被 `--runner codex-exec` 选中。缓解手段是 `windows_native=None`(`:147`)+ `apply_compat_report` 的降级(`:528-589`),但那只在**跑过 compat matrix 并落盘报告**之后生效——`autoload_compat_report`(`:445-464`)对缺失文件静默返回 `{}`,而缺失正是全新安装的常态(`:454` 注释自陈)。
**修复优先级最高**:见 §7。

### W4 — 压缩在预算/取消闸门后完全放弃,没有降级路径 【源码确定,后果未复现】

**位置**:`harness/loop.py:1309-1317`
```python
if self._cancel_requested():            return None
call_cap = max(0, int(getattr(self, "max_model_calls", 0) or 0))
if call_cap and model_calls >= call_cap: return None
if self.shared_budget is not None and self.shared_budget.exceeded(): return None
if self._over_budget(total):            return None
```
四个 `return None` 之后,调用方(`loop.py:1827`)拿到 `None` 即「本轮不压缩」,**继续把全量 thread 发给 provider**。在 `reason="threshold"` 的普通轮次这没问题;但 `_maybe_compact` 也承担溢出恢复(`forced = session.pop(FORCE_KEY)`,`:1280`),此时「不压缩」= 必然再次溢出 = 该轮必然失败。而 `1287-1289`、`1301-1305` 只在 `forced` 时 emit `skipped`,四道预算闸门的 `return None` 路径**连事件都不发**。

对照 opencode:同样的情形下 `processor.ts:621-631` 会设 `needsCompaction`,`prompt.ts:1149-1159` 无条件走 `compaction.process`,并有独立的 `prune` 路径(`compaction.ts:28-31` `PRUNE_MINIMUM=20_000`、`PRUNE_PROTECT=40_000`、`TOOL_OUTPUT_MAX_CHARS=2_000`)——即**先剪旧工具输出,再考虑摘要**,Collie 没有等价的无成本降级。
**未复现**:需要构造预算恰好在溢出点前耗尽的样本才能观察。
**修复**:把 `forced=True` 时的预算闸门改为「允许一次超额压缩」或引入无 LLM 成本的 prune(截断历史 tool 输出),并对所有 `return None` 路径补 emit。

### W5(附加)— `_never_reached_the_prompt` 的 fallback 判据依赖一个可能永远为假的前提 【未复现】

**位置**:`harness/runner_slice.py:400-427`,配合 `:206`
```python
if not (outcome.pre_prompt_failure and index + 1 < len(chain)):
    break
```
`_never_reached_the_prompt` 要求 `snapshot.mutation_check_complete` 为真(`:419-420`)才可能返回 True。而 `mutation_check_complete` 来自 `_mutation`(`agent_runners.py:1267-1272`),需要**前后两次 `workspace_snapshot` 都 `snapshot_complete=True`**。`verification._filesystem_snapshot`(`:187-263`)在遇到超过 `_SNAPSHOT_FILE_CAP` 的文件数、超过 `_SNAPSHOT_BYTE_CAP` 的字节数、任何 socket/FIFO/设备节点(`:249-253`)、或 Windows 保留设备名引发的 `ValueError`(`:256-261`)时都会置 `complete=False`。
**后果**:在**大仓库**上,`snapshot_complete` 恒为 False → `_never_reached_the_prompt` 恒返回 False → `pre_prompt_failure` 恒为 False → **`decision.fallback_chain` 永远不会被使用**。fallback 这个特性在最需要它的场景(大型真实代码库)下静默失效。
**未复现**:需要知道 `_SNAPSHOT_FILE_CAP` / `_SNAPSHOT_BYTE_CAP` 的具体数值与目标仓库规模的关系;这两个常量在 `verification.py` 顶部,本次未读取其值。
**修复**:让 fallback 判据在 `mutation_check_complete=False` 时回退到「`cursor` 未推进 且 `exit_code is None`」这条已经存在的更强证据(`:421-427`),而不是一票否决。

---

## 6. 为 opencode 设计的公平对比传输(全部基于源码,不发明 flag)

### 6.1 应当选哪条传输

opencode 有三条真实入口(`packages/opencode/src/index.ts:81-99` 的命令注册):

| 入口 | 源码 | 适配性 |
|---|---|---|
| `opencode run` | `cli/cmd/run.ts` | 一次性、有 `--format json`、退出即结束 |
| `opencode serve` + HTTP | `cli/cmd/serve.ts`,`server/routes/instance/httpapi/groups/session.ts` | 长驻、有 abort/prompt_async/fork/revert |
| `opencode acp` | `cli/cmd/acp.ts` | ACP 协议(Collie 已有 `hermes-acp` 先例) |

**推荐:`serve` + HTTP,而非 `run`。** 理由是场景 S4/S5/S11/S12 需要的四个原语只在 HTTP 面上存在:

- `POST /session/:sessionID/prompt_async` —— `groups/session.ts:96`,handler `handlers/session.ts:311-329`。返回 `NoContent`,内部 `Effect.forkIn(scope, {startImmediately:true})`。**这是 opencode 唯一的 steering 通道。**
- `POST /session/:sessionID/abort` —— `groups/session.ts:91`。
- `POST /session/:sessionID/fork` —— `groups/session.ts:90`。
- `POST /session/:sessionID/revert` / `unrevert` —— `groups/session.ts:99-100`。
- `POST /session/:sessionID/permissions/:permissionID` —— `groups/session.ts:101`,对应 `permissionRespond`。**这是把 opencode 的审批接回 Collie `harness/gate.py` 的接点**,能让 opencode 成为 Collie 生态里第二个 `approval_round_trip=True` 的 runner(目前只有 `codex-app-server` 和 `hermes-gateway`)。
- 事件流:`groups/event.ts`(`run.ts:834` 的 `client.event.subscribe()` 即消费它)。
- 提示词投递:`POST /session/:sessionID/message`(注意路径,`SessionPaths.prompt = ${root}/:sessionID/message`,`groups/session.ts:95`——**不是 `/prompt`**)。

工作目录路由由 `x-opencode-directory` 头承担(`cli/cmd/serve.ts:10-11` 注释;`middleware/workspace-routing.ts`)。

### 6.2 隔离配置(全部是源码中真实存在的环境变量)

要达到 Collie 对 `codex exec` 那种"不继承宿主人格"的等价隔离,可用的开关如下。每一项都给出源码出处:

| 变量 | 出处 | 作用 |
|---|---|---|
| `OPENCODE_CONFIG_CONTENT` | `config/config.ts:482-490` | 内联 JSON 配置,merge 到 `"local"` 层 |
| `OPENCODE_DISABLE_PROJECT_CONFIG` | `config/paths.ts:27`,`config/config.ts:420`,`session/instruction.ts:81` | 不读项目内 `.opencode` / AGENTS 类文件 |
| `OPENCODE_PERMISSION` | `config/config.ts:559-565` | JSON,merge 进 `result.permission` |
| `OPENCODE_PURE` | `index.ts:62-71`,`effect/runtime-flags.ts:18` | 不加载外部插件 |
| `OPENCODE_DISABLE_DEFAULT_PLUGINS` | `runtime-flags.ts:19` | 关掉默认插件 |
| `OPENCODE_DISABLE_EXTERNAL_SKILLS` | `runtime-flags.ts:21` | 关掉外部 skills |
| `OPENCODE_DISABLE_LSP_DOWNLOAD` | `runtime-flags.ts:22` | 禁止运行时下载 LSP |
| `OPENCODE_DISABLE_CLAUDE_CODE` / `_PROMPT` / `_SKILLS` | `runtime-flags.ts:23-29` | 关掉 Claude Code 兼容注入 |
| `OPENCODE_DISABLE_SHARE` | `share/share-next.ts:23`(接受 `"true"` 或 `"1"`) | 禁止分享外发 |
| `OPENCODE_DISABLE_EMBEDDED_WEB_UI` | `runtime-flags.ts:20` | 关内嵌 Web UI |
| `OPENCODE_DISABLE_AUTOCOMPACT` | `config/config.ts:593-595` → `compaction.auto=false` | **S6 的对照组开关** |
| `OPENCODE_DISABLE_PRUNE` | `config/config.ts:596-598` → `compaction.prune=false` | **S6 的对照组开关** |
| `OPENCODE_SERVER_PASSWORD` / `OPENCODE_SERVER_USERNAME` | `cli/cmd/serve.ts:15-17`,`effect/config-service.ts:33-34` | Basic auth;未设时 serve 会打印 unsecured 警告 |
| `OPENCODE_PRINT_LOGS` / `OPENCODE_LOG_LEVEL` | `index.ts:53-68` | stderr 日志;`log-level` 取值受限于 `["DEBUG","INFO","WARN","ERROR"]` |

**必须显式处理但没有开关的一项**:`config/config.ts:492-528` 的组织配置远程拉取。它由 `accountSvc.active()` 返回带 `active_org_id` 的账户触发,没有 env 开关可以关闭。**唯一可靠的隔离办法是让 benchmark 用一个没有 org 绑定的账户**,并在每次 run 前用 `opencode account`(`cli/cmd/account.ts`)确认。同理 `:538-548` 的 macOS MDM 路径在 macOS runner 上不可关闭——建议 benchmark 主机不用 macOS,或明确记录 MDM 状态。

### 6.3 建议的 `HarnessSpec` 形状(供 `runner_registry._SPECS` 增补)

按现有表的字段语义(`runner_specs.py:600-650`)与 `RunnerCapabilities` 的诚实性约定,`opencode` 应声明为:

```
key="opencode", label="opencode (serve + HTTP)", kind="external",
binary="opencode", version_argv=("opencode","--version"),   # index.ts:51 .version(...)
credential_family="collie-sidecar",   # 计费路线在 opencode 自己的 account 上,此处无证据
env_policy="sidecar-harness",
guard_alias="",                       # 无 Collie 侧计费证据 -> billing_class 保持 unknown
phase=<下一个未发布阶段>,
caps=RunnerCapabilities(
    protocol="opencode-httpapi-v1",
    session_create=True,              # POST /session
    session_resume=True,              # GET /session/:id + POST /session/:id/message
    session_fork=True,                # POST /session/:id/fork
    streaming=True,                   # GET event subscribe
    cursor_replay=False,              # 事件流无 cursor 语义(未在树内确认)
    steer=True,                       # POST /session/:id/prompt_async
    follow_up=True,                   # 同上
    cancel="native+process-tree",     # POST /session/:id/abort + Job/pgroup
    approval_round_trip=True,         # POST /session/:id/permissions/:permissionID
    usage_tokens=None_or_False,       # step_finish part 形状不在树内 -> 不得声明 True
    usage_cost=False,
    confinement="permission-ruleset",  # 非沙箱;OPENCODE_PERMISSION 是规则集不是边界
    tools=frozenset({"code","bash","mcp"}),
    prompt_transport="http",
    interactions=frozenset({"approval"}),
    capability_handshake=True,
    windows_native=None),             # 由 compat 报告决定
notes=(
    "org-bound accounts fetch remote config from <account.url>/api/config "
    "(config.ts:492-528); benchmark hosts must use an account without active_org_id",
    "macOS managed preferences override all config (config.ts:538-548)",
    "non-interactive `run` injects deny rules for question/plan_enter/plan_exit "
    "(cli/cmd/run.ts:430-448); the HTTP transport does not, so parity requires "
    "sending the same ruleset in the session create payload",
)
```

两点必须坚持 Collie 现有的诚实性纪律:

1. **`usage_tokens` 不得声明 `True`**。`agent_runners._merge_usage` 的注释(`:1288-1292`)确立了原则——「缺失 ≠ 零」。opencode 的 token 形状在 pinned 树内不可见,声明 True 就是猜。走 `runner_compat` 的 `usage` 列去证据化。
2. **`confinement` 不得写 `"workspace-write"`**。opencode 的权限是 ruleset 评估(`Permission.evaluate` / `Permission.merge`,`prompt.ts:346, 975`),不是 OS 级沙箱。按 `COLLIE_CAPABILITIES` 里 `confinement="none"` 的自陈逻辑(`runner_registry.py:104-108`——"claiming a sandbox Collie does not have would cost a great deal"),这里同样必须诚实。

### 6.4 传输实现要点

- **进程归属**:复用 `SubprocessRunner` 的 gate 模式(`agent_runners.py:166-195, 557-633`)启动 `opencode serve`,让 Job Object / process group 覆盖服务端及其派生的所有 shell 子进程 —— 这正是 S12 要验证的东西,也是 opencode 自身(`forceKillAfter: "3 seconds"`,`prompt.ts:564`)不提供的保证。
- **事件游标**:opencode 事件流无 cursor,断线重连会丢事件。因此 `cursor_replay=False`,并且 snapshot 的权威来源应是 `GET /session/:sessionID/message`(`groups/session.ts:85`)的轮询对账,而非事件流本身。
- **审批**:把 `permission.asked` 事件桥接到 `set_approval_callback`(`runner_slice.py:334-338` 已有该 seam),fail-closed 默认 reject —— 与 `cli/cmd/run.ts:809-820` 的默认行为一致。
- **不要用 `--attach`**:`cli/cmd/run.ts:839-843` 的 `finish()` 在 attach 模式下直接 `return`,不等待完成、不设退出码。作为测量传输不可用。

---

## 7. 修复优先级(按产品影响排序)

| # | 事项 | 依据 | 影响 | 成本 |
|---|---|---|---|---|
| **P0** | **Windows 上把 `codex-exec` 从 `option_keys()` 中移除,直到 compat 报告证据化** | `runner_registry.py:256-260`(自记 1/4 成功)、`:147` `windows_native=None`、`:1013` `option_keys` 只看 phase | 用户在 Windows 上选中它,得到 exit 0 + 零变更 + 「已完成」摘要。这是所有失败模式里最伤信任的一种——它伪装成成功 | 低:在 `option_keys()` 加 `windows_native is not False` 过滤 |
| **P0** | **给 runner spec 增加 `max_version` 上界并在 `runner_select` 中执行** | `runner_specs.py:607` 无 `max_version`;`runner_select.py:262-264` 只拒更旧;W2 的围栏链 `agent_runners.py:1156-1157 → 1067 → 1082 → 1117 → 831` | 一次 Codex 自动升级即可让所有已改文件的线程被永久 `RecoveryRequiredError` 围栏。用户无自助恢复路径 | 低:字段 + 一个比较 |
| **P1** | **W2:放宽终结事件之后的未知记录** | `agent_runners.py:1150-1181` | 同上,但这是根因修复而非闸门 | 中:需要与 `terminal_count > 1`(`:1179-1180`)的既有语义划清界限 |
| **P1** | **W4:溢出强制压缩时给出无 LLM 成本的降级(prune)** | `loop.py:1309-1317`;对照 `opencode compaction.ts:28-31` | 预算耗尽 + 上下文溢出 = 必然失败且无事件。长任务上这是最常见的"莫名其妙断掉" | 中:新增 prune 路径 |
| **P1** | **W5:让 fallback 判据不依赖 `snapshot_complete`** | `runner_slice.py:419-420` + `verification.py:187-263` | fallback 在大仓库上静默失效——恰好是最需要它的场景 | 低:改判据顺序,复用 `:421-427` 的现有证据 |
| **P2** | **W1:补 backlog 溢出的 NACK** | `runner_slice.py:552-553` vs `:558, :532-533` | 需要 >64 条待发转向才触发;真实影响窄,但"投递状态与事实不符"违反该模块的核心承诺 | 极低:一行 `else` |
| **P2** | **给 `codex-exec` 加一条"pseudo-steer"路径** | `runner_registry.py:127-128`;`webapp.py:5824` | 目前用户在 codex-exec 上的转向只能等下一轮。可在 turn 结束后自动把 backlog 拼进 `resume` 的 prompt,并如实标注为 "delivered at next turn",而不是 `steer=True` | 中 |
| **P3** | **接入 opencode runner(§6)** | — | 直接收益:第二个 `approval_round_trip=True` 且能跑 Opus 5 的 worker,补上 `claude-code` 无 shell 的空缺 | 高 |

---

## 8. 给 benchmark 设计者的三条硬约束

1. **榜单必须按"路径"而非"产品"分列**:`collie-native`、`collie→claude-code`、`opencode-http` 是三个条目。合并即无效。
2. **S2/S3 这类场景必须双向计分**:Collie 在 `recovery_required` 时拒绝续跑是**设计目标**(`sessions.py:438-441` 的 reason 文本写得很清楚)。用单一"任务完成率"会把这个安全属性计成失败。建议同时报 `completion_rate` 与 `unattested_side_effect_rate`。
3. **opencode 的宿主状态必须在每次 run 前快照并记录**:账户 `active_org_id`、`~/.config/opencode` 与 `OPENCODE_CONFIG_DIR` 内容哈希、平台是否有 MDM。依据 `config/config.ts:492-548`。缺这一项,跨机器结果不可比。

---

需要我继续深入的话,以下三处是我这轮没有展开、但对场景 S6/S7/S12 的判据落地最关键的:`harness/compaction.py` 的 `plan`/`prepare` 具体阈值、`verification.py` 顶部的 `_SNAPSHOT_FILE_CAP`/`_SNAPSHOT_BYTE_CAP` 常量值(直接决定 W5 的触发规模)、以及 `harness/loop.py:2265-2600` 的 `execute_code` RPC 内嵌工具边界。