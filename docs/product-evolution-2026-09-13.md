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

提交 `e63989135a0cc2c3a0e6e115350954fd96413c65` 让新任务的 Send 自动检查所填目录，检查通过后继续同一次发送。Send 与“使用此目录”的同一检查共用结果，重复点击不会吞掉请求或重复启动；等待时换目录、改字、替换附件或切换会话会保留当前草稿并放弃旧发送。已有会话仍使用保存的目录。最终独立回归 116 passed，旧代码 18 failed、9 passed，Claude Code 只读审阅 approve；其中一个旧失败是原有未修改测试的时序竞态，其余确定行为失败支撑修复。父流程针对目录变化和重复确认的独立失败/通过实验另行保留。精确测试快照 `48d7dfaf012d00b1f676fc445600829f391d17e6`，证据 `manual/folder-send-merged/verification-v2`。原工作区合入只应用审阅后的三文件增量，71 个无关脏文件哈希保持一致。

提交 `6a047ea6f7fcfae89b00c734f1550f0b100d0391` 保留页面末尾脚本到达前的 New task/Today 选择，并让该选择优先于更早的会话深链；早期新输入及主动清空优先于旧根草稿，按正常输入路径保存。恢复页面不会启动任务。独立 125 passed、旧代码 6 failed/13 passed、Claude Code 只读审阅 approve；补查 Mission、会话显示、任务连续性和成果交接共 26 passed。精确组合快照 `35d18c953f2ea2a0d98cd52ffc9b02bd4f95e0a9`，证据 `manual/ui-bootstrap-merged/verification` 和 `bootstrap-wider-ui-v2`。更早一次扩大检查因文件名写错而未收集测试，保留为无效尝试，不计通过。该修复只处理两个根视图按钮；其他导航在同一早期窗口、未选择根视图时的深链早期输入仍未覆盖。

提交 `28564f12cad7e16d470d66d3656743ce83be6238` 修复执行进程退出后任务仍被标成 claimed、无法开始/编辑/取消的队列。读取或编辑/取消入口先尝试非阻塞获取会话租约，再用现有日志核对：已经交付的标为 consumed，尚未交付的恢复 pending；活执行者仍持有租约时不干扰它，日志不可读时不凭猜测重新排队。独立 193 passed、旧代码 2 failed/15 passed、只读审阅 approve。完整回归 3954 passed、20 skipped，GUI 63/63、surfaces 41/41、全部门禁通过。精确快照 `d40a3fcb9e115ad6043c0d502199bd0a8e9cc46e`，证据 `rounds/04-queued-intent`；该完整快照不含第八、九项网页修复，组合检查另行记录。

提交 `32c790d27eb13bd5ebdb4af812ecb12cea24b93b` 修复外部 worker 的结束记录：正常停止时返回 canonical stop_reason/completed，读取持久化恢复状态并随第一条结束帧报告，调度器据此保留待处理任务；停止时保存已有的部分答案。恢复日志不可读时保留不确定性，不自动确认或清除恢复门禁。Claude Code 作者在临时切回旧代码做比较时超时；父流程从它保存的精确补丁恢复实现，叠加到十项修复快照，独立 208 passed、旧代码 6 failed/16 passed、只读审阅 approve 后合入。精确测试快照 `6786eca14c5c278c11b29fe0022502e26adbb11d`，证据 `manual/worker-terminal-merged/verification`；集成保留 70 个无关脏文件。聊天界面尚未消费 recovery 字段，正在单独补齐，不能把后端修复说成完整恢复体验已经完成。pack、断连异常等相邻终态的剩余差异也保留为后续检查项。

提交 `dcb87e2c659cf2c4935e3bcc602fec9a158c8393` 修复会话分叉恰好截在工具调用与结果之间时的历史不完整。分叉前缀现在给缺失的结果补充明确的分支说明，保留已记录结果，不声称外部动作没有执行或已撤销；父会话日志不变。独立 64 passed、旧代码 3 failed/23 passed、只读审阅 approve，精确快照 `7c9f79592ad389b084b046661c649003cb5a4e0f`。检查经过真实 provider 消息转换，但没有以此声称做过真实 API 拒绝对照。旧的已落盘分支暂不回填；分叉共享隔离目录的清理所有权另行修复。第十一、十二项组合为 `5a32299f17908c202e98579de8f674e823e04b36`，158 passed、2 skipped 后，于 16:29:34 UTC 更新自动分支，证据 `combined-fork-worker-checks`。

