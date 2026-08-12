# Collie Agent Runner 集成决策

日期：2026-08-12
状态：Collie 原生 overnight control plane 已实现，但 direct Claude-plan 数据面被真实预检阻断；Codex `exec` runner POC 已验证；其他 adapter 仍是集成计划

## 结论

Collie 应继续是长任务的控制面，Prime Agent、Codex、Pi、Hermes 只能作为可替换的执行 worker。不要把 Mission、预算、调度、重试、审批和完成判断下放给外部 harness；否则会出现两个控制面同时续跑、压缩、重试或宣告完成，故障恢复也无法判定哪一方是事实来源。

当前原生 control plane 已具备 12 小时 active wall budget、7 天 elapsed window、持久会话、进程树取消、workspace baseline 和 fresh host verifier。实验性数据面用 `--provider anthropic-oauth --model claude-opus-4-8` 冻结路由，并由 Collie 自有 harness/loop 直接向 Anthropic Messages endpoint 发请求；body 只有 Collie 自己的 system/tool contract，不调用 `claude -p`，也不引入 Claude Code system prompt。但它在测试账号上的真实最小请求返回 HTTP 429，而官方 Claude Code 同时可用；Anthropic 也没有把任意 raw Messages OAuth 调用文档化为 Claude plan 的支持接口。因此当前状态是 **实现但不 admitted**：startup live probe 会 fail closed，不能宣称已经可以跑一夜。

另有一个独立的 12 小时阻断条件：Collie 不实现、也不写入 Claude Code 的私有 refresh-token 流程，因此创建 overnight Mission 时必须证明 login-store access token 本身覆盖完整 12 小时 active window。测试令牌的有效期明显短于该窗口；依赖后台另起 Claude Code 进程刷新共享 credential 不再是 Collie 自己的 direct-call 模式。短有效期或没有明确 expiry 都会在启动时 fail closed。

外部集成建议保持统一 `AgentRunner` contract。已有的 `CodexExecRunner` POC 验证了 JSONL 事件、usage 和同一 thread resume；下一步优先用 Codex SDK 做自动化 worker，需要完整 approval/event/live-control 时再接 App Server。Prime RPC 和 Pi RPC 可共用严格 LF-JSONL transport，但 adapter 必须分开。Hermes 则同时提供 Gateway worker 协议和可借鉴的 durable goal/Kanban claim 语义。ACP 保留为编辑器互操作层，不作为 Collie 的首选 worker 控制协议。

计费口径必须以当前官方政策为准。截至 2026-08-12，Anthropic 已暂停原定 6 月 15 日启用的独立月度 credit 变更；Claude Agent SDK、`claude -p` 和第三方 app 当前仍从 Claude 订阅用量中扣除。因此不应再断言这些路径“现在必须用 monthly credits”或“一定按 token 额外收费”。`claude -p` 在本项目中只用于 benchmark/compatibility comparison，不是原生 overnight runtime。另一方面，一个 adapter 的实际认证方式、环境覆盖和未来政策仍可能改变路由；Collie 仍需在每个可运行边界重做 guard、锁定官方 endpoint、禁止环境代理与 paid/API/provider fallback，无法证明时 fail closed。[Anthropic：use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)

## 架构边界

