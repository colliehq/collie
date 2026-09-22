# Collie × goose 执行层深度对照审计

审计对象:`C:/workspace/collie`(`collie-harness`,Python,包名 `harness/`)与 `C:\workspace\collie-product-2026-09-06\references\goose`(workspace version **1.49.0**)。只读,未联网,未读取任何凭据/会话/评分器文件。

**可信度声明(重要)**:pinned goose 检出只包含 `crates/goose` 与 `crates/goose-cli`(`Cargo.toml:2` `members = ["crates/*"]`,实际磁盘上只有这两个 crate)。`goose-providers`(`Conversation`、`ModelConfig`、`ProviderUsage`、定价)与 `goose-context-management`(`DEFAULT_COMPACTION_THRESHOLD` 的字面值)**不在本地**。凡涉及这两个 crate 内部实现的结论,我一律标注为「未验证」,不据此下判断。

---

## 一、执行路径的实际差异(基于源码,非 README)

### 1.1 主循环形状

| 维度 | Collie | goose |
|---|---|---|
| 循环体 | `harness/loop.py:1780` `for turn in (range(turn_cap) if turn_cap else itertools.count())` | `agents/agent.rs:2558` `loop {}`,内层 `agent.rs:2698` 消费 provider 流 |
| 轮次上限默认 | `settings.py:294` `MAX_TURNS` 默认 `"0"` = **无限**;`cli.py:332` 又把它硬夹到 `min(120, ...)` | `agent.rs:90` `DEFAULT_MAX_TURNS: u32 = 1000` |
| 轮次记账 | 每次 `continue` 都推进 `turn`,**harness 注入的 nudge 也消耗预算** | `agent.rs:2638-2644`:`retrying_after_stop_hook_denial` / `retrying_after_empty_turn` **不递增** `turns_taken` |
| 空响应 | 无专门处理;走 `loop.py:2885` 的"最终合成"补一次无工具调用 | `agent.rs:3443` `MAX_EMPTY_TURN_RETRIES = 3`,再失败发 `EMPTY_TURN_MESSAGE` |

Collie 单轮内可被 harness 消耗的轮次上界:`verify_max`(默认 2,`--verification required` 提到 4)+ `coverage_max` 2 + `critic_max` 2 + `rollback_rounds` 1 + `edit_forced` 1 + `hook_stop_rounds` 3 ≈ **最多 11 轮**用于自注入消息。给两边设同一个 `--max-turns` 是**不等价**的。

### 1.2 工具执行:串行 vs 并发 —— 最大的结构差异

Collie 两遍式:`loop.py:2565-2568` 先对整批 `comp.tool_calls` 全部 `_prepare_tool_call`(仓修 + 授权 + `PreToolUse`),`loop.py:2571-2588` 再**严格串行**逐个 `_execute_prepared_tool`。

goose:`agent.rs:920-951` `handle_approved_and_denied_tools` 对每个已批准请求立即 `dispatch_tool_call` 拿到 `ToolStream`,`agent.rs:2892-2899` `stream::select_all(with_id)` **并发轮询**全部工具流,`agent.rs:2907` `tokio::select!` 收结果。

后果:模型一轮发 4 个 `bash`,goose 墙钟 ≈ max(t₁..t₄),Collie ≈ Σtᵢ。Opus 5 会大量批量发工具调用,这一项单独就能主导墙钟排名。

### 1.3 工具产出上限与超时

| | Collie | goose |
|---|---|---|
| shell 默认超时 | `tools.py:485` **120s**,clamp `[1,600]` | `config/extensions.rs:10` `DEFAULT_EXTENSION_TIMEOUT = 300`,`shell.rs:549` `resolve_shell_timeout` |
| 输出上限 | `tools.py:546` **8000 字符**(保尾部 + 溢出文件) | `shell.rs:158-159` `OUTPUT_LIMIT_LINES = 2000` / `OUTPUT_LIMIT_BYTES = 50_000` |
| 中断语义 | `tools.py:502-532`:区分 `PRELAUNCH_CANCELED` / `CANCELED` / `TIMEOUT` / `HANDOVER_ERROR`,并在 `tree_terminated=False` 时明说"子进程可能仍在写文件" | `shell.rs:597-630` `tokio::time::timeout` + kill;无"树未确认停止"的显式声明 |

一个 150 秒的测试套件:goose 默认通过,Collie 默认被杀。这不是模型能力差异。

### 1.4 上下文管理:两条完全不同的曲线

**Collie 无条件历史裁剪**(`context.py:364-383`):
```python
window = 4 if shrink else 14
stub    = 120 if shrink else 240
... if m["role"]=="tool" and i < len(msgs)-window and len(c) > stub:
        content = c[:stub] + " …[older tool output elided]"
```
超过最近 **14 条消息**(≈7 个工具往返)的**所有**工具输出被截到 **240 字符**,与模型窗口无关、与是否接近上限无关。

**Collie 语义压缩**(`compaction.py:175`):`threshold_tokens: int = 48_000`,绝对值默认,注释明说"Collie 没有可信的 per-model 上下文窗口元数据"。`loop.py:1827` 在**每一轮**调用 `_maybe_compact`。

**goose**:`context_mgmt/mod.rs:237-240` 阈值是 `GOOSE_AUTO_COMPACT_THRESHOLD`(比率,回落到 `DEFAULT_COMPACTION_THRESHOLD`,测试注释 `mod.rs:1268` 写 "Default threshold (0.8)"),`mod.rs:269-272` `usage_ratio > threshold`,基于 `context_limit`。且这个主动检查在 `agent.rs:2298`,位于 `reply()` 内、`reply_internal()` **之前** —— 即**每条用户消息只查一次**,整个多轮 agentic turn 内不再主动压缩;turn 内只有:
- `agent.rs:2668-2679` `maybe_summarize_tool_pairs`(按 `tool_call_cut_off`,由 `mod.rs:367-373` `context_limit * threshold` 算出),
- `agent.rs:3179-3238` 被动的 `ContextLengthExceeded` 压缩,`compaction_attempts >= 2` 就放弃。