提交 `ed5f5efb5f6e16f0faeef8c9bd3b0d211dcb6e90` 修复 native/pack 的订阅显示分类遗漏：SDK 和已接受的别名不再漏出 plan 标签；使用实际执行 provider 的既有规范分类，外部 worker pack 使用已有 runner billing evidence。金额仍是 API 等价估算，没有增加账单认证或宣称实际收费为零。独立 193 passed、旧代码 10 failed/60 passed、只读审阅 approve；其中两项旧代码失败触及缺失 helper，另外八项为真实布尔结果错误。新增桌面测试固定已有 plan 提示与 metered 金额显示，是消费者合同检查，旧代码也通过。私有快照 `0429172b8e964716b5c2106917798f72d25806d3`，证据 `manual/subscription-label-merged/verification`。每个 pack attempt 及部分移动端金额标签暂未覆盖。

全部十三项的干净组合快照为 `87eeda438d4212b6c9aefa46014de6d4e27998e7`；180 项组合回归通过后，于 16:37:11 UTC 同步到自动分支。此前全十项的完整回归不能代替这个新组合的全量测试。聊天恢复入口候选的独立 175 项检查已通过，但尚未完成旧界面对照和只读审阅，不算已合入。

## 验证证据

| 检查 | 结果 | 验证对象 |
| --- | --- | --- |
| 开始时快照的完整离线回归 | 3,896 passed、20 skipped；所有门禁通过 | `e709b141d8de72421ece19714f2086855238e3e0` |
| GUI 和其他界面 | GUI 63/63；surfaces 41/41 | 同一开始快照 |
| 排队预算与权限修复后的完整离线回归 | 3919 passed、20 skipped；GUI 63/63、surfaces 41/41、全部门禁通过 | `a4a79b46`，尚不含后来的取消与网页补丁 |
| 七项修复的完整离线回归 | 3951 passed、20 skipped；全部门禁通过、surfaces 41/41 | `775b9a86`，`seven-fixes-full`，15:13 UTC 完成 |
| 十项修复的完整离线回归 | 3982 passed、20 skipped；GUI 63/63、surfaces 41/41、全部门禁通过 | `e2ad9541`，`ten-fixes-full`，16:16:25 UTC 完成；不含第十一项 worker 终态修复 |
| 已合并的四项产品修复 | 121 passed | `a35de724`，覆盖排队、权限、worker 结果、网页草稿；是专项回归 |
| 修复后的专项回归 | 313 passed、1 skipped | 12 个相关测试文件，包括终端路由、恢复和任务 inbox |
| 同一新增测试文件放到旧代码上 | 8 failed、3 passed | 相同测试文件 SHA256；失败包括预算漂移、损坏快照和下一轮生成设置 |
| 独立 Claude Code 审阅 | approve，无阻断项 | 保存原始审阅及结构化结论 |
| 持续任务控制器 | 16 项单元/集成检查通过 | 包含真实 Git＋pytest 的通过提交和拒绝不提交场景 |
| 控制器维护后的验证 | 19 项通过 | 增加已完成批次恢复、旧进程存活时拒绝重复启动、保留原截止时间；真实 Claude Code 结构化审阅调用通过 |
| Windows 子进程生命周期 | 2 个真实场景通过 | 父进程退出、截止时间到达后，所属子进程均已终止 |

完整回归已覆盖开始快照、排队修复快照以及七项、十项修复的合并快照。测试使用 mock provider、独立状态目录和临时数据，网络依赖检查按既有开关跳过，不能据此声称所有真实模型和平台均已验收。

第一轮 native Claude Code 编码调用保存了修复和测试，随后额外执行全量测试时触及 25 分钟单次时间限制。该调用保留为 timeout；父进程另行完成上述验证与审阅，没有将它记成成功结束的调用。

## 持续任务

控制器运行在 `C:\workspace\collie-improvement-2026-09-13`。易读状态为该目录的 `PROGRESS.md`，机器状态为 `status.json`，事件历史为 `events.jsonl`。

后台使用 native Claude Code 2.1.228、Claude Opus 5 / high 和已登录的 Claude Max 订阅。启动时确认 firstParty / claude.ai，额外付费未启用。每轮先读取额度；不足时等待自然重置，无法确认订阅路线时暂停新调用。

按小时最多安排 24 批，依次覆盖接受时的权限、网页连续操作、额度恢复、任务队列、会话存储、输出契约、配置隔离、worker 取消、验证流程、跨平台和上下文压缩。独立审阅发现的“终端 `/next` 尚未重放 frozen capabilities”已安排为第一批验证目标。

