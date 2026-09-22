# Collie 与当前 coding harness 产品实测（2026-08-12）

## 结论

在本轮受限的小型代码任务测试中，Collie 与 Codex **并列第 1**：两边都是
`6/6` 通过外置隐藏 grader，正式轮 solve rate 均为 `100%`。

| 排名 | 产品臂 | 模型与推理档位 | 隐藏测试 | 总体中位时长 |
| --- | --- | --- | ---: | ---: |
| 1（并列） | Collie 原生循环 + Claude Agent SDK | `claude-opus-4-8`, high | 6/6 | 51.413 s |
| 1（并列） | Codex CLI | 请求 `gpt-5.6-sol`, high | 6/6 | 50.217 s |

这不是“Collie 已经全面等于 Codex”的结论。它只说明：在两个冻结的 synthetic
代码契约任务、每个任务每臂三次的条件下，Collie 没有出现正确率劣势。两个任务才是
两个独立问题，重复运行主要衡量稳定性，不能当作 12 个独立 benchmark 样本。

## 实测协议

- 独立 admission：Codex 与 Collie 各一次，必须实际调用本地工具、写出非空 patch，
  并通过隐藏 grader，才启动正式轮。
- 正式轮：2 个任务 × 3 次 × 2 个产品臂，共 12 次；顺序按 AB/BA 反平衡。
- 每次使用 fresh Git repo；agent 只能看到 fixture 与公开 issue。
- gold implementation 与 hidden grader 不进入 agent 镜像；grader 在容器外执行。
- 同一任务给两边的 evaluator prompt 字节一致。
- 正确率是唯一排名指标；速度和 token 只作描述，不能打破正确率平局。
- 任一 auth、sandbox、模型回退、工具权限或适配器异常均为 infrastructure invalid，
  不能计作某个产品的能力失败。

最终有效 suite：`fbedd427eb6b69f1f20822c675b53e8ae7d17e2b7794b9afdfeb9de00a9ffebc`。
它绑定到源码提交 `00f391f0416792a5b8e2541c5e82348fc9943057` 和 Docker image
`sha256:754ad3abc430ddabb45453cf1b37add63d7a350abdbbeb241301edbbdc84317a`。

运行环境固定为：

- Collie：官方 Claude Agent SDK `0.2.136`，`claude-opus-4-8`，high effort，
  不调用 `claude -p`，Collie 保留自己的 system prompt、工具协议和循环。
- Codex：CLI `0.147.0`，显式请求 `gpt-5.6-sol`，high reasoning，fresh
  `CODEX_HOME`、`--ephemeral`、`workspace-write`。
- Codex 的 web、MCP、plugins、apps、subagents、goals、memory 等外部能力关闭；
  本地 `shell_tool`、`unified_exec`、`code_mode_host` 和 `shell_snapshot` 保留。
- 外层容器 drop all capabilities、no-new-privileges、read-only root；无模型预检证明
  unprivileged user namespace 和 workspace write canary 均可用。

## 分任务结果

| 任务 | Collie（3/3）中位时长 | Codex（3/3）中位时长 | 观察 |
| --- | ---: | ---: | --- |
| request ID 端到端传播 | 77.703 s | 47.738 s | Codex 在这个多文件小改动上更快 |
| circuit breaker 状态机 | 43.431 s | 52.695 s | Collie 在这个单文件状态机任务上更快 |

总体速度几乎相同，但任务间方向相反，因此不应从这两个任务推导普遍速度排名。

Collie 在第一个任务的三次运行各用了 13 个物理模型请求；第二个任务用了
6、7、7 个。所有请求都有 durable reserve/settle 记录且全部完成。Codex CLI
只报告聚合 usage，不公开可独立核验的内部模型请求次数，因此不能做公平的 request
count 或 token-efficiency 排名。这反而暴露出 Collie 下一步很值得优化的点：在保持
正确率的同时减少多文件任务的循环次数。

## 订阅与计费核验

本轮只使用现有订阅登录，没有 API key fallback：