净效果:在 Opus 5 的大窗口下,**Collie 会在 goose 完全不动的区间里反复丢信息 + 额外花 provider 请求**;而在极长任务里,goose 又更容易撞到硬上限后只有 2 次挽救机会。

### 1.5 中断与会话恢复 —— Collie 明显更强,goose 有真实缺口

Collie 在**每个边界**写日志:`loop.py:1880`(`calling_model`)、`1903`(`model_complete`)、`2320`(`executing_tool`,含 `replay_safe` 证明)、`2425`(`tool_complete`/`external_action`)、`3100/3103`(终态)。`sessions.py:444-449` `_recovery_required` 把 `executing_tool`/`external_action` 且非 host 认证只读的边界标为**必须人工核对**;`sessions.py:463-475` `_replay_safe_read` 只承认 `read_file/glob/grep/memory_search/delegate` 这五个内建实现(而非工具名或 MCP 只读提示)。`loop.py:1184-1227` `_close_unanswered_calls` 对"正在跑"和"没开始"给出不同文本。`loop.py:3015-3033` 把 `KeyboardInterrupt` 当作正常 cancel 结局而非崩溃。

goose:
- `agent.rs:3554-3557` 本轮 `messages_to_add`(assistant tool_request + tool_response)**直到外层迭代末尾才批量落盘**。进程在工具执行中被杀 → 该轮完全不在会话文件里,**没有任何"某工具已产生副作用"的记录**。
- Ctrl-C 的修复在 CLI 层:`goose-cli/src/session/mod.rs:1851-1928` `handle_interrupted_messages`,而 `mod.rs:2255-2257` `push_message` **只写内存 `self.messages`,不调用 `session_manager`** —— 修复消息不持久化。
- `agent.rs:2902-2960` 工具收集循环在 `is_token_cancelled` 时 `break`,随后 `agent.rs:3136-3139` 仍用 `request_to_response_map.remove(...).unwrap_or_else(|| Message::user()...)` 构造 `final_response`;未返回的工具会得到一条**不含 `ToolResponse` 的空 user 消息**。是否在别处被修复取决于 `Conversation` 的实现 —— **未验证**(`goose-providers` 未 vendored)。

### 1.6 队列 / steering

| | Collie | goose |
|---|---|---|
| 易失队列 | `loop.py:559` `self.steering` 回调,`loop.py:1804` 轮首、`loop.py:2802` 收尾前各抽一次 | `agent.rs:307` `steer_queues: Mutex<HashMap<String, SteerQueue>>`,`agent.rs:2563` 轮首抽 |
| 持久队列 | **有**:`task_inbox.py` 文件存储 + `session_owner` 字节锁租约;`loop.py:1006-1107` `_consume_durable_steering` 严格 claim→append→checkpoint→ack 顺序,失败即中止整个 run | **无**。纯内存,`agent.rs:580` `discard_pending_steers` 直接丢弃 |
| 首轮可抽 | 是(`loop.py:1816`,turn 0 即抽) | 否 —— `agent.rs:2531` `can_drain_pending_steers = false`,`agent.rs:3321` 才置 `true`。**第一轮 provider+工具执行期间到达的 steer 要等到第二轮才被看到** |
| 注入入口 | `task_inbox.enqueue(...)` 纯文件 API(`task_inbox.py:614`);或 `webapp.py` HTTP | 仅 ACP:`acp/server.rs:2353` `on_steer_session` → `agent.steer()`。`acp/server.rs:1982` 无活动 run 时报 "no active run to steer" |

**`goose run` 子命令没有任何 steering 注入点。**

### 1.7 权限门

- Collie:`loop.py:723-724` `gate=None` → 完全放行(benchmark/embedded 路径);`loop.py:755-765` 有 gate 但 `approve is None` → **拒绝**并告知模型"没人能批准,去做不需要批准的部分"。`loop.py:743-748`:审计账本写入失败会**阻止**已放行的高风险动作。
- goose:`goose-cli/src/session/mod.rs:1594-1613` 非交互模式下,`GooseMode::Approve/SmartApprove` **直接报错退出**,其余模式 `Permission::AllowOnce` **自动放行**。

两边 headless 默认语义相反。

---

## 二、8-12 个可区分、可复现的场景

统一前置(所有场景):

```
BENCH_ROOT/<scenario>/<harness>/<seed>/
  repo/           # git init 后的固定仓库快照(每次 run 前 git clean -xfd && git reset --hard)
  home/           # HOME / USERPROFILE 覆写
  out/            # stdout.json / stderr.ndjson / 外部判定产物
```
环境隔离:Collie `COLLIE_STATE_DIR`、`COLLIE_SESSIONS_DIR` 指向本 run 私有目录;goose `XDG_CONFIG_HOME` / `GOOSE_...` 同理(见第四节)。所有判定用 **run 之外的独立脚本**读 `repo/` 与 `out/`,不读任何模型自述文本。

---

**S1 · 批量工具并发度(墙钟纯度)**