每个候选补丁都经过独立的旧代码失败复现、修复后回归和另一次只读 Claude Code 审阅，通过后提交到独立仓库 `automation-repo` 的 `automation/collie-24h-2026-09-13` 分支。每三个通过的批次额外运行完整回归。中断或未通过的候选补丁和日志也会保留。

独立仓库没有 remote，不共享原仓库 refs 或 objects。其起点 `3a0065399720ca4fc4f939c389a72e5e370e1e03` 是精确测试过的工作快照加首个修复。后续后台成果由进度文件记录。

14:37 UTC，自动分支先汇入七项修复到 `775b9a866e99b083631ba943561e8650107ee890`。第 3 批额度恢复完成后，新任务视图和 conformance 修复合并经过 186 passed、5 skipped 的专项检查，在控制器批次间隙用旧 tip 校验更新。证据在 `combined-quota-view-compat-tests` 和 `manual/combined-quota-view/merge.json`。该精确合并快照随后也通过完整回归，见验证表。

15:57 UTC，第 4 批队列修复与两项网页修复经过组合检查 280 passed，自动分支更新到 `e2ad95417952b8897b52be67571e8bde961f1a9f`，包括全部十项产品修复。干净组合 checkout 为 `manual/combined-queue-ui/product`，记录在 `combined-queue-ui-checks` 和其 `merge.json`。全十项的 `ten-fixes-full` 于 16:16:25 UTC 正常完成，耗时 1108.839 秒，结果见验证表。第 5 批会话存储任务已基于该快照开始；第十一项手动修复待批次结束后合并到自动分支。

控制器同时检查绝对截止时间、剩余单调时钟时间和 `STOP` 文件；所有主要执行任务由 Windows Job 管理子进程。创建运行目录下的 `STOP` 文件即可提前结束。本机休眠或关机会减少实际运行时间。

第一批审阅曾将非阻塞建议放入阻断项，控制器据此拒绝提交。维护后改用 Claude Code 的结构化输出，保留原批次证据，补齐测试后由父流程完成上述第二次审阅及合入。控制器从第二批恢复，原截止时间不变；第二批改查取消和恢复，避免与并行网页实验重复。隔离仓库的 Git 提交身份复用原项目本地配置。

## 网页体验复测

实际 Chrome、真实本地 web server 和 mock provider 验证了读取文件、任务完成、切换会话和草稿恢复。最初关于“已确认的工作目录在刷新后丢失”的判断经严格对照被推翻：明确选定并等待确认的目录能够恢复。原观察混入了新任务采用默认目录的行为，纠正记录与前后 DOM 均已保留。

进一步复现的是尚未确认的目录输入丢失，现已由 `b68a62d` 修复。修复后的实际浏览器检查保留未确认的 `folder-b` 路径和草稿，刷新后自动检查目录，然后真正读取该目录的 README，返回 `Collie UI fixture: folder-b`；DOM、截图和说明位于 `ui-pending-candidate`。编码调用达到 20 分钟时限，候选补丁被保留，再由父流程独立验证、审阅后合入；没有把超时调用记作成功。

一次 Send 自动检查目录已由上文第八项提交合入。过程中第一次独立 100 项回归后，追加测试发现改选并确认 B 会让等待 A 的旧 Send 在 B 启动；此问题已修复，原失败与通过复测分别保留在 `folder-send-race-check`、`folder-send-race-after`。候选与七项已提交修复合并后的阶段回归为 114 passed、旧代码 15 failed/10 passed，实际 Chrome 一次 Send 自动检查并读取 folder-b 的证据在 `ui-folder-send-merged`。实际截图对应阶段补丁 bea，不是最终 090 补丁的逐字节验收。

父流程随后又在同一合并候选复现了重复目录检查导致 Send 停住：自动检查期间点击“使用此目录”，即使目录和草稿都未改变，旧等待也会被丢弃。草稿保留，但要再次 Send。`folder-send-duplicate-check` 保留失败，原候选审阅正常停止。Claude Code 的 `manual/folder-send-merged/coalescing-followup` 完成修复，同一目录选择的各检查入口共用在途检查；父流程原复现在 `folder-send-duplicate-after` 通过。最终独立验证和只读审阅均通过后才合入，见上文。

首次模型设置选择较多仍保留为体验观察。实际 Chrome 中有一次初次加载期间点击 New task 后仍显示 Today，随后点击正常；尚未通过受控条件复现，不把它归因于已确认缺陷，也不声称新视图修复覆盖它。