- Collie：Claude Max first-party 登录；usage credits 关闭、auto-reload 关闭。
- Codex：ChatGPT 登录；credits 为 0、automatic reload 关闭。
- 运行后再次检查：Claude 仍为 `$0.00 spent`，Codex 仍为 `0 credits`，两边自动
  充值均未开启。

这证明没有观察到 credits/额外用量扣费，不代表订阅额度没有被消耗，也不是供应商
账单级计量凭证。Claude 当前 session usage 在测试期间上升，属于 Max 套餐额度消耗。

## 为什么 Prime、Pi、Hermes 和 Claude Code 没有分数

| Harness | 本轮状态 | 原因 |
| --- | --- | --- |
| Codex | 已实测 | 现有 ChatGPT 登录可被隔离复用，并通过 admission |
| Claude Code | 有意排除 | `claude -p` 会带入 Claude Code 的 system prompt，不满足“Collie 自己的 prompt/loop”条件 |
| Prime | 未入场 | 本机需要在 Prime 自己的交互界面完成 `/login`；未授权时不冒充实测 |
| Pi | 未入场 | 需要 Pi 自己的交互式登录与凭据存储；本轮没有该会话 |
| Hermes | 未入场 | `openai-codex` 当前未登录；导入凭据会修改 Hermes 配置并需要明确确认 |

因此这里是 **Collie vs Codex 的真实产品微基准**，不是把 Prime/Pi/Hermes 用资料推测
出来的排行榜。下一版若要纳入它们，必须先逐个完成交互式登录，再让每个 harness
通过同一套 billing guard、write canary、fresh workspace 和 hidden grader。

## 调试中被排除的运行

正式 suite 之前的尝试全部排除，不跨版本拼接结果。它们帮助修复了四个真实的
harness 集成问题：

1. Docker PID 1 未回收 Agent SDK watchdog zombie，导致 Collie 的进程树消亡证明
   fail-closed；加入 `--init` 后修复。
2. Docker 默认 seccomp 阻止 Codex 创建 Linux user namespace；在仍保留 cap-drop、
   no-new-privileges、read-only root 和受限 mounts 的前提下开放 namespace syscall，
   并加入无模型 write canary。
3. 过度关闭 `code_mode_host`/`shell_snapshot` 让 Codex 只能文字回答、不能调用本地
   coding tools；现在只关闭外部能力，保留原生本地执行面。
4. PATH alias 的无害 permission warning 被旧解析器与 “patch” 文本拼接，误判为写入
   拒绝；现在只检查失败写命令自身的权限输出。

最终 suite 没有 infrastructure invalid，12 个保存下来的 patch 也被重新应用到 fresh
fixture 并再次独立评分：`12/12` 全部通过，patch SHA 全部与运行记录一致。

## 对 Collie 的直接建议

1. 保留 Agent SDK 作为正式 subscription-native provider：它已经证明能用 Collie 自己
   的 system prompt/loop 完成真实代码修改，不需要 `claude -p`。
2. 把 admission 变成所有 harness integration 的强制接口：认证、模型、local tool、
   非空 patch、隐藏 grader 任一缺失就停止，不进入排行榜。
3. 继续沿用 durable request ledger 与进程树所有权；这是把一次性 demo 升级成 overnight
   agent 的必要基础。
4. 下一轮先优化 Collie 的请求效率，再扩大任务面；当前第一个任务每次 13 requests，
   是最明显的改进目标。
5. 另开 endurance 评测：8–12 小时、故障注入、daemon 重启、断点恢复、配额窗口变化。
   本轮短任务不能证明“能稳定跑一晚上”。

## 证据文件

- `bench/results/current-product-v1-fbedd427eb6b/manifest.json`
- `bench/results/current-product-v1-fbedd427eb6b/summary.json`
- `bench/results/current-product-v1-fbedd427eb6b/post-run-billing.json`
- 同目录 `runs/*/result.json`、`grader.json` 与 `patch.diff`

这些结果明确标记为 `publishable: false` 和
`subscription_native_product_comparison_not_harness_only`：两边使用不同模型家族和产品原生
工具语义，不能把它改写成纯 harness 因果比较或公开通用能力榜。