- 初始状态:`repo/` 内 `slow/a.sh`..`slow/d.sh`,每个 `sleep 20 && echo DONE_<x> > /tmp/<run>/<x>.stamp`(用 run 私有临时目录)。
- 提示词:`Run all four scripts under slow/ (a.sh, b.sh, c.sh, d.sh). Issue all four shell calls in a single response. Then report which finished.`
- 注入:无。
- 外部判定:四个 stamp 均存在;记录 `wall_ms`。**判别量 = wall_ms**。预期 goose ≈ 22-30s(`agent.rs:2899` select_all),Collie ≈ 82-90s(`loop.py:2571` 串行)。
- 注意:必须报告"并发度"而非"速度",否则该场景会被误读为模型差异。

---

**S2 · 单命令超时边界**

- 初始状态:`repo/Makefile` 的 `make slowtest` 执行 `sleep 150 && exit 0`。
- 提示词:`Run \`make slowtest\` and tell me its exit code. Do not modify the Makefile.`
- 外部判定:stdout 中最终答案是否包含真实退出码 0,且 `repo/` 未被修改(`git diff --exit-code`)。
- 依据:Collie `tools.py:485` 默认 120s → `ERROR: command timed out after 120s (killed)`;goose `extensions.rs:10` 300s → 通过。Collie 只有在模型主动传 `timeout_s=200` 时才能过 —— 这正是"harness 默认值 vs 模型自救"的可测分离。

---

**S3 · 大工具输出的保真度**

- 初始状态:`repo/gen.py` 打印 40 000 行,其中第 27 314 行为 `MARKER_7Q4X=<随机 12 hex>`,其余为噪声。
- 提示词:`Run \`python gen.py\` and tell me the value assigned to MARKER_7Q4X.`
- 外部判定:答案中的 hex 是否与生成器种子一致(判定脚本自己重算)。
- 依据:Collie `tools.py:546` 截到尾部 8000 字符 + 溢出文件(marker 在首行),模型必须 `read_file`/`grep` 溢出文件才拿得到;goose `shell.rs:159` 50 000 字节 + 截断通知。测的是**恢复路径是否可用**,不是记忆力。

---

**S4 · 长历史下的早期事实召回(裁剪 vs 压缩)**

- 初始状态:`repo/` 内 30 个各约 400 行的源文件;`repo/config/ORACLE.txt` 含 `DEPLOY_TOKEN_HINT=<12 hex>`。
- 提示词:`First read config/ORACLE.txt. Then, one file at a time, read every file under src/ and give me a one-line summary of each. When you are done, repeat the exact value of DEPLOY_TOKEN_HINT.`
- 外部判定:最终答案的 hex 是否精确匹配。同时从事件流统计 Collie `compaction` 事件次数与 goose `HistoryReplaced` 次数。
- 依据:`context.py:365-366` 会把 14 条消息之前的 `ORACLE.txt` 读取结果截到 240 字符;`compaction.py:175` 48k 阈值会在 goose 完全不压缩的区间触发。这是**信息损失曲线**的直接测量。

---

**S5 · 输出 token 截断恢复**

- 初始状态:空仓 + `spec.md` 要求生成一个约 1800 行、结构严格的 `generated/table.py`。
- 提示词:`Create generated/table.py exactly as spec.md describes. It is long; write the whole file.`
- 外部判定:`python -c "import ast;ast.parse(open('generated/table.py').read())"` 通过,且行数 ≥ 1700。
- 依据:Collie `providers.py:587` `default_max_tokens = 8192` → 大概率 `stop_reason == "length"`,触发 `loop.py:2105-2137`:整批工具调用被 `TRUNC_MSG` 作废(**不执行**),`loop.py:2124-2129` 把 `provider.max_tokens` 翻倍(上限 32768),`trunc_rounds >= 3` 放弃。goose 侧 `agent.rs:2728` 只记录 `output_token_limit_reached`,不作废工具调用。语义完全不同,是最干净的判别场景之一。
- **副作用观测点**:同进程连跑两次,记录第二次的输出上限 —— Collie 的 `max_tokens` 提升不会复位(见 W6)。

---

**S6 · 运行中 steering(转向被接纳的语义)**

- 初始状态:`repo/` 中有 `alpha/` 与 `beta/` 两棵目录。
- 提示词(初始):`Add a docstring to every function in alpha/. Work file by file.`
- 注入:外部注入器等待事件流出现**第 2 次** `tool` 事件(Collie:stderr NDJSON `type=="tool"`;goose:stdout `StreamEvent::Message` 内首个 `ToolRequest`)后 500ms,注入 `STOP working on alpha/. Do beta/ instead. Do not touch any more files under alpha/.`
  - Collie:`python -c "from harness import task_inbox; task_inbox.enqueue(SID,'s1',TEXT,mode='steer')"`(纯文件,`task_inbox.py:614`)
  - goose:ACP `_goose/unstable/session/steer`(`acp/server.rs:2353`)
- 外部判定:`git diff --name-only` 中 `alpha/` 被改文件数 ≤ 注入时刻已改数 + 1;`beta/` 至少 1 个文件被改。
- 已知偏差(必须记录):goose 的 `can_drain_pending_steers`(`agent.rs:2531/3321`)使第一轮内的 steer 延后一整轮生效,因此"注入时刻"必须按**工具事件序号**而非墙钟对齐。

---

**S7 · 中断后的会话恢复保真度(硬杀)**

- 初始状态:`repo/` 干净 git 树。
- 提示词:`Append a line "// touched" to src/a.rs, then run \`sleep 60\`, then append the same line to src/b.rs.`
- 注入:检测到 `sleep 60` 对应的工具开始事件后 3s,`SIGKILL` 整个进程树(Windows 用 `taskkill /T /F`)。
- 外部判定(全部外部读盘,不重启 agent):
  1. `src/a.rs` 已被修改(两边应都成立);
  2. **会话文件是否记录了 `a.rs` 的编辑**;
  3. **会话文件是否记录了 `sleep 60` 这次工具调用处于未完成状态**。
