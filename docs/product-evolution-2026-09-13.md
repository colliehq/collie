# Collie：排队、取消、草稿与 24 小时持续改进

本轮从 2026-09-13 05:15 PDT 开始，后台任务截止于 2026-09-14 05:15 PDT。

## 已合入的产品修复

提交 `e5e073456b86dc9e478d05839131ce2ae747e275` 修复了跨界面继续任务时的设置漂移：网页排队请求保存了接受时的预算，但 REPL/TUI 的 `/next` 原先会使用后来修改的设置，可能提前中止任务，也可能放宽原先的预算。

两个终端入口现在都在完成本轮路由、确定实际 provider 之后应用接受时的预算、显式轮数上限和生成设置。显式环境限制仍优先；下一条手动输入恢复当前设置。损坏的预算快照会在 claim 之前被拒绝，请求继续保持 pending。普通交互没有新增默认 40 轮停止条件。

改动涉及 `harness/terminal_queue.py`、`harness/cli.py`、`harness/tui.py`，回归测试位于 `tests/test_accepted_queue_limits.py`。

第二个产品提交 `3178a31c5c74ededf6449f1aac2822cb05100bb0` 补齐同一路径的权限快照：终端领取网页已接受的任务时，保留当时的能力授权；后来开启的权限不会自动授予旧任务，后来撤销的权限仍立即生效。连续领取任务各用自己的权限；下一条手动输入恢复当前设置。损坏或不支持的快照在领取前被拒绝，任务保留。

这次独立专项回归 139 passed；同一新增测试在旧代码上 7 failed、5 passed；第二次 Claude Code 只读审阅 approve、无阻断项。新增测试覆盖 REPL 和 TUI 的实际工具上下文，并调用真实截图工具的权限判断，采集后端被替换，不实际截屏。精确测试快照为 `a4a79b46fa5db513b00592677c5a1eb50f39b392`，证据在运行目录 `manual/queue-policy/verification`。该快照的完整回归 `queue-policy-full` 已于 13:50 UTC 通过，3919 passed、20 skipped，全部门禁通过，耗时 998.746 秒。

提交 `c82a8260fdf0748b74231c3fdd648266269962a7` 修复外部 worker 的停止结果丢失：把 RunnerSnapshot 转成 RunResult 时，原代码没有保留取消标志，导致用户停止被显示成错误，甚至在停止后收到干净结束事件时显示完成。现在取消标志传到记录和界面，部分答案继续保留。独立回归 149 passed，新增行为在旧代码上两项失败，独立审阅 approve；精确快照 `ab5f7248`，证据在 `rounds/02-cancel-recovery`。

提交 `b68a62d164ec5bc9a6b3fe947bbab9a89e3265f5` 保留新任务中尚未确认的目录输入，避免切换会话或刷新后文字恢复、目录却回到上次确认值。目录概要显示尚未检查的选择，校验完成前不能在别处启动；已有会话继续使用自己的目录。独立回归 129 passed，相邻会话/目录套件 15 passed，新测试在旧代码上 3 failed、5 passed，审阅 approve。精确快照 `03de14a7`，证据在 `manual/draft-folder/verification` 和 `draft-folder-adjacent-tests`。

提交 `70bdc0ff70954b734818439a0c49785434072338` 让新建任务刷新后回到任务页面，保留草稿，不再跳回 Today。显式点击 Today 则继续回到 Today；会话深链优先，token 和桌面/IDE 参数保留，恢复页面不会启动任务。独立回归 125 passed，新测试在旧代码上 6 failed、4 passed，另一次 Claude Code 只读审阅 approve。精确快照 `e9c6dfa62337d062cef9ab3c99199785738ff117`，证据在 `manual/new-task-view/verification`；实际 Chrome 复测及截图在 `ui-new-task-view`。