独立诊断用服务端延迟分别控制 Today 数据、配置和会话响应，未发现这些迟到响应覆盖新视图；另用 HTML 分段传输确实复现了“按钮已显示、末尾脚本尚未加载时点击被吞掉”。这是一个受控缺陷，但没有证据证明它就是前述实际 Chrome 观察的原因。`manual/ui-bootstrap/intent-followup` 补齐早期显式导航与会话深链的先后语义，但调用在 15:08 UTC 达到时限，保留候选，不记作成功结束。

父流程又在该候选复现旧根草稿覆盖早期新输入和主动清空的情况，补上输入事件记录、按当前根视图恢复并通过正常输入处理保存。原候选两项失败的证据在 `early-draft-precedence-before`，修正后同一外部检查及页面连续性套件在 `early-draft-precedence-after` 通过。

扩大回归出现一个未修改的目录测试失败：它只等待输入后立即更新的目录概要，就在 Use folder 检查结束前发送。以 0.8 秒服务端延迟，在 clean 775 和启动候选上均复现相同 Send 丢失；证据 `bootstrap-adjacent-folder-base/candidate` 的通过表示诊断断言确认缺陷存在，不是产品验收通过。该问题由已独立接受的目录发送提交解决。启动增量随后叠加到 `48d7dfaf`，新的 `manual/ui-bootstrap-merged/verification` 为 125 passed、旧代码 6 failed/13 passed，只读审阅及额外 26 项相邻回归通过后已合入第九项提交。

## 真实 Claude Code 工作流实验

`live-claude-compat-v3` 使用已安装的 SDK 自带 Claude Code 2.1.228 运行 Collie 自带的 live conformance。默认模型拒绝旧 CLI，返回“该模型至少需要 2.1.251”，结果 8 PASS、3 FAIL。显式选择 Opus 5 的修复任务仍可运行，因此没有据此硬编码新的全局最低版本或自动切换模型。