- 依据:Collie 在 `loop.py:2320` 已写 `executing_tool` 检查点,`sessions.py:444` 会把它标为 `recovery_required`;goose 的本轮消息要到 `agent.rs:3554` 才落盘,预期 (2)(3) 均为否。这是 Collie 明确胜出的场景,必须放进套件以避免整体报告偏向 goose。

---

**S8 · Ctrl-C 后续接(软中断 → resume)**

- 同 S7 初始状态与提示词,但发 `SIGINT`(Windows:`CREATE_NEW_PROCESS_GROUP` + `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT)`),等进程自行退出。
- 第二次调用:Collie `collie run "Continue what you were doing." --continue`;goose `goose run -t "Continue what you were doing." --resume --session-id <SID>`。
- 外部判定:第二次 run 是否成功启动且 provider 未因悬空 tool_use 报 400;`src/b.rs` 最终是否被修改;Collie 侧额外检查 stdout JSON 的 `recovery_required` 字段(`cli.py:2283`)是否为 `true`。
- 依据:Collie `sessions.py:491-515` `_resume_messages` 会为未配对调用回填 `RECOVERY:` 结果;goose 的修复在 `session/mod.rs:1851` 但不落盘(`mod.rs:2255`)。goose 侧结果取决于未 vendored 的 `Conversation` 校验 —— **本场景对 goose 的预期标为未验证,只报观测值**。

---

**S9 · 无人值守下的权限语义**

- 初始状态:`repo/` 内 `deploy.sh` 会执行 `curl` 到 `127.0.0.1:9/nope`(必然失败的本地丢弃端口,不产生真实外发)。
- 提示词:`Read deploy.sh, then run it, then tell me what happened.`
- 运行方式:stdin 非 TTY(管道),两边均 headless。
- 外部判定:进程退出码;答案中是否声称已执行;`out/` 中是否存在 `deploy.sh` 写下的 `ran.stamp`。
- 依据:Collie `cli.py:2061` `has_approver` 为假 → `h.approve` 未设 → `loop.py:755-765` **拒绝**并给出可绕行说明;goose `session/mod.rs:1609-1612` 在 `Auto` 下 `warn!` 后 `AllowOnce`,在 `Approve/SmartApprove` 下 `session/mod.rs:1603` **整个 run 失败**。三种结局互不可比 —— 这个场景的价值是**证明"通过率"在跨 harness 时无意义**,必须作为校准场景而非评分场景。

---

**S10 · 验证门的假阴性(Collie 专有风险,goose 作对照)**

- 初始状态:`repo/` 一个已知 bug + `tests/test_bug.py`。
- 提示词:`Fix the bug so that tests/test_bug.py passes. Verify by running the tests.`
- Collie 运行 `--verification required`(`cli.py:391-397` → `verify_gate=True, require_assert=True`)。
- 两个子例:
  - **S10a**:提示词追加 `Run the tests as a single shell command.`
  - **S10b**:提示词追加 `Run the tests as a multi-line shell script that first cd's into the repo root and then invokes pytest.`
- 外部判定:判定脚本自己执行 `python -m pytest tests/test_bug.py -q` 判断真实通过与否;再对比 Collie stdout JSON 的 `error` 是否为 `verification required but no executed post-edit assertion passed`(`loop.py:2980`)。
- 依据:`loop.py:189-246` `_has_unsafe_test_shell_control` 对任何未加引号的 `\n`/`;`/`|` 返回 `True`(`loop.py:228`),多行命令因此**永远不算证据**。S10b 预期出现"测试真的过了但 run 被标 FAILED"。

---

**S11 · 拒绝作为证据的误判(退出码 0 但首行是 ERROR)**

- 初始状态:`repo/check.py` 修好后运行时打印 `ERROR-level events: 0` 作为**第一行**,随后 `assert ok`,`exit 0`。
- 提示词:`Fix src/mod.py so that \`python check.py\` succeeds, then run it to prove it.`(Collie 用 `--verification required`)
- 外部判定:判定脚本独立跑 `python check.py` 确认 `returncode == 0`;对照 Collie 的 `res.verified` / `error`。
- 依据:`loop.py:380-389` `_repro_failed` 判据是 `o.startswith("ERROR") or o.startswith("[exit")`,而 `tools.py:533` 在 `returncode == 0` 时 `head = ""`,于是工具输出**以 stdout 首字符开头**。首行以 `ERROR` 开头的通过命令会被判为失败。

---

**S12 · 高并发下的会话身份完整性**

- 初始状态:同一 `COLLIE_STATE_DIR` / 同一 goose 配置根下,**同时**启动 64 个独立任务(不同 `repo/`,同一秒内 fork)。
- 提示词:每个 run 用唯一 marker,`Write the single line <MARKER_i> into out/marker.txt and stop.`
- 外部判定:枚举会话存储中的所有会话文件,检查是否存在**任一会话文件同时包含两个不同 MARKER**;统计因 ownership 被拒的 run 数。
- 依据:`sessions.py:131-132` `new_id()` = `strftime("%Y%m%d-%H%M%S") + os.urandom(2).hex()`,只有 **16 bit** 随机;`sessions.py:251` `merged = old + new[common:]` 会把两段无公共前缀的对话**拼接**;`session_owner.py:205-213` 是**非阻塞**字节锁,同 id 并发的另一方直接 `OwnershipRefused`(`loop.py:1511-1515`)。goose 侧用 `--session-id` 显式指定即可规避(`goose-cli/src/cli.rs:89`),Collie 的 `collie run` **没有等价开关**,只能靠 per-run `COLLIE_SESSIONS_DIR`。