提交 `09691c15ccad86f1d5f196f88d5755a613b858ed` 避免额度等待消耗 Mission 的无进展预算。原先连续三次额度拒绝后，任务会要求用户介入，即使 provider 已提供下一次重置时间。现在保存并识别有效的重置元数据，等待不算成无进展；没有有效重置时间时原有保护仍生效，显式预算、截止时间和恢复门禁保持原语义。独立回归 107 passed、只读审阅 approve；精确快照 `f4880fd5`。原新增测试的旧代码失败先触及缺失元数据字段，父流程因此又做了独立时钟推进实验：旧版第三个五小时窗口后 needs_you，新版连续六次拒绝仍等待，提前唤醒不调用 provider，缺失/过期重置仍触发原保护。证据在 `quota-advancing-before`、`quota-advancing-candidate`；是模拟时钟与 mock provider，没有真的耗尽六个额度窗口。

提交 `f685df87a0bae744096dabd95eb36f12223a3d38` 修复 live conformance 的失败依赖：第一轮未通过时不再额外调用 resume；没有完成的任务不会被误报为已完成但 usage 为 0。后续列保留主因并标为 UNVERIFIED，能力验证仍收紧；第一轮成功、续接失败时保留第一轮的有效用量。独立回归 105 passed、5 skipped，新测试在旧代码上 6 failed、3 passed，只读审阅 approve。精确快照 `df54b7c8`；真实 CLI 复测见下文。

## 验证证据

| 检查 | 结果 | 验证对象 |
| --- | --- | --- |
| 开始时快照的完整离线回归 | 3,896 passed、20 skipped；所有门禁通过 | `e709b141d8de72421ece19714f2086855238e3e0` |
| GUI 和其他界面 | GUI 63/63；surfaces 41/41 | 同一开始快照 |
| 排队预算与权限修复后的完整离线回归 | 3919 passed、20 skipped；GUI 63/63、surfaces 41/41、全部门禁通过 | `a4a79b46`，尚不含后来的取消与网页补丁 |
| 已合并的四项产品修复 | 121 passed | `a35de724`，覆盖排队、权限、worker 结果、网页草稿；是专项回归 |
| 修复后的专项回归 | 313 passed、1 skipped | 12 个相关测试文件，包括终端路由、恢复和任务 inbox |
| 同一新增测试文件放到旧代码上 | 8 failed、3 passed | 相同测试文件 SHA256；失败包括预算漂移、损坏快照和下一轮生成设置 |
| 独立 Claude Code 审阅 | approve，无阻断项 | 保存原始审阅及结构化结论 |
| 持续任务控制器 | 16 项单元/集成检查通过 | 包含真实 Git＋pytest 的通过提交和拒绝不提交场景 |
| 控制器维护后的验证 | 19 项通过 | 增加已完成批次恢复、旧进程存活时拒绝重复启动、保留原截止时间；真实 Claude Code 结构化审阅调用通过 |
| Windows 子进程生命周期 | 2 个真实场景通过 | 父进程退出、截止时间到达后，所属子进程均已终止 |

完整回归分别覆盖开始快照和排队修复快照；取消与网页修复另经上述专项检查。测试使用 mock provider、独立状态目录和临时数据，网络依赖检查按既有开关跳过，不能据此声称所有真实模型和平台均已验收。

第一轮 native Claude Code 编码调用保存了修复和测试，随后额外执行全量测试时触及 25 分钟单次时间限制。该调用保留为 timeout；父进程另行完成上述验证与审阅，没有将它记成成功结束的调用。

## 持续任务

控制器运行在 `C:\workspace\collie-improvement-2026-09-13`。易读状态为该目录的 `PROGRESS.md`，机器状态为 `status.json`，事件历史为 `events.jsonl`。

后台使用 native Claude Code 2.1.228、Claude Opus 5 / high 和已登录的 Claude Max 订阅。启动时确认 firstParty / claude.ai，额外付费未启用。每轮先读取额度；不足时等待自然重置，无法确认订阅路线时暂停新调用。

按小时最多安排 24 批，依次覆盖接受时的权限、网页连续操作、额度恢复、任务队列、会话存储、输出契约、配置隔离、worker 取消、验证流程、跨平台和上下文压缩。独立审阅发现的“终端 `/next` 尚未重放 frozen capabilities”已安排为第一批验证目标。

