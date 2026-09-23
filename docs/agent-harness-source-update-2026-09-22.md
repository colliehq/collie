# Agent harness 源码复核：2026-09-22

本次补充 [已有的八项目对照](agent-harness-reference-2026-09-06.md)，重新阅读
Codex、OpenCode 和 Goose 的指定代码路径。它不是三套产品的完整评测，也没有运行
OpenCode 或 Goose 的任务成功率基准。版本固定到下表，不能把实验路径当成所有用户的默认体验。

| 项目 | 本次检出的 commit | 阅读范围 |
|---|---|---|
| Codex | `fe74a774532af67b5a4a3dec03ce9469e17f89af`，`rust-v0.156.0` | Windows 配置兼容、CLI 与 app-server 启动契约 |
| OpenCode | `2406400f0aeb07b36d0495af4e05aaca49159832` | `packages/core/src/session` 的 V2 输入与运行循环，以及对应规格 |
| Goose | `7b68b47ffd6cfe2ffc52342e19b6fd03ec32b609` | 默认执行器、最终输出工具、实验状态机的若干操作 |

## 先核实入口，再比较架构

OpenCode 的 V2 在 `packages/core/src/session`，旧入口仍在
`packages/opencode/src/session`。本次版本的 V2 规格明确列出尚未完成的 V1 行为，
事件数据库也仍被描述为可重建的实验状态。因此，V2 的设计值得参考，但规格中的目标
不等于已经替换全部旧路径。[V2 对照清单](https://github.com/anomalyco/opencode/blob/2406400f0aeb07b36d0495af4e05aaca49159832/specs/v2/session.md#L123)、
[事件兼容说明](https://github.com/anomalyco/opencode/blob/2406400f0aeb07b36d0495af4e05aaca49159832/specs/v2/schema-changelog.md#L681)。

Goose 的新状态机有独立的操作模块；`GOOSE_STATE_MACHINE` 的读取函数在没有设置时
返回 false。下面借鉴其具体机制，不据此宣称所有 Goose 入口都已采用新状态机。
[开关实现](https://github.com/aaif-goose/goose/blob/7b68b47ffd6cfe2ffc52342e19b6fd03ec32b609/crates/goose/src/agents/state_machine/mod.rs#L72)。

Collie 不需要为了采用相同抽象而增加第二套执行循环。先证明现有用户路径存在缺口，
再用可复现的用例判断改动是否值得。

## 值得保留的执行契约

**接受请求应是持久化动作。** OpenCode V2 的输入有稳定 ID；重复接收先找已有记录，
新输入通过持久事件取得序号。这个机制对应浏览器重试、运行中追加要求和进程重启，
比单纯增加一个“继续”按钮更有价值。Collie 已有持久 inbox 和单会话执行所有权，
应该验证真实 HTTP 路径是否保持这些契约。[输入接收实现](https://github.com/anomalyco/opencode/blob/2406400f0aeb07b36d0495af4e05aaca49159832/packages/core/src/session/input.ts#L41)。

**中断记录不能被理解为没有发生效果。** OpenCode V2 会为历史中尚未结算的工具写入
中断失败事件。这里读取的代码说明了它如何补齐记录，不能据此推断整个产品的重放策略。
Collie 的恢复检查需要继续区分可重做的读取和效果未知的动作；后者不能因为出现一个
error 字段就自动重做。[中断记录实现](https://github.com/anomalyco/opencode/blob/2406400f0aeb07b36d0495af4e05aaca49159832/packages/core/src/session/runner/llm.ts#L119)。

**交付物可以有机器可检查的格式契约。** Goose 的最终输出工具在接受结果前校验 JSON
schema，默认执行器也接入了该工具；这是独立于新状态机的能力。Collie 的通用自动化
还没有相同的交付物字段，但是否增加应由具体工作流决定。结构校验只证明格式，不证明
数字、来源或外部操作正确。[校验与接受结果](https://github.com/aaif-goose/goose/blob/7b68b47ffd6cfe2ffc52342e19b6fd03ec32b609/crates/goose/src/agents/final_output_tool.rs#L104)、
[默认执行器接入](https://github.com/aaif-goose/goose/blob/7b68b47ffd6cfe2ffc52342e19b6fd03ec32b609/crates/goose/src/agents/agent.rs#L1026)。

**预算停止要有准确的结果状态。** Goose 的实验状态机把已用轮数提示给模型，并在
达到上限时让出控制权。这是可参考的信息提示，不是要求 Collie 恢复任意的 40 轮硬停。
Collie 的交互默认、用户显式上限和无人值守预算应分别保留；达到显式上限的任务必须
显示尚未完成，保留已有结果。[预算操作](https://github.com/aaif-goose/goose/blob/7b68b47ffd6cfe2ffc52342e19b6fd03ec32b609/crates/goose/src/agents/state_machine/ops_maxturns.rs#L14)。

本次没有完整审阅 Goose 的 recipe 分发和权限存储，也没有完整审阅所有项目的执行锁。
在几个路径中没有找到某机制，不能写成“竞品都没有”，更不能据此证明产品优势。
操作系统锁在进程退出后会释放；能跨重启延续的是持久状态和恢复协议，不是锁本身。

## 依赖升级与工作流验证

Codex 0.156 移除了 Collie 旧 CLI 参数中的 Windows 配置键。兼容处理需要根据实际
解析到的可执行文件版本选择参数，保留原有 sandbox 和 approval 策略。仅修改 SDK
依赖版本不能保证用户 PATH 上的 CLI 也兼容。[固定版本源码](https://github.com/openai/codex/tree/fe74a774532af67b5a4a3dec03ce9469e17f89af)。

本次真实 CLI 协议检查覆盖 0.155.1 和 0.156 的初始化、模型列表和会话启动；它没有
证明新版模型写入及恢复已验证，因此不能把协议通过写成完整升级通过。

后续评估按不同证据分开记录：

1. 产物是否满足任务要求，由 agent 工作区之外的检查器判断。
2. 中间操作是否遵守用户的文件、命令和访问限制，包括随后删除的临时文件。
3. 最终说明是否准确，只报告实际执行和观察到的检查。
4. 输入是否持久化且只消费一次，取消后是否留下明确状态，进程是否清理。
5. 界面是否只在需要用户决定时提示，正常等待和自动排队是否被误报为异常。

最终文件正确，不能替代其余四项。宿主请求计数、SDK 内部请求数、原生 CLI 轮数和
实际计费也不是同一个量。小样本合成任务可以发现缺陷；它不能证明普遍领先、客户
留存或长期产品价值。