---

## 三、使朴素排名失效的模型 / 工具 / 认证差异

**必须在跑分前固定或显式声明的项:**

1. **`max_tokens` 默认值不同,且 Collie 会在运行中改它。** `providers.py:587` `AnthropicProvider.default_max_tokens = 8192`;`loop.py:2124-2129` 在截断后 `self.provider.max_tokens = min(32768, cur*2)` 且**永不复位**。goose 的默认来自 `ModelConfig`(**未验证**,`goose-providers` 缺失)。→ 必须两边显式 pin:Collie `COLLIE_MAX_TOKENS`,goose 通过 `ModelConfig`/配置。

2. **扩展思考默认状态不同。** Collie 只在 `COLLIE_THINKING` 被设时开启,且 OAuth 路径下是 `{"type":"adaptive"}`(`providers.py:868-897`),并把 `max_tokens` 抬到 `max(self.max_tokens, 32000)`(`providers.py:877`)。goose 有 `ThinkingEffort` / `thinking_effort_support()`(`agent.rs:3659`),默认值未验证。**不 pin 就是在比两个不同的推理预算。**

3. **认证路径改变行为,不只是改变凭据。** Collie 有 4 条互不等价的 Anthropic 路径:`anthropic`(API key)、`anthropic-oauth`/`claude-sub`(`providers.py:803`,`subscription_only=True` 时走 frozen direct route、独有 beta `oauth-2025-04-20`、独有 UA、并且 `_spend_exceeded` 里 `subscription_only` 使 **$ 预算完全失效**,`loop.py:353-358`)、`claude-cli`(`providers.py:996`,子进程)、`claude-agent-sdk`(`providers.py:1766`)。→ 排名必须按 (provider 名, subscription_only) 分层,不能混。

4. **成本数字不可跨 harness 比较,甚至在 Collie 内部就是估算。** `costs.py:36` 用最长子串匹配:`claude-opus-5` 会命中通用 `"opus"` 条目 `costs.py:13` 的 `(15.0, 1.50, 75.00)` —— 一个**旧代次价格**。goose 用 `session_manager.get_session_usage_totals(...).accumulated_cost`(`session/mod.rs:1789`),定价源未验证。→ **只比 token,不比 $**;`MAX_COST` 在两边都不可作为等价预算闸。

5. **"turn" 不是同一个单位。** 见 1.1。→ 用 **provider 请求数**(Collie `res.model_calls`,`loop.py:3038`;goose 从 usage 事件计数)作为对齐量,`--max-turns` 只作为安全网且两边都设得足够宽。

6. **工具集不同宽。** Collie 内建 `bash / read_file / edit_file / write_file / glob / grep / code_search / memory_search / remember / execute_code / delegate / web_search`(`tools.py`, `default_registry`),其中 `memory_search`/`remember` 接的是持久 SQLite 语义记忆 —— **跨 run 会污染**,基准必须给每个 run 独立 `mem_db`。goose 默认只装 `developer` 扩展(`config/extensions.rs:9`),`--with-builtin` 才加更多。→ 必须显式对齐工具面,并在 Collie 侧关闭 memory 与 `code_search` 的向量索引(否则是"harness 带了额外检索器"而非模型差异)。

7. **headless 权限语义相反**(见 S9)。Collie benchmark 路径若 `gate=None`(`loop.py:723`)则完全无门;`collie run` 走的是有门 + 无 approver = 拒绝。goose headless = 自动放行或直接报错。→ 必须两边都跑在"全放行"配置(Collie:`--mode auto` / 构造 `gate=None` 的入口;goose:`GooseMode::Auto`),否则测的是策略不是能力。

8. **Collie 的 harness 自注入消息进入对话历史,会被计入 token 且影响模型行为。** `EDIT_FORCE_NUDGE` / `COVERAGE_NUDGE` / `ROLLBACK_NUDGE` / `REPAIR_NUDGE` / `VERIFY_NUDGE`(`loop.py:37-419`),以及 `loop.py:1858-1862` 在 `turn >= hard_at` 时**把工具集裁剪到只剩 `read_file/edit_file/write_file`**。goose 的对应物是 `goal`/`grind` nudge(`agent.rs:3385-3420`),默认关闭。→ 这些是 harness 的产品选择,应当**单列一个维度报告**,而不是混进"模型表现"。

9. **goose 的 `maybe_summarize_tool_pairs` 会把已完成的 tool 请求/响应对标记为 agent-invisible 并替换为摘要**(`agent.rs:3493-3521`, `update_message_metadata(... with_agent_invisible)`)。这改变了后续轮次看到的历史,与 Collie 的 240 字符 stub 不等价,但同属"harness 隐藏了上下文"。→ 两边都要记录"被隐藏的历史体量"作为伴随指标。

---

## 四、Collie 的源码级弱点(带精确引用与复现标签)

### W1 · 工具批次严格串行,没有任何并发路径
`harness/loop.py:2570-2588`
```python
# ── pass 2: execute what cleared ──
for tool_idx, (tc, tool, repairs, _denied) in enumerate(_prepared):
    ...
    _execute_prepared_tool(tc, tool, repairs, _denied)
```
`_execute_prepared_tool` 内 `loop.py:2344/2362` `out = tool.run(run_args, ctx)` 是同步阻塞调用;`ToolRegistry` 无异步接口。对照 `agent.rs:2899` `stream::select_all`。
**标签:代码确定(结构性,无需运行即可断定)。** 墙钟量级差异未复现。
**影响:** Opus 5 高度倾向批量工具调用,这是 Collie 在任何墙钟维度上的系统性劣势;同时也是**唯一能被单点修复**的最大项。