每个候选补丁都经过独立的旧代码失败复现、修复后回归和另一次只读 Claude Code 审阅，通过后提交到独立仓库 `automation-repo` 的 `automation/collie-24h-2026-09-13` 分支。每三个通过的批次额外运行完整回归。中断或未通过的候选补丁和日志也会保留。

独立仓库没有 remote，不共享原仓库 refs 或 objects。其起点 `3a0065399720ca4fc4f939c389a72e5e370e1e03` 是精确测试过的工作快照加首个修复。后续后台成果由进度文件记录。

当前自动分支已汇入上述七项修复，推进到 `775b9a866e99b083631ba943561e8650107ee890`。第 3 批额度恢复完成后，新任务视图和 conformance 修复合并经过 186 passed、5 skipped 的专项检查，并于 14:37 UTC 在控制器批次间隙用旧 tip 校验更新。证据在 `combined-quota-view-compat-tests` 和 `manual/combined-quota-view/merge.json`。完整回归仍只覆盖前述 `a4a79b46`，不把专项检查写成最新快照的完整回归。

控制器同时检查绝对截止时间、剩余单调时钟时间和 `STOP` 文件；所有主要执行任务由 Windows Job 管理子进程。创建运行目录下的 `STOP` 文件即可提前结束。本机休眠或关机会减少实际运行时间。

第一批审阅曾将非阻塞建议放入阻断项，控制器据此拒绝提交。维护后改用 Claude Code 的结构化输出，保留原批次证据，补齐测试后由父流程完成上述第二次审阅及合入。控制器从第二批恢复，原截止时间不变；第二批改查取消和恢复，避免与并行网页实验重复。隔离仓库的 Git 提交身份复用原项目本地配置。

## 网页体验复测

实际 Chrome、真实本地 web server 和 mock provider 验证了读取文件、任务完成、切换会话和草稿恢复。最初关于“已确认的工作目录在刷新后丢失”的判断经严格对照被推翻：明确选定并等待确认的目录能够恢复。原观察混入了新任务采用默认目录的行为，纠正记录与前后 DOM 均已保留。

进一步复现的是尚未确认的目录输入丢失，现已由 `b68a62d` 修复。修复后的实际浏览器检查保留未确认的 `folder-b` 路径和草稿，刷新后自动检查目录，然后真正读取该目录的 README，返回 `Collie UI fixture: folder-b`；DOM、截图和说明位于 `ui-pending-candidate`。编码调用达到 20 分钟时限，候选补丁被保留，再由父流程独立验证、审阅后合入；没有把超时调用记作成功。

一次 Send 自动检查目录仍是候选，尚未合入。第一次独立 100 项回归后，追加测试发现改选并确认 B 会让等待 A 的旧 Send 在 B 启动；此问题已修复，原失败与通过复测分别保留在 `folder-send-race-check`、`folder-send-race-after`。候选已与七项已提交修复合并，新的独立回归 114 passed、旧代码 15 failed/10 passed，实际 Chrome 一次 Send 自动检查并读取 folder-b 的证据在 `ui-folder-send-merged`。

父流程随后又在同一合并候选复现了重复目录检查导致 Send 停住：自动检查期间点击“使用此目录”，即使目录和草稿都未改变，旧等待也会被丢弃。草稿保留，但要再次 Send。`folder-send-duplicate-check` 保留失败；该候选审阅已停止，`manual/folder-send-merged/coalescing-followup` 正在修复各检查入口的合并行为。普通路径通过不代表所有连续操作已完成。

首次模型设置选择较多仍保留为体验观察。实际 Chrome 中有一次初次加载期间点击 New task 后仍显示 Today，随后点击正常；尚未通过受控条件复现，不把它归因于已确认缺陷，也不声称新视图修复覆盖它。