Prime 的 Verifiers v1 把系统拆成 taskset（做什么）、harness（怎么做）和 runtime（在哪里做）；它还把 local subprocess、Docker 和远端 sandbox 放在同一个 Runtime contract 后面。这是 Collie 最值得借鉴的抽象，但 Collie 的 Mission 比 taskset 多了长期目标、用户审批、预算和恢复语义，因此不能直接把 Mission 等同于一次 harness rollout。[Prime Verifiers v1](https://www.primeintellect.ai/blog/verifiers-v1)

```text
用户 / API / UI
       |
       v
Collie Mission control plane
  目标 · 预算 · 调度 · lease · 审批 · 恢复 · host verifier
       |
       +---- Collie native CodeSliceProcessRunner (已实现)
       |
       +---- AgentRunner contract (外部 worker)
              +---- Prime RPC adapter
              +---- Pi RPC adapter
              +---- Codex SDK / App Server adapter
              +---- Hermes Gateway adapter
       |
       v
Runtime
  本地进程 · 容器 · 远端 sandbox · process-tree cancellation
       |
       v
受限 workspace / worktree
```

职责分配如下。

| 责任 | Collie 控制面 | 外部 harness worker | Runtime |
| --- | --- | --- | --- |
| durable goal、任务树、优先级 | 唯一所有者 | 只接收本 slice 指令 | 无 |
| 预算、计费策略、截止时间 | 唯一所有者 | 报告 native usage | 强制 wall/process 限制 |
| 会话上下文、工具循环、compaction | 保存引用和摘要 | 唯一所有者 | 提供进程和存储 |
| retry/backoff | 决定何时重跑 | 报告 rate/auth/transient error；可做 turn 内短重试 | 负责基础设施重试 |
| 审批 | 制定 policy、持久化决定 | 发出请求，执行批准结果 | 落实 sandbox 权限 |
| 完成判断 | 运行 fresh host verifier，唯一可写 `VERIFIED` | `final`/`turn.completed` 只表示本轮停止 | 返回命令与文件证据 |
| restart/resume | 保存 native session ref 和 cursor，恢复 lease | 恢复自己的 thread/session | 重建或重新连接资源 |
| cancel | 记录意图和结果 | 协议级 interrupt/abort | 最终 kill 整个进程树 |

明确不做：

- 不同时启用 Collie Mission continuation 和 Prime goal/heartbeat/autonomous continuation。
- 不让 runner 的自然语言“done”直接完成 Mission。
- 不把 OAuth token、API key 或可刷新的 credential 写入 Mission 数据库。
- 不静默从订阅 allowance 或本地模型切到 paid overage、API 或其他计费路由。
- 不假设 Prime RPC 与 Pi RPC 永远 wire-compatible；只共享 transport 和公共映射，各自维护 dialect/version probe。
- 不把本地进程隔离宣传成安全沙箱。Prime 自己也说明 daemon worker 的进程隔离用于生命周期和故障收敛，并不改变操作系统权限。[Prime daemon architecture](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/daemon.md)

## 候选协议比较

| 候选 | 官方控制面 | 主要能力 | Collie 适配判断 |
| --- | --- | --- | --- |
| Prime Agent RPC | LF-JSONL over stdio | `prompt`、`steer`、`follow_up`、`abort`、session state/messages、compaction、retry、usage；另有 daemon session、消息、heartbeat、goal | 最接近完整 runner；P0。集成时禁用其长期控制功能，只保留 session/agent loop |
| Codex SDK | TypeScript/Python library；Python SDK 控制本地 app-server | start/continue/resume thread、sandbox presets；发布版 Python SDK 携带 pinned CLI runtime | 官方推荐的自动化/job 接入点；作为下一个 production adapter |
| Codex `exec` | 一次性 CLI，`--json` 输出 JSONL | structured events、usage、output schema、session resume、显式 sandbox | POC 已验证 start→resume 与事件持久化；保留为 smoke/fallback，不承担完整 live control |
| Codex App Server | JSON-RPC over stdio、WebSocket 或 Unix socket | thread start/resume/fork、turn start/steer/interrupt、完整 item events、approval、sandbox、goal、review | 需要深度产品集成时使用；优先 stdio，WebSocket 当前为 experimental/unsupported |
| Pi RPC | strict LF-JSONL over stdio | prompt/steer/follow-up/abort、state/messages、session switch/fork、compaction/retry、usage | 最干净的通用 RPC 基线；P0，与 Prime 共用 transport、不共用未验证语义 |
| Hermes Gateway + durable controls | JSON-RPC over stdio/WebSocket；SQLite Kanban/SessionDB | session resume/branch、steer/interrupt、approval；persistent goal、atomic claim/TTL、heartbeat、stale reclaim、retry/circuit breaker、explicit complete/block | Gateway 作 worker adapter；goal/Kanban 语义用于对照 Collie 持久性，不与 Mission 双重续跑 |
| Hermes ACP | ACP JSON-RPC over stdio | session、prompt、stream、tool、permission、fork、cancel、auth | 适合 IDE/通用客户端兼容；编排和统计能力取决于 capability，P2 |

## 当前工程排名（不是模型质量榜）

这里排的是“作为 Collie 外部 worker 的集成优先级”，不是回答质量或 SWE-bench
名次；后者需要同模型、同 runtime、外部隐藏 grader 的受控实验，现有两类合成任务
不足以给五个产品排总榜。

| 集成顺序 | Worker surface | 为什么 |
| ---: | --- | --- |
| 1 | Codex Python SDK | stable SDK、pinned runtime、thread resume 和 sandbox preset；最少自建 transport，适合先做 production adapter |
| 2 | Prime RPC | runner 操作最完整，已有 steer/follow-up/abort、usage、compaction；但必须关闭 goal/heartbeat/schedule，避免和 Mission 双控制面 |
| 3 | Hermes TUI Gateway | approval、interrupt、branch、usage、session history 和 multi-agent 都很全；surface 较大，先做 capability negotiation |
| 4 | Pi RPC | LF-JSONL contract 最小、适合 conformance 基线；缺少 Prime/Hermes 那层常驻 supervisor，且当前 Claude Pro/Max route 不满足无额外计费要求 |

把 Collie 放进同一张“产品成熟度”表时，结论要分层：Mission 的 lease、ancestor
aggregate budget、显式 uncertain recovery、fresh host verifier 已经接近一个真正的
overnight control plane；但这个 branch 仍是实验实现，而且原生 Opus 数据面未获准。
Codex、Prime、Hermes 已有可用的长期/常驻产品 surface，工程成熟度领先；Pi 更像一个
优秀的 agent loop/RPC worker。Collie 当前的优势是控制面边界和 fail-closed 预算语义，
短板是 adapter 覆盖、跨平台 process ownership 的实战时间，以及最关键的 direct Opus
subscription 可用性。

在用户要求的最窄赛道——“Collie 自己发请求、只带 Collie system prompt、Opus
subscription、不能额外计费”——当前没有一个已证明可用的冠军：Collie raw direct
probe 返回 429 且 token 不覆盖 12 小时；`claude -p`/Agent SDK 虽有文档化订阅路径，
却会引入 Claude Code harness；Pi 当前明确走 extra usage；Prime/Hermes 也没有一条已
由我们证明同时满足这四个条件的官方 route。因此本分支正确的产品状态是
`blocked/fail-closed`，不是把第二名路径偷偷当第一名。

### Prime Agent

Prime RPC 已有对 Collie 很合适的双向操作：请求有 correlation id，事件异步流出；可以 steer、follow-up、abort、读取 state/messages、触发 compaction、控制 transient retry 并读取 session usage。其 RPC 文档还特别要求只按 LF 分割 JSONL，不能把 Unicode `U+2028/U+2029` 当记录边界。[Prime RPC](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/rpc.md)

Prime 的 daemon/session worker、lease、reconnect snapshot、crash journal、detached process cleanup，以及 goal/heartbeat/schedule 的持久化说明了一个成熟 overnight harness 需要哪些恢复语义。[Prime daemon architecture](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/daemon.md)、[Prime long-running agents](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/long-running-agents.md)

集成时只借用 native session 和 agent loop。Collie 不调用 Prime 的 goal、heartbeat 或 schedule API；否则 Prime 会在 Collie 暂停后继续推进，或两边重复投递 continuation。第一版使用 Collie-owned child process；后续若要复用 Prime resident daemon，只把它当 Runtime/session host，并要求稳定 cursor、lease ownership 和明确的 uncertain-side-effect 恢复结果。

### Codex

三种入口应分层使用：

- SDK：OpenAI 对自动化 job/CI 推荐 SDK。Python SDK 控制本地 App Server，发布版携带 pinned Codex CLI runtime；TypeScript SDK 支持 thread start、continue 和 resume。因此 Collie 的 production `CodexRunner` 应优先用 SDK 实现常规 slice/session 生命周期。[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- `codex exec --json`：每行一个事件，包含 thread/turn/item/error 与 turn usage；支持 `codex exec resume <SESSION_ID>`，也能明确设置 sandbox。当前 POC 已用真实 ChatGPT login 验证 start→resume 使用同一 thread，并持久化 normalized JSONL snapshot/cursor/usage。它仍是 smoke/fallback adapter，不用来假装实时 steer 或 approval round-trip。[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- App Server：需要深度客户端集成时，接入完整 thread/turn/item 生命周期、`thread/resume`、`turn/steer`、`turn/interrupt`、server-initiated approval、workspace sandbox 和 schema generation。优先用 stdio JSONL；官方将 WebSocket transport 标为 experimental 且不支持 production workload。不要解析 TUI 文本。[Codex App Server](https://learn.chatgpt.com/docs/app-server)

Codex 不提供 Opus，因此它解决的是“让 Collie 可调度另一个成熟 coding harness”，不是“复用 Opus 订阅”。ChatGPT/Codex 的 auth 和 quota 也必须作为独立 billing profile，不能与 Anthropic 订阅合并。

### Pi

Pi 的 RPC 是最适合做 contract conformance 的最小实现：strict LF-JSONL、request/response correlation、异步 events、steer/follow-up/abort、session state/messages、compaction/retry 与完整 session stats 都有公开定义。[Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)

实现上抽出 `LfJsonlProcessTransport`，Prime 和 Pi adapter 各自提供命令映射、事件映射、版本探测与 capability snapshot。即使两者当前相似，也不以类名或字段巧合推断长期兼容。

Anthropic 的通用政策页目前说 Agent SDK、`claude -p` 和第三方 app 仍可从 plan limits 扣除；但 Pi 自己的当前 provider 文档对其具体 Claude Pro/Max 路由写得更窄：该第三方 harness 用量走 extra usage、按 token 计费，而非 Claude plan limits。因此 Pi 可以进入通用 worker 集成和有预算的 paid track，但当前不能进入 Collie 的“Opus subscription、无额外计费”overnight track。以后只有 Pi 官方文档和运行时 billing evidence 都改变后才重新评估。[Pi providers](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)

### Hermes

Hermes 官方给出三种程序化入口：ACP、TUI Gateway JSON-RPC、OpenAI-compatible HTTP API。TUI Gateway 能映射 prompt、steer、interrupt、compress、status、history、resume、branch，以及 approval/clarify/secret 等交互，最符合 Collie worker 的完整需求；HTTP API 适合已有 OpenAI client 的轻量接入，但会丢失一部分 native 控制语义。[Hermes programmatic integration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md)

Hermes 的另一个价值是它已经把长跑状态做成了显式协议。Persistent Goals 把 goal 存入 `SessionDB.state_meta`，每轮由 judge 判定是否续跑，用户输入可以抢占 continuation，并有 turn budget、pause/clear/resume。Kanban 则用 SQLite 保存 task/event/run，dispatcher 做 atomic claim/TTL、heartbeat、stale reclaim、runtime timeout、retry/circuit breaker，worker 必须显式 `complete` 或 `block`；正常退出却没有 terminal board call 被视为协议违规。这些是 Collie Mission/TaskTree 应继续对照的恢复语义。[Hermes Persistent Goals](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/goals.md)、[Hermes Kanban](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md)

集成时必须只选一个 durable control plane：默认用 Gateway 做受 Collie Mission 调度的 worker，禁用 Hermes goal/Kanban 自动 continuation。若未来要导入 Kanban board，则需一个显式 bridge 映射 claim generation、heartbeat、terminal outcome 和 uncertain side effects，不得让两个 dispatcher 同时拥有同一任务。

ACP 的强项是标准 IDE 互操作：协议定义了 initialize、session new/load/prompt/cancel、streaming update、permission 和客户端文件/terminal 能力。它不是专门为 durable job orchestration 设计的，usage、cursor、backoff、checkpoint 等必须依赖扩展 capability；因此 Collie 可以同时做 ACP agent/server surface，但作为 Hermes worker client 应优先 Gateway。[ACP overview](https://agentclientprotocol.com/protocol/v1/overview)

Hermes 的计费路由同样要在 adapter probe 时核实。不从旧 provider 文案推导“必然 extra usage”，也不因为有 OAuth 就自动标为 no-paid-overage；没有符合当前 Anthropic 政策的运行时证据时继续 fail closed。

## 统一 `AgentRunner` contract

建议 contract 保持小而严格，所有方法都带 `mission_id`、`runner_id` 和 idempotency key：

```python
class AgentRunner(Protocol):
    async def probe(self) -> RunnerCapabilities: ...
    async def create(self, spec: RunnerSpec) -> NativeSessionRef: ...
    async def resume(self, ref: NativeSessionRef) -> RunnerSnapshot: ...
    async def run_slice(self, ref: NativeSessionRef, request: SliceRequest) -> AsyncIterator[RunEvent]: ...
    async def steer(self, ref: NativeSessionRef, message: str, *, expected_turn_id: str | None) -> Ack: ...
    async def resolve_approval(self, ref: NativeSessionRef, approval_id: str, decision: Decision) -> Ack: ...
    async def cancel(self, ref: NativeSessionRef, reason: str) -> CancelResult: ...
    async def inspect(self, ref: NativeSessionRef) -> RunnerSnapshot: ...
    async def close(self, ref: NativeSessionRef, disposition: str) -> None: ...
```

Contract 语义：

- `create` 只创建 native session，不代表开始一个不可控的无限任务。
- `run_slice` 有 wall、turn、token 和 output-byte 上限；正常 turn 结束返回 `YIELDED`，不是 `VERIFIED`。
- `resume` 必须检查 workspace identity、runner/version、native session 与 lease；不匹配时进入 `RECOVERY_REQUIRED`，禁止盲重放。
- `steer` 尽量携带 expected turn id，避免消息送入下一轮。
- `cancel` 先走 native abort/interrupt，再由 Runtime 在 grace period 后 kill process tree。
- `close` 可重入；不得删除 transcript、workspace 或验证证据，除非有单独、明确的清理授权。

`RunnerCapabilities` 至少包含：

```text
protocol_name/version
session_create/resume/fork
streaming/cursor_replay
steer/follow_up/cancel
approval_round_trip
usage_tokens/usage_cost
compaction/retry_state
sandbox_controls
native_goal/native_scheduler
```

能力缺失必须显式降级。例如无 `steer` 时把消息排到下一个 slice；无 replay cursor 时重连后先 `inspect` 并标记事件 gap；无 usage 时不填零，而是写 `unknown` 并使用更保守的 wall/turn budget。

## Durable fields

Mission/runner checkpoint 至少持久化以下数据；credentials 永远不在其中：

| 分类 | 字段 |
| --- | --- |
| 身份 | `runner_kind`、`runner_protocol_version`、`runner_binary_version`、`runner_id` |
| native session | `native_session_id`、`native_thread_id`、`native_session_locator`、`native_turn_id` |
| 恢复 | `last_event_cursor`、`last_event_seq`、`last_acked_request_id`、`recovery_state`、`uncertain_operation` |
| workspace | canonical root、worktree/branch、baseline digest、current digest、repository identity |
| 执行配置 | provider、model、reasoning/effort、sandbox、approval policy、capability snapshot |
| 计费 | `auth_mode`、`billing_mode`、`paid_overage_allowed`、marginal/equivalent cost、token/call deltas |
| slice | attempt、slice index、deadlines、turn/token/output limits、started/last-event/completed timestamps |
| 交互 | pending approval/clarification id、request digest、expiry、decision provenance |
| 验证 | verify command、exit code、evidence digest、verified workspace digest、verified timestamp |
| lease | owner、generation、acquired/renewed/expires timestamps |

`native_session_locator` 可以是 owner-private 文件路径或 opaque id，但不能内嵌 token。版本和 capabilities 必须在每次 create/resume 时重新探测；若发生不兼容升级，暂停并要求迁移，不把解析错误当 transient model failure。

## Normalized events

统一 envelope：

```json
{
  "schema_version": 1,
  "event_id": "runner-local-unique-id",
  "mission_id": "...",
  "runner_id": "...",
  "native_session_id": "...",
  "native_turn_id": "...",
  "type": "turn.completed",
  "at": "2026-08-12T12:00:00Z",
  "cursor": "opaque-native-or-collie-sequence",
  "payload": {},
  "raw_type": "turn/completed",
  "raw_digest": "sha256:..."
}
```

第一版 canonical event type：

- `session.started`、`session.resumed`、`session.state`
- `turn.started`、`turn.yielded`、`turn.failed`、`turn.cancelled`
- `message.delta`、`message.completed`
- `tool.started`、`tool.progress`、`tool.completed`
- `file.changed`
- `approval.requested`、`approval.resolved`
- `compaction.started`、`compaction.completed`
- `retry.scheduled`、`rate_limit.reached`、`auth.required`
- `usage.updated`
- `runner.warning`、`runner.error`、`runner.exited`

Native raw event 可以作为压缩诊断附件保存，但必须做 secret redaction 和大小限制。Canonical event 不承诺还原 harness 的全部 UI，只承诺 Mission 恢复、预算、审计、审批和验证所需的稳定语义。

## Subscription no-paid-overage policy

当前原生 overnight 实现使用 `--no-paid-overage`，不是一个模糊的“Opus subscription”标签：

```text
collie mission start "<goal>" --code --workspace PATH --overnight \
  --provider anthropic-oauth --model claude-opus-4-8 --no-paid-overage \
  --verify-command "python -m pytest -q"
```

1. 用户先在 provider 账户中关闭 paid usage credits/overage 和 auto-reload，再给出显式 attestation。
2. 只允许冻结的 `anthropic-oauth` + 显式 model 路由；当前原生命令使用 `claude-opus-4-8`。Codex OAuth 和 `claude -p` 均不是 overnight route。
3. guard 检查官方 Claude login store 中的 Pro/Max plan、`user:inference` scope，以及 access token 是否足以覆盖完整 12 小时 active window；Collie 不实现 Claude Code 私有 refresh flow，也不持久化 token。实际 request 只发往 `https://api.anthropic.com/v1/messages`，显式禁用 ambient proxy，不得退回 API key、CLI、其他 provider 或 model。
4. 创建时做一次真实 inference preflight；之后每个可运行边界只做本地、零调用的 login/scope/plan/expiry revalidation，旧 receipt 只用于审计；下一次真实 provider request 仍会独立 fail closed。
5. 订阅限额、auth 失效或证据不足只能进入 `WAITING`/`NEEDS_YOU` 或拒绝运行；绝不自动购买、充值、打开 paid overage、注入 API key 或切换 provider。
6. control plane 将已 admitted 的 login-backed request 记为 `marginal_charge_usd=0`，并单独保留 equivalent API list-price 供容量分析；这是为了阻止 metered fallback 的预算分类，不是对 provider 实际账单的观测或保证。

对未来的外部 `AgentRunner`，运行时计费分类至少要区分
`subscription_allowance`、`paid_overage`、`api_metered`、`local` 和 `unknown`。
`--no-paid-overage` 下只放行有当前证据的 `subscription_allowance` 或 `local`；
其他类别均 fail closed。Anthropic 目前说 Agent SDK、`claude -p` 和第三方
app 消耗 plan limits，但这不替代对 Collie direct OAuth route 的认证、endpoint 和账户开关
做实时预检，也不保证未来政策不变。

Collie 可以保证的是“无付费 fallback，证据不足即停”，不是对 provider 最终账单的事后证明。产品文案应同时显示当前 route、preflight 时间、限额耗尽后的行为和这一不确定性。

## 路线图

### 当前已完成

- Collie native overnight control plane：自有 loop + 实验性 direct Anthropic OAuth，冻结官方 endpoint/model，禁用 proxy/CLI/API/provider fallback，每个 runnable boundary重做 guard，并具有可恢复 session、process-tree cancellation、workspace baseline/fresh verifier 和 12-active-hour/7-day-elapsed leash；当前 direct inference probe 为 429，故数据面 fail closed、不可用。
- `AgentRunner`/snapshot/event 原型与 `CodexExecRunner`：使用 stdin prompt、`workspace-write` sandbox、JSONL 事件、usage/cursor 持久化、start→resume、超时/取消和保守 recovery 标记。

### P0：把外部 runner 接入 Mission

目标：让已验证的 runner POC 受现有 Mission 边界统一控制，而不是比较谁回答得更好。

- 在已有 `AgentRunner`/snapshot/event POC 上补齐 `RunnerCapabilities`、`NativeSessionRef` 和 billing policy types。
- 把已有 `CodexExecRunner` 接入 Mission capability，仍只作为 JSONL/resume/usage 的 smoke adapter。
- 实现 Codex Python SDK runner，覆盖 thread start/continue/resume、sandbox 和稳定 runtime pinning，作为首个 production external worker。
- 新增可 kill、限流、严格 LF framing 的 `LfJsonlProcessTransport`。
- 实现 `PiRpcRunner` 与 `PrimeRpcRunner`；共享 transport，不共享未经 conformance test 的命令/字段假设。
- 接入现有 Mission lease、checkpoint、event store、host verifier 和 process-tree cancellation。
- CLI 试验入口（未实现）：`collie mission start --code --runner prime|pi|codex-exec --workspace ... --provider ... --model ... --verify-command ... --no-paid-overage`。
- 对 Prime/Pi 明确关闭 native goal、heartbeat、schedule 与自动 provider fallback。

P0 验收：同一 fixture task 连续运行至少 20 个 slice；在 prompt 接收前后、tool side effect 后和 checkpoint 前后强杀进程，均可恢复到同一 native session，且不盲重放 uncertain operation；最终只有 fresh host verifier 可以写 `VERIFIED`。

### P1：完整双向控制

- 需要富事件、live steer/interrupt 和 approval round-trip 时实现 `CodexAppServerRunner`，覆盖 initialize/schema pin；使用 stdio transport。
- 实现 `HermesGatewayRunner`，覆盖 Gateway ready、session resume/branch、steer/interrupt、approval/clarify/secret。
- 增加 Hermes goal/Kanban conformance fixtures：检查 claim generation、heartbeat、stale reclaim、retry/circuit breaker 和 explicit terminal outcome，同时证明 native continuation 被禁用。
- 抽象 `Runtime`：local subprocess 与 container 两种实现；workspace mount、network policy 和 secret injection 独立于 runner。
- UI/API 加入 attach、steer、cancel、approval、native session/version、billing mode、rate-limit reset 和 verification evidence。
- 做真实 8–12 小时 soak：系统睡眠/唤醒、Collie daemon 重启、runner 崩溃、网络抖动、auth refresh 与限额重置。

P1 验收：一次 overnight mission 经至少两次 supervisor restart 和一次 worker crash 后仍恢复；没有重复 Collie dispatch；任何不确定外部副作用都进入 `RECOVERY_REQUIRED`；成功状态带匹配当前 workspace digest 的验证证据。

### P2：互操作和规模化

- 实现 generic `AcpRunner`，用于只提供 ACP 的 agent；capability 不足时明确降级。
- 把 Collie 现有 ACP server 与新的 ACP client 共用 schema/normalizer，但保持角色和权限隔离。
- 增加 remote sandbox runtime、credential broker 和 per-run network egress policy。
- 采用 append-only message/event graph，原生表示 fork、compaction 和 subagent branch，避免长期 transcript 二次增长；这是 Prime Verifiers v1 message graph 最值得后续吸收的部分。[Prime Verifiers v1](https://www.primeintellect.ai/blog/verifiers-v1)
- 发布 adapter conformance kit，让新增 harness 只需实现 contract 和通过故障矩阵。

## Failure 与 chaos test 矩阵

所有 adapter 必须跑同一 contract suite，并加 native dialect fixtures。

### 协议与流

- JSON 被任意 byte chunk 切开、半个 UTF-8 code point、CRLF 输入、stdout 最后一行无 LF。
- JSON 字符串包含 `U+2028/U+2029`；只能由 LF 分帧。
- malformed/truncated JSON、未知 event type、重复 response id、event 无 request id。
- stdout flood、stderr 噪声、consumer 慢导致 backpressure、单 event 超限。
- 重连后 cursor 重复、gap、倒退或 native protocol 不支持 replay。
- runner 升级后 schema/capability 改变。

### 进程与恢复

- kill：spawn 后、prompt write 前、accept ack 后、tool start 后、tool side effect 后、turn complete 后、checkpoint commit 前后。
- child hang、grandchild/orphan、native cancel 无响应、cancel 与 natural completion 竞态。
- Collie daemon crash、机器 reboot、sleep/wake 导致 monotonic/wall-clock 跳变。
- 两个 Collie worker 同时争同一 Mission/native session，lease 必须阻止双写。
- session file 损坏、磁盘满、workspace 被移动或 repository identity 改变。

### 模型、认证与计费

- 401/expired refresh token、429 带/不带 reset time、5xx、断网、partial response。
- token/cost usage 缺失或累计值回退；必须标 `unknown`，不能当零。
- `ANTHROPIC_API_KEY` 意外出现在父环境；`no-extra` 必须在启动前拒绝或清除并记录。
- harness 尝试从 subscription allowance 切到 paid overage、API 或另一个 provider；必须拒绝。
- plan limit 耗尽；Mission 等待或请求用户，不能自动购买、充值或 fallback。
- 中途 `/model`/`set_model` 改变 billing class；必须由 Collie 再授权。

### 完成与安全

- harness 说“完成”但无 diff、验证失败、验证证据来自旧 workspace digest。
- tests 通过后文件再次变化；旧证据立即失效。
- verify command timeout、输出超限、产生后台进程。
- approval 请求在无人在线时到达、过期后收到迟到答复、同一 approval 重复提交。
- symlink/junction 逃逸 workspace、命令访问未授权 root、网络请求超出 policy。
- runner 自带 goal/scheduler 被误开启；contract test 必须发现双重 continuation。

## 下一轮工程试验

建议用一个小型、可重复、带失败测试的临时仓库跑同一任务，不以单次质量排名为目标：

1. Codex SDK：复用已有 `exec` trace 作对照，验证 thread resume、sandbox、取消与 Mission checkpoint 映射。
2. Prime/Pi RPC：运行同一套 strict LF-JSONL transport/conformance tests，分别记录 dialect 和 capability 差异。
3. Hermes Gateway：验证 ready、session/status、steer/interrupt、approval round-trip，再用 goal/Kanban fixture 验证 claim、heartbeat、reclaim 和 explicit completion。
4. Codex App Server：只覆盖 SDK 未提供的富事件、live steer/interrupt 和 approval 缺口，用 stdio，不依赖 experimental WebSocket。

每个试验产出：capability snapshot、原始 event fixture、normalized trace、crash timeline、billing classification、最终 host-verification receipt。只有这六项齐全，adapter 才能从“可启动”进入“可用于 overnight”。

最终推荐顺序是：**先保持 Collie native overnight control plane 作可验证基线，同时把 direct Claude-plan 数据面留在 fail-closed；把已有 Codex `exec` POC 接入 Mission，再升级到 Codex SDK；并行做 Prime/Pi LF-JSONL conformance；随后补 Codex App Server 和 Hermes Gateway/goal/Kanban 语义；最后做 ACP 和远端 runtime。** 这样 Collie 获得的是可持续扩展的 agent control plane，而不是四套彼此不兼容的 CLI wrapper。