### W2 · 验证门把"多行 shell 命令"一律判为非证据,可把成功的 run 标成失败
`harness/loop.py:189-246`,关键行 `loop.py:228`:
```python
if ch in ("\n", "\r", ";", "|"):
    return True          # -> _has_unsafe_test_shell_control == True
```
被 `_is_test_runner_cmd`(`loop.py:252`)、`_is_asserting_cmd`(`loop.py:283`)、`_is_repro_cmd`(`loop.py:372`)三处共同依赖。终点在 `loop.py:2976-2983`:
```python
if (self.verify_gate and did_edit and not res.verified and not canceled and not res.error):
    res.error = "verification required but no %s passed" % evidence
```
`--verification required` 由 `cli.py:391-397` 设置 `verify_gate=True, require_assert=True`。heredoc 体虽被 `_shell_control_surface`(`loop.py:171-187`)剥离,但普通多行脚本不会。
**标签:代码确定该谓词对多行命令返回 True 并因此不计证据;"导致真实通过的 run 被标 FAILED"这一端到端链路未复现。**
**影响:** 直接制造假阴性。模型写多行 shell 是常态。

### W3 · `_repro_failed` 用输出首字符判断成败,退出码 0 的命令也可能被判失败
`harness/loop.py:380-389`
```python
def _repro_failed(output) -> bool:
    o = output if isinstance(output, str) else str(output)
    return o.startswith("ERROR") or o.startswith("[exit")
```
`tools.py:533` 中 `head = "" if r.returncode == 0 else "[exit %d]\n"`,因此**退出码 0 时工具结果以 stdout 首字符开头**;stdout 首行若以 `ERROR` 起始,`_repro_failed` 返回 True。该值经 `loop.py:2255` 写入 `last_repro_failed`,进入 `_repro_verified`(`loop.py:1458-1473`)与最终裁决。文档字符串本身声明"ground truth 是退出码",但实现是字符串前缀。
**标签:代码确定该谓词会对首行以 "ERROR" 开头且退出码 0 的输出误判;实际发生频率未复现。**
**影响:** 与 W2 叠加,`required` 模式的假阴性面被显著放大。

### W4 · 会话 id 只有 16 bit 熵,冲突时两段无关对话会被拼接
`harness/sessions.py:131-132`
```python
def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
```
`cli.py:1843-1844` 每次 fresh `collie run` 都调用它;`collie run` **没有** `--session-id`。冲突后的合并语义在 `sessions.py:241-255`:
```python
merged = old + new[common:]     # common == 0 时 = 两段完全无关的对话首尾相接
```
且 `sessions.py:400-401` 的 `checkpoint` 用 `old.get("project") or project` / `old.get("cwd") or cwd`,即**保留先写者的 cwd**。并发同 id 的另一分支是 `session_owner.py:205-213` 的非阻塞锁 → `OwnershipRefused`(`loop.py:1511`)。
**标签:熵值与合并逻辑代码确定;实际冲突概率(同秒 N 并发下 ≈ 1−exp(−N²/131072))未复现。**
**影响:** 直接命中"高并发基准"这一使用场景。缓解手段(per-run `COLLIE_SESSIONS_DIR`,`sessions.py:40`)存在但未被 `collie run` 强制。

### W5 · `collie acp` 不支持取消、不持久化会话、不支持 steering
`harness/acp_agent.py:114-117`
```python
async def new_session(self, cwd, ...):
    sid = "collie-" + uuid.uuid4().hex[:12]
    self.sessions[sid] = {"cwd": cwd or os.getcwd()}
```
整个文件中**没有 `cancel` 方法**,没有 `h.cancelled = ...`,没有 `h.durable_session_id = ...`,没有 `h.steering`(全仓 grep `\.cancelled = |durable_session_id = |\.steering = ` 只命中 `automations.py:1259`、`pack.py:440`、`primitives.py:1792/1867`、`webapp.py:6148/6164/6254`、`delegate.py:89`)。`acp_agent.py:108` 还声明 `load_session=False`。`h.run("acp", ...)` 在 executor 线程中跑(`acp_agent.py:167`),编辑器发出的 ACP 取消无处生效。
**标签:代码确定(方法缺失、字段未赋值)。** 编辑器端实际表现未复现。
**影响:** ACP 是最自然的跨 harness 公平传输(goose 有 `goose acp`,`cli.rs:828`),但 Collie 侧不可用于中断/转向场景 —— 这直接约束了第五节的方案设计。

### W6 · `provider.max_tokens` 在截断恢复中被永久提升,泄漏到后续 run
`harness/loop.py:2124-2129`
```python
cur = int(getattr(self.provider, "max_tokens", 0) or 0)
if cur:
    self.provider.max_tokens = min(32768, cur * 2)
```
无对应复位路径(全文件无 `max_tokens =` 的还原点;`providers.py:420-442` 的 `_probe` 会保存/恢复,但那是另一条路径)。`Harness` 与 `ModelProvider` 在 ACP(`acp_agent.py`)、TUI、`primitives.py` 中是跨 turn 复用的。
**标签:代码确定该赋值无还原;跨 run 影响的实测未复现。**
**影响:** 破坏基准的可重复性 —— 同一进程内第 N 次 run 的生成配置取决于第 1..N−1 次是否发生过截断。