独立诊断用服务端延迟分别控制 Today 数据、配置和会话响应，未发现这些迟到响应覆盖新视图；另用 HTML 分段传输确实复现了“按钮已显示、末尾脚本尚未加载时点击被吞掉”。这是一个受控缺陷，但没有证据证明它就是前述实际 Chrome 观察的原因。`manual/ui-bootstrap/intent-followup` 正补齐早期显式导航与会话深链的先后语义，尚未合入。

## 真实 Claude Code 工作流实验

`live-claude-compat-v3` 使用已安装的 SDK 自带 Claude Code 2.1.228 运行 Collie 自带的 live conformance。默认模型拒绝旧 CLI，返回“该模型至少需要 2.1.251”，结果 8 PASS、3 FAIL。显式选择 Opus 5 的修复任务仍可运行，因此没有据此硬编码新的全局最低版本或自动切换模型。

官方 npm 包 2.1.270 安装在运行目录 `toolchains` 内，不替换全局 CLI、SDK 或已安装 Collie。[官方安装故障说明](https://code.claude.com/docs/en/troubleshoot-install)记录了 npm 平台原生包的检查方法。使用该隔离运行时重跑同一产品 conformance，`live-claude-compat-v4` 为 11 PASS、0 FAIL、0 SKIP、0 UNVERIFIED：真实模型修改由主机读取文件确认，第二次调用使用同一原生会话，usage 返回有效 token 数据。取消列使用真实所属子进程替身，没有调用真实模型，不能据此声称真实模型工具中断已经验收。

随后 `live-cli-flow` 在合并产品快照 `a35de724` 上，用未经改写的 `harness.cli run`、`claude-code` worker、2.1.270 和显式 Opus 5/high/standard 完成两轮任务。第一轮修复去重顺序与复制隔离，第二轮在同一 Collie 会话及同一原生 Claude locator 上追加生成器和输入验证要求。主机持有的测试没有被模型修改，分别 4 项、7 项通过；两轮均 completed、无 error、无 recovery_required，产品宿主验证及额外外部验证都成功。

`live-native-cli-flow` 又在快照 `775b9a86` 上经 `--runner collie --provider claude-agent-sdk` 跑了同一公开验收任务，覆盖 Collie 自己的工具循环和结构化响应路径。两轮 completed、保持同一 Collie 会话，受保护测试未被修改，产品宿主验证及额外外部检查均为 4/4、7/7；第二轮记录 5 次模型调用。这一路使用已安装官方 SDK 自带运行时，没有注入另一条 CLI 路径。它与外部 worker 的验收分开记录，不把不同运行时、不同工具循环下的耗时当成受控排名。

七项已提交修复的完整回归 `seven-fixes-full` 已于 14:55:50 UTC 开始，精确快照 `775b9a866e99b083631ba943561e8650107ee890`。仍在运行，不能提前报告通过；它不包含上述尚未合入的目录发送和早期点击候选。

调用前独立确认 Claude Max 登录、订阅额度与未启用额外付费，环境排除了 API-key 路线；产品的离线 probe receipt 仍将 billing 标为 unknown/unconfigured，不能将独立前置检查写成产品已完成账单认证。这是功能验收，样本不足以给不同 harness 排名；CLI 报告的美元数是用量折算，不作为订阅实际账单。

conformance 依赖关系修复后的真实对照保存在 `live-compat-prereq-old` 和 `live-compat-prereq-new`：旧运行时默认模型仍按原样失败，但报告变为 8 PASS、1 FAIL、2 UNVERIFIED，不再虚构额外的续接/用量失败；新运行时依旧 11 PASS。测试计划记录了精确工作区补丁 SHA256，与独立审阅对象相同。少一次调用由记录调用次数的行为测试验证，实际报告确认了依赖列未执行。

## 工作保全

开始时的 72 个未提交改动文件已逐文件备份并记录 SHA256。快照提交包含本轮开始前已有工作，不作为本轮新增成果归因。首个产品提交只包含经过审阅的四个文件增量，原工作区既有未提交改动继续保留。

原始运行日志、配置、测试报告和控制器源码保存在上述本地运行目录中。该目录的 `README.md`、`baseline.json`、`first-integration.json`、`controller-manifest.json` 记录入口及证据关联。