官方 npm 包 2.1.270 安装在运行目录 `toolchains` 内，不替换全局 CLI、SDK 或已安装 Collie。[官方安装故障说明](https://code.claude.com/docs/en/troubleshoot-install)记录了 npm 平台原生包的检查方法。使用该隔离运行时重跑同一产品 conformance，`live-claude-compat-v4` 为 11 PASS、0 FAIL、0 SKIP、0 UNVERIFIED：真实模型修改由主机读取文件确认，第二次调用使用同一原生会话，usage 返回有效 token 数据。取消列使用真实所属子进程替身，没有调用真实模型，不能据此声称真实模型工具中断已经验收。

随后 `live-cli-flow` 在合并产品快照 `a35de724` 上，用未经改写的 `harness.cli run`、`claude-code` worker、2.1.270 和显式 Opus 5/high/standard 完成两轮任务。第一轮修复去重顺序与复制隔离，第二轮在同一 Collie 会话及同一原生 Claude locator 上追加生成器和输入验证要求。主机持有的测试没有被模型修改，分别 4 项、7 项通过；两轮均 completed、无 error、无 recovery_required，产品宿主验证及额外外部验证都成功。

`live-native-cli-flow` 又在快照 `775b9a86` 上经 `--runner collie --provider claude-agent-sdk` 跑了同一公开验收任务，覆盖 Collie 自己的工具循环和结构化响应路径。两轮 completed、保持同一 Collie 会话，受保护测试未被修改，产品宿主验证及额外外部检查均为 4/4、7/7；第二轮记录 5 次模型调用。这一路使用已安装官方 SDK 自带运行时，没有注入另一条 CLI 路径。它与外部 worker 的验收分开记录，不把不同运行时、不同工具循环下的耗时当成受控排名。

七项已提交修复的完整回归 `seven-fixes-full` 于 15:13:04 UTC 正常完成，耗时 1034.799 秒，精确快照 `775b9a866e99b083631ba943561e8650107ee890`，无工作区差异。3951 passed、20 skipped，全部门禁通过；它不包含上述尚未合入的目录发送和早期点击候选。

调用前独立确认 Claude Max 登录、订阅额度与未启用额外付费，环境排除了 API-key 路线；产品的离线 probe receipt 仍将 billing 标为 unknown/unconfigured，不能将独立前置检查写成产品已完成账单认证。这是功能验收，样本不足以给不同 harness 排名；CLI 报告的美元数是用量折算，不作为订阅实际账单。

conformance 依赖关系修复后的真实对照保存在 `live-compat-prereq-old` 和 `live-compat-prereq-new`：旧运行时默认模型仍按原样失败，但报告变为 8 PASS、1 FAIL、2 UNVERIFIED，不再虚构额外的续接/用量失败；新运行时依旧 11 PASS。测试计划记录了精确工作区补丁 SHA256，与独立审阅对象相同。少一次调用由记录调用次数的行为测试验证，实际报告确认了依赖列未执行。

### 真模型工具执行中的停止与继续

Claude Code 编写的实验工具在 `manual/live-cancel-design`，使用实际 stock webapp、独立临时项目和宿主验收。父流程先复现并修正两个实验判定漏洞：过期工具入口文件不证明进程仍存活，未收到 tool_result 也可能只是事件尚在缓冲。修正后只有活着、内容未改、尚未结束的短时工具进程才建立 Collie 工具边界；外部 worker 的事件顺序仍标为未验证。父流程还核对真实产品完成状态、有效 JSON 部分成果、所需验证结果以及全部进程清理。原生作者版本、修正补丁、模拟试跑和独立检查都保留。

`live-cancel-collie-v1` 于 15:42 UTC 完成，精确产品仍为 `775b9a86`。模型先修改文件，再真正通过 bash 运行最长 90 秒的临时 hold.py；宿主确认该进程存活后按 Stop，约 95.2 ms 收到 canceled 结束帧，工具实际运行约 0.3 秒便退出，没有完成标记。部分修改保留、受保护测试未改，随后同一产品会话继续，产品明确 completed、required verification 通过、额外宿主 4/4 通过，所属进程全部清理。这是一个真实功能样本，不能推出一般延迟或成功率。

`live-cancel-worker-v1` 也经实际外部 Claude Code worker 接受 Stop 并返回 canceled，部分修改保留。其工具事件无法证明物理执行边界，因此该能力仍未验证。显式继续收到 recovery_required 的起跑前拒绝：必须先检查中断效果。实验整体 FAIL 表示直接继续未达成，不意味着恢复检查本身错误。更具体的产品缺口是第一条 worker 结束帧没有携带恢复状态，用户再次尝试才看见原因；报告路径已作为第十一项后端修复合入，真实恢复门禁保留。

`live-worker-terminal-v2` 于 16:19:22 UTC 收尾，精确快照 `6786eca1`、同一隔离 Claude Code 2.1.270 / Opus 5。第一条停止帧立即带上 completed=false、stop_reason=canceled、recovery_required=true 和 external_action。实验据此跳过第二次请求，避免一次已知无效的重试；宿主确认部分修改保留、受保护测试未改、所属进程树已清理。原工作流总判定仍为 FAIL（直接继续未达成），物理工具边界仍 UNVERIFIED；独立读取原始前后结束帧的 `terminal-contract.json` 将“恢复元数据修复”单列为 PASS。不是完整恢复工作流或成功率排名。

16:13–16:15 UTC 的实际浏览器检查 `ui-ten-fixes` 在干净 e2ad 上使用 mock provider：刷新后新任务文字与尚未确认的 folder-a 保留；改为 folder-b 后只按一次 Send、未按 Use folder，唯一会话读取了 folder-b 的真实 README；再刷新同会话，答案和目录保持。截图和 DOM 记录已保存，测试页面及服务已关闭。首次加载的一次 New task 点击仍有未归因的异常观察，没有把它记为已解决。

该 worker 实验还暴露工具自身的记录问题：已收到起跑前 done，却又等待 start 180 秒，并在摘要丢掉 done。原始 SSE 完整保留，父流程已修正等候与摘要逻辑、两项检查通过，没有重写旧实验。两次 live 执行的原驱动 SHA256 均为 `1be9d3cf…a6d9f233`，代码字节按执行前后哈希核对后保存在各外层运行目录 sources/driver.py；后来的读取修正另有版本。各 runtime、费用估算、订阅前置检查继续分开记录，不混为 harness 排名或实际账单。

## 工作保全

开始时的 72 个未提交改动文件已逐文件备份并记录 SHA256。快照提交包含本轮开始前已有工作，不作为本轮新增成果归因。首个产品提交只包含经过审阅的四个文件增量，原工作区既有未提交改动继续保留。

原始运行日志、配置、测试报告和控制器源码保存在上述本地运行目录中。该目录的 `README.md`、`baseline.json`、`first-integration.json`、`controller-manifest.json` 记录入口及证据关联。