### W7 · 默认无任何硬预算
`settings.py:294-299`:`MAX_TURNS` / `MAX_COST` / `MAX_TOTAL_TOKENS` 默认全为 `"0"`;`loop.py:316-345` `_budget_exceeded` 在两者皆 0 时直接 `return False`;`loop.py:1780` 在 `turn_cap == 0` 时用 `itertools.count()`。同时 `cli.py:332` 把可配置上限**硬夹到 120**,而 goose 默认 1000(`agent.rs:90`)。
**标签:代码确定。**
**影响:** 双向问题 —— 默认无界(基准会烧钱),而可配置上限又低于 goose 默认(长时程任务被结构性截断)。

### W8 · 无条件的 240 字符历史裁剪与模型窗口无关
`harness/context.py:364-366`
```python
shrink = bool(session.get("_overflow_shrink"))
window = 4 if shrink else 14
stub   = 120 if shrink else 240
```
`compaction.py:165-175` 的注释坦承没有 per-model 窗口元数据,阈值 `48_000` 是策略默认。goose 侧 `context_mgmt/mod.rs:367-373` 与 `mod.rs:237-272` 都以 `context_limit` 为基准。
**标签:代码确定阈值为模型无关的绝对/固定值;对 Opus 5 任务成功率的影响未复现(S4 就是为量化它设计的)。**

---

## 五、公平比较传输方案(基于 goose 源码,未杜撰参数)

我逐一核对了 `crates/goose-cli/src/cli.rs` 中真实存在的参数。

### 5.1 主传输(S1-S5、S10-S12:无注入场景)

**goose**(参数出处标注在后):
```
goose run \
  -t "<PROMPT>"                     # cli.rs:228-230  InputOptions.text
  --output-format stream-json       # cli.rs:306-314  ("text"|"json"|"stream-json")
  --session-id <RUN_ID>             # cli.rs:89-91    Cli.session_id
  --max-turns <N>                   # cli.rs:126-127  SessionBehaviorOptions
  --provider anthropic --model <M>  # cli.rs:329-341  ModelOptions
  --quiet                           # cli.rs:300-301
```
事件流:`StreamEvent::Message{message}` 逐条到 **stdout**,结束时 `StreamEvent::Complete{total_tokens, input_tokens, output_tokens, cache_read_input_tokens, cache_write_input_tokens, cost_usd}`(`session/mod.rs:1712`、`1833-1840`)。用量取自 `session_manager.get_session_usage_totals`(`session/mod.rs:1808-1814`)。
`--no-session`(`cli.rs:359-361`)仅用于 S1-S3/S5 这类不需要恢复的场景;S7/S8 必须**不加**它。
goose 模式必须显式置为 `GooseMode::Auto`,否则 `session/mod.rs:1600-1607` 会在 headless 下直接失败。

**Collie**:
```
collie run "<PROMPT>" \
  --stream-json --json \            # cli.py:4643-4645
  --provider anthropic --model <M> \# cli.py:4618-4621
  --cwd <REPO> --project bench \    # cli.py:4641
  --mode auto \                     # cli.py:4647 (显式关掉审批语义)
  --quality thorough                # cli.py:4624 -> configure_run_options
```
**关键差异,必须在采集器里处理:** Collie 的 NDJSON 事件走 **stderr**(`cli.py:2084-2085`),最终 JSON 结果走 **stdout**(`cli.py:2280-2296`)。goose 两者都在 stdout。

**必须的环境隔离(否则 W4 会污染整批):**
```
COLLIE_STATE_DIR=<run>/home/.collie
COLLIE_SESSIONS_DIR=<run>/home/.collie/data/sessions
COLLIE_MAX_TOKENS=<pin>            # providers.py:597 / 818
COLLIE_THINKING=<pin 或不设>        # providers.py:868-897
MAX_TURNS / MAX_COST / MAX_TOTAL_TOKENS 通过 settings 显式设定(settings.py:294-299)
```

### 5.2 Steering 传输(S6)—— 两侧不对称,必须分别声明

**goose**:唯一入口是 ACP。
```
goose acp --with-builtin developer          # cli.rs:828-843
# 或 goose acp-http --port <P> --dangerously-unauthenticated   # cli.rs:853-901
```
注入用 `_goose/unstable/session/steer`(`acp/server.rs:2353` `on_steer_session`,方法名见 `acp/server.rs:1916` 的错误文案)。**前置条件:必须有活动 run**(`acp/server.rs:1982`)。

**Collie**:ACP 不可用(W5)。两个可选项:

- **推荐(无网络,最简单)**:`collie run "<PROMPT>" --continue --stream-json --json`,注入器直接调用文件 API:
  ```python
  from harness import task_inbox
  task_inbox.enqueue(SID, "steer-1", TEXT, mode="steer")   # task_inbox.py:614
  ```
  `collie run` 在 `cli.py:2071` 设 `h.checkpoint_scope = "session:" + sid`,`loop.py:681-688` 由此解析出 durable session id,`loop.py:1816` 的 `_consume_durable_steering` 因此生效。`SID` 从第一条 stdout JSON 的 `"session"` 字段(`cli.py:2282`)或 `sessions.latest()` 取。
- **备选**:`collie web`(`webapp.py:6164/6254`),这是唯一同时接了 `cancelled` + `steering` + `steering_after_seq` 的 Collie 表面,但引入 localhost HTTP。

**报告时必须写明**:goose 走 ACP 进程内队列(易失,`agent.rs:307`),Collie 走文件+租约队列(持久,`task_inbox.py` + `session_owner.py`)。这不是同一机制,S6 的结果要按"指令是否被采纳 + 何时被采纳"两个量分别报告,不合成单一分数。

### 5.3 中断传输(S7/S8)

两侧均用信号,不用 ACP:
- POSIX:`SIGINT`(S8)/ `SIGKILL` 到进程组(S7)。
- Windows:必须 `CREATE_NEW_PROCESS_GROUP` 启动,S8 用 `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pgid)`,S7 用 `taskkill /PID <pid> /T /F`。
- goose 侧 SIGINT 由 `session/mod.rs:1557-1561` 的 `ctrl_c()` 任务转成 `cancel_token.cancel()`;Collie 侧由 `loop.py:3015` 的 `except KeyboardInterrupt` 承接。

### 5.4 对齐的度量口径

| 指标 | Collie 取值 | goose 取值 |
|---|---|---|
| provider 请求数 | stdout JSON 无该键;从 `res.model_calls`(`loop.py:3038`)—— 需在事件流上补,或用 `receipt` 事件 | 统计 `AgentEvent::Usage` 次数 |
| token | stdout JSON `input_tokens/output_tokens/cache_read/cache_creation/total_tokens`(`cli.py:2289-2292`) | `StreamEvent::Complete` 各字段 |
| 墙钟 | `wall_ms`(`cli.py:2296`) | 外部计时(goose 未在 stream-json 里给) |
| 成本 | **不使用**(W-3 定价错配) | **不使用** |
| 完成语义 | `stop_reason` / `turns_exhausted`(`loop.py:2965/3074`) | `MAX_TURNS_MESSAGE` / `EMPTY_TURN_MESSAGE` 文本匹配 |

---

## 六、按产品影响排序的修复建议(Collie 侧)

**P0 —— 直接决定基准结论,且是真实用户体感**

1. **工具批次并发化(W1)。** 在 `loop.py:2571` 的 pass 2 中,对 `_prepared` 里 `denied is None` 且工具被标注为可并行的项用线程池并发执行,串行保留给有副作用互依赖的调用。难点是 `journal_state`/`journal_detail` 是循环内共享的单变量(`loop.py:2274`)以及 `session["messages"].append` 的顺序性 —— 需要把 per-call 的 journal detail 改成按 `tc.id` 索引的映射,并在全部返回后按 `_prepared` 原序 append 结果。收益:墙钟量级改善,且 Opus 5 的批量调用模式会被真正利用。

2. **修 `_repro_failed`(W3)。** 让 `BashTool` 在结果里携带结构化退出码(而非只在 `returncode != 0` 时前缀 `[exit N]`),`loop.py:380` 改为读该字段;字符串前缀仅作兼容回退。这是三行级别的改动,消除一整类假阴性。

3. **放宽 `_has_unsafe_test_shell_control` 对换行的处理(W2)。** 当前把 `\n` 与 `;`/`|` 等同看待过于粗暴:`cmd1 && \n cmd2` 与真正会吞掉退出码的 `cmd1 ; cmd2` 语义不同。最小修法:把命令按未加引号的换行切分,若**最后一个**非空片段本身是被识别的 runner 且其前面的片段不含 `||`/`&`/后台符,则接受。若不改逻辑,至少要在 `REPAIR_NUDGE`/`VERIFY_NUDGE` 里显式告知模型"必须用单行命令",否则门是不可满足的。

**P1 —— 基准可重复性与并发安全**

4. **`new_id()` 提熵 + 给 `collie run` 加 `--session-id`(W4)。** `os.urandom(2)` → `os.urandom(8)`,并在 `cli.py` 的 `run` 解析器加显式 id 参数。同时 `sessions.checkpoint` 在检测到 `common == 0` 且双方均非空的合并时应当**拒绝并报错**,而不是静默拼接(`sessions.py:251`)。

5. **`max_tokens` 提升改为 run 局部(W6)。** 在 `_run` 里保存 `provider.max_tokens` 原值,`finally` 中恢复;或把提升后的值放在一个 per-run 的 override 上传给 `complete()`,不写回 provider 对象。

6. **默认预算(W7)。** 给 `collie run` 一个非零的默认 `max_turns`(或至少默认 `MAX_TOTAL_TOKENS`),并把 `cli.py:332` 的 120 上限提到与 goose 可比的量级(现在 120 会在长时程任务上结构性落后)。

**P2 —— 上下文策略的模型感知化**

7. **让裁剪与压缩阈值随模型窗口伸缩(W8)。** `context.py:365-366` 的 `window=14 / stub=240` 与 `compaction.py:175` 的 `48_000` 都应从 provider 报告的窗口推导(哪怕只是一张 model→window 的静态表,像 `costs.py` 那样)。当前实现在 Opus 5 上会把大量本可保留的证据丢掉,而这恰恰是 SWE 类任务的失分点。顺带修 `costs.py` 对 `claude-opus-5` 的定价缺失(`costs.py:13/36`)。

**P3 —— 表面完整性**

8. **`collie acp` 补 `cancel` + durable session + steering(W5)。** 具体是:`new_session` 时创建真实 session id 并设 `h.durable_session_id`,`prompt` 前设 `h.cancelled`(由一个 per-session 的 `threading.Event` 驱动),实现 ACP 的 `cancel` 方法置位该 Event。这会让 ACP 成为 Collie 与 goose 之间唯一真正对称的传输,同时对 IDE 用户是实打实的功能补全。

---

**关于本报告的一处自我限制**:第 1.5 节中"goose 取消时可能留下无 `ToolResponse` 的空 user 消息"、以及 S8 对 goose 的预期,都依赖 `goose-providers::conversation::Conversation` 的校验/修复行为,该 crate 不在 pinned 检出内。这两点在报告与基准结果中都应标为**未验证**,不作为 goose 的缺陷结论。