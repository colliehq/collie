"""User settings — one place to configure collie's many knobs, instead of remembering 35 env vars.

Layered precedence: an explicit env var (COLLIE_*) ALWAYS wins (so CLI/scripts stay authoritative),
then the saved settings.json (what the web Settings panel writes), then the code default. The web
GUI reads SCHEMA to render the panel and GET/POSTs the values; make_harness/_provider/_embedder read
`get()` so a saved setting takes effect on the next run with zero env fiddling.
"""
import json
import os
import time
import unicodedata

_PATH = os.environ.get("COLLIE_SETTINGS_PATH") or os.path.expanduser("~/.collie/settings.json")
_cache = {"mtime": -1.0, "data": {}}
# env vars set BEFORE we ran are authoritative (a user's CLI `COLLIE_X=… collie …` must win over a
# saved panel value); apply() never overwrites these.
# Env vars the USER set, which outrank the Settings panel. Snapshotted at import — but a var this
# process INHERITED from a parent Collie is not a user override, and treating it as one is how the
# panel silently stopped working: apply() exports every saved setting as COLLIE_<KEY>, the desktop
# app spawns the web server as a child (start_server_windowless), the child inherits those exports,
# and its own snapshot then classes them as hard-set. From that point apply() skips them forever —
# the panel saves, the file is correct, and the running server keeps answering with the value it
# started with. Measured: settings.json said LANG=zh while the live server reported en.
#
# So apply() announces what it injected, and those keys are excluded here. A var a real user
# exported carries no such marker and still wins.
_INJECTED_ENV = "COLLIE_APPLIED_KEYS"
_inherited = {k.strip() for k in (os.environ.get(_INJECTED_ENV) or "").split(",") if k.strip()}
_HARD_ENV = {k for k in os.environ if k.startswith("COLLIE_")} - _inherited - {_INJECTED_ENV}


# Each knob: key (the settings.json field + the env var suffix COLLIE_<KEY>), label, type, default,
# and (for select/bool) options. Grouped for the panel. ONLY user-facing knobs — debug/internal
# env vars (COLLIE_DEBUG, COLLIE_RPC_PORT, COLLIE_SUBAGENT, …) are intentionally omitted.
# Types: select (options=[str] or [{value,label}]), text (optional list=[…] for a datalist of
# suggestions), number (optional min/max/step), bool (rendered as a toggle; stored "on"/"off").
# `hint` is the one-line help shown under the control — every knob gets one so nothing is a mystery.
SCHEMA = [
    {"group": "Identity", "key": "COMPANION_NAME", "label": "Companion name",
     "label_zh": "伙伴名字", "type": "text", "default": "", "max": "32",
     "placeholder": "Rowan", "placeholder_zh": "Rowan",
     "hint": "The personal name shown for this Collie across Home, phone and ambient desktop. "
             "This does not rename Slack apps, @handles or mail addresses. An explicit web "
             "--name selects a kennel dog and stays authoritative for that server.",
     "hint_zh": "这只 Collie 在主页、手机和动态桌面上显示的名字。不会改动 Slack 应用、@用户名或邮件地址；"
                "显式的 web --name 用来选择犬舍成员，并始终以该名字为准。"},
    {"group": "Identity", "key": "PROFILE_AGE_BAND", "label": "Age eligibility",
     "label_zh": "年龄资格", "type": "select", "default": "unset",
     "options": [
         {"value": "unset", "label": "Not provided", "label_zh": "未提供"},
         {"value": "16", "label": "I am 16 or older", "label_zh": "我已满 16 岁"},
         {"value": "18", "label": "I am 18 or older", "label_zh": "我已满 18 岁"},
         {"value": "21", "label": "I am 21 or older", "label_zh": "我已满 21 岁"}],
     "hint": "A local eligibility claim, not your birth date. Collie may reuse it only when "
             "automatic profile claims are enabled below and a form asks the same or a lower "
             "threshold. CAPTCHA, biometric/KYC, signatures and person-required MFA never use it.",
     "hint_zh": "仅保存在本机的年龄资格声明，不保存生日。只有下方开启自动使用个人事实时，Collie "
                "才会在表单询问相同或更低年龄门槛时复用；CAPTCHA、生物识别/KYC、签名和明确要求本人"
                "完成的 MFA 永远不会因此自动通过。"},
    # UI language: the web GUI chrome + this panel render in it. auto = follow the browser.
    # label_zh / hint_zh on any entry (and label_zh inside options) localize the panel — the GUI
    # picks them when the resolved language is zh; missing translations fall back to English.
    {"group": "General", "key": "LANG", "label": "Language", "label_zh": "界面语言", "type": "select",
     "default": "auto",
     "options": [
         {"value": "auto", "label": "Auto (follow browser)", "label_zh": "自动(跟随浏览器)"},
         {"value": "en", "label": "English"},
         {"value": "zh", "label": "简体中文"},
         {"value": "zh-tw", "label": "繁體中文"}],
     "hint": "Language of every web surface. English and Chinese are currently complete; auto follows those browser languages and otherwise uses English.",
     "hint_zh": "所有 Web 界面的显示语言。目前英文和中文已完整覆盖；auto 在其他浏览器语言下使用英文。"},
    # API key is the default.  The official Agent SDK is the supported Claude-plan route for
    # Collie's own harness; anthropic-oauth remains an experimental raw request and must never be
    # described as guaranteed-free or silently selected.
    {"group": "Model", "key": "PROVIDER", "label": "Provider", "label_zh": "模型提供方", "type": "select", "default": "anthropic",
     "options": [
         {"value": "anthropic", "label": "Anthropic API (API key, metered)"},
         {"value": "claude-agent-sdk", "label": "Claude Agent SDK (your Claude plan; Collie harness)"},
         {"value": "anthropic-oauth", "label": "Claude direct (experimental; billing unverified)"},
         {"value": "codex-oauth", "label": "ChatGPT Codex subscription (OAuth)"},
         {"value": "claude-cli", "label": "Claude CLI (your logged-in CLI)"},
         {"value": "gemini", "label": "Google Gemini (GEMINI_API_KEY) ☁"},
         {"value": "openai", "label": "OpenAI (OPENAI_API_KEY) ☁"},
         {"value": "deepseek", "label": "DeepSeek (DEEPSEEK_API_KEY) ☁"},
         {"value": "openrouter", "label": "OpenRouter — many models (OPENROUTER_API_KEY) ☁"},
         {"value": "groq", "label": "Groq (GROQ_API_KEY) ☁"},
         {"value": "moonshot", "label": "Moonshot / Kimi (MOONSHOT_API_KEY) ☁"},
         {"value": "zhipu", "label": "Zhipu GLM (ZHIPU_API_KEY) ☁"},
         {"value": "qwen", "label": "Qwen / DashScope (DASHSCOPE_API_KEY) ☁"},
         {"value": "ollama", "label": "Ollama (local models — nothing leaves this machine)"},
         {"value": "openai-compat", "label": "OpenAI-compatible endpoint"},
         {"value": "mock", "label": "Mock (offline, canned — testing only)"}],
     "hint": "Where completions come from. ☁ = third-party cloud: your prompt, code excerpts and tool output are sent to that vendor under its data policy (keys are read from the named env var, never stored by collie; secret redaction below keeps credentials out of what any vendor sees). Claude Agent SDK uses your logged-in Claude plan with Collie's harness; plan limits and current provider policy still apply. Claude direct is experimental and its availability/billing are not guaranteed. Ollama/mock stay fully local."},
    {"group": "Model", "key": "MODEL", "label": "Model", "type": "text", "default": "",
     "list": ["claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5",
              "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna",
              "gemini-2.5-pro", "gemini-2.5-flash", "gpt-4o-mini", "deepseek-chat", "deepseek-reasoner"],
     "hint": "Optional exact model pin. Leave empty for Auto: Collie stays inside the chosen provider and selects model + reasoning per task. The model pill opens a searchable picker.",
     "hint_zh": "可选的精确模型锁定。留空即 Auto：Collie 不跨 provider，按任务选择模型和推理强度。点顶栏模型标签可搜索并锁定模型。"},
    {"group": "Model", "key": "REASONING_EFFORT", "label": "Default reasoning effort",
     "label_zh": "默认推理强度", "type": "select", "default": "auto",
     "options": [
         {"value": "auto", "label": "Auto by task", "label_zh": "按任务自动"},
         {"value": "low", "label": "Low", "label_zh": "低"},
         {"value": "medium", "label": "Medium", "label_zh": "中"},
         {"value": "high", "label": "High", "label_zh": "高"}],
     "hint": "Reasoning depth for providers that support it. Auto resolves per run; unsupported models use their provider default and the receipt says so.",
     "hint_zh": "对支持该能力的模型设置推理深度。自动模式会逐任务决定；不支持时使用 provider 默认值，并在回执中说明。"},
    {"group": "Model", "key": "TEMPERATURE", "label": "Temperature", "type": "number", "default": "", "min": "0", "max": "1", "step": "0.1",
     "hint": "Sampling randomness. 0 = deterministic & repeatable (best for code); ~1 = more creative/varied. Leave empty to use the provider default (Claude ≈ 1.0)."},
    {"group": "Model", "key": "MAX_TOKENS", "label": "Max output tokens / turn", "type": "number", "default": "", "min": "0",
     "hint": "Ceiling on tokens the model may generate in one turn. Empty = provider default. Raise it for long files or big plans."},

    {"group": "Tools", "key": "WEBSEARCH", "label": "Web search", "type": "bool", "default": "off",
     "hint": "Let collie search the web (keyless engines / SearXNG). If the local-Chrome bridge below is live, real Chrome is used instead."},
    {"group": "Tools", "key": "BROWSER_BRIDGE", "label": "Use my local Chrome (bridge)", "type": "select", "default": "auto",
     "options": [
         {"value": "auto", "label": "Auto — use it whenever the extension is connected"},
         {"value": "1", "label": "Always on"},
         {"value": "0", "label": "Off"}],
     "hint": "Drive your REAL logged-in Chrome through the browser extension, so pages you're signed into (search, docs) just work. Auto is recommended."},
    {"group": "Tools", "key": "PLAN_FIRST", "label": "Plan before multi-file edits", "type": "bool", "default": "off",
     "hint": "On larger SWE tasks, write and commit a scope/plan before touching files. Slower but steadier on sprawling changes."},
    {"group": "Tools", "key": "MCP_MANAGE", "label": "Let Collie manage MCP servers", "label_zh": "允许管理 MCP 服务器", "type": "bool", "default": "off",
     "hint": "Let Collie add, re-enable and delete MCP servers itself — which means it can grant itself whatever tools those servers expose, under your credentials for remote ones. Off by default: Collie asks first and only proceeds if you agree. Reading the list and switching a server OFF never need this."},
    {"group": "Desktop", "key": "WALLPAPER", "label": "Ambient desktop at login", "label_zh": "登录时启动动态桌面", "type": "bool", "default": "off",
     "hint": "Run Collie's live wallpaper (clock, weather, music, an app dock, and a command bar) behind your desktop icons, started automatically when you log in. Turn OFF to remove the autostart and keep your normal wallpaper — Windows only."},
    {"group": "Desktop", "key": "DESKTOP_CONTROL", "label": "Control desktop apps", "label_zh": "控制桌面应用", "type": "bool", "default": "off",
     "hint": "Let Collie drive your native apps — click buttons, fill fields, launch apps, use menus — via Windows UI Automation or macOS System Events, in the background. Adds the desktop_* tools. Powerful, so off by default. Windows & macOS (macOS needs Accessibility permission)."},
    {"group": "Desktop", "key": "SCREEN_CAPTURE", "label": "Let Collie see the screen", "label_zh": "允许查看屏幕", "type": "bool", "default": "off",
     "hint": "Let Collie capture a window (even one behind others — no focus stealing) or the whole screen and actually LOOK at it, which is how it can judge whether a UI renders correctly. The image is sent to your configured model, along with anything else visible at the time, so this is separate from desktop control and off by default. Adds the screenshot tool. Windows & macOS (macOS needs Screen Recording permission)."},

    {"group": "Retrieval", "key": "EMBED", "label": "Embedder", "type": "select", "default": "auto",
     "options": [
         {"value": "auto", "label": "Auto (granite semantic → BM25 if unavailable)"},
         {"value": "granite", "label": "granite-107m (Apache, 55MB, multilingual — default)"},
         {"value": "bge-m3", "label": "bge-m3 (MIT, 2.2GB, best Chinese — quality)"},
         {"value": "e5", "label": "multilingual-e5-small (MIT, 118MB)"},
         {"value": "bm25", "label": "BM25 only (no model, keyword + fresh)"}],
     "hint": "Semantic model behind memory recall. Auto uses granite (in-process ONNX) and degrades to BM25 keyword retrieval when its deps/model are unavailable — never to hash (measured worse than BM25). Changing this needs a `collie mem reembed`."},
    {"group": "Retrieval", "key": "HF_ENDPOINT", "label": "Model download mirror", "type": "text", "default": "",
     "list": ["https://hf-mirror.com"],
     "hint": "Where embedding/reranker weights download from (Hugging Face URL format). Empty = "
             "huggingface.co with one automatic hf-mirror.com retry on failure; set it explicitly "
             "for an intranet mirror, or to https://hf-mirror.com if huggingface.co is blocked "
             "(mainland China).",
     "hint_zh": "向量/重排模型权重的下载源(Hugging Face 地址格式)。留空 = huggingface.co,失败自动"
                "用 hf-mirror.com 重试一次;内网镜像或大陆用户可显式填 https://hf-mirror.com。"},
    {"group": "Retrieval", "key": "RECENCY_HALFLIFE", "label": "Recency half-life (days)", "type": "number", "default": "90", "min": "0",
     "hint": "Newer memories get a mild retrieval boost that halves every N days — ports move and decisions get reversed, so fresh facts break ties. Relevance still dominates. 0 disables time weighting."},
    {"group": "Retrieval", "key": "RERANK", "label": "Reranker (cross-encoder)", "type": "bool", "default": "off",
     "hint": "Re-scores recall candidates jointly with the query for a sharper top-k. More accurate, a little slower per turn."},
    {"group": "Retrieval", "key": "DISTILL", "label": "Distill turns into memories", "type": "bool", "default": "off",
     "hint": "Summarize long turns into compact facts as you go, so future recall stays cheap and on-point."},

    {"group": "Autonomy", "key": "MISSION_APPROVAL_MODE", "label": "Mission autonomy",
     "label_zh": "Mission 自主模式", "type": "select", "default": "smart",
     "options": [
         {"value": "smart", "label": "Hands-off — interrupt only when needed",
          "label_zh": "放手执行 — 仅在确实需要我时打断"},
         {"value": "review", "label": "Review every external action",
          "label_zh": "逐项审阅外部操作"}],
     "hint": "The default for plain /mission. Hands-off lets Collie execute available actions "
             "inside the Mission leash without asking at every publish/send step. It still stops "
             "for credentials or identity that have not been connected, CAPTCHA/MFA that requires "
             "a person, new consent choices, new spending "
             "authority, scope expansion, and uncertain duplicate risk. Use /mission --review "
             "to override one Mission.",
     "hint_zh": "普通 /mission 的默认模式。放手执行会让 Collie 在 Mission Leash 范围内直接执行"
                "已有能力，不再每次发布/发送都询问；尚未连接的凭据或工作身份、必须由本人完成的"
                " CAPTCHA/MFA、新的同意选择、新增支出权限、扩大范围，以及结果不确定可能重复时"
                "仍会停下来。已连接并授权的邮箱、号码、验证码收件箱和登录态可直接使用。单次任务可用 "
                "/mission --review 覆盖。"},
    {"group": "Autonomy", "key": "AUTO_APPLY_PROFILE_CLAIMS",
     "label": "Use confirmed profile facts automatically", "label_zh": "自动使用已确认的个人事实",
     "type": "bool", "default": "off",
     "hint": "Let Hands-off Missions apply facts you explicitly saved (for example an age "
             "threshold) to matching low/medium-risk forms. This does not authorize CAPTCHA, "
             "person-required MFA, biometric/KYC, legal signatures, payments, or a different claim.",
     "hint_zh": "允许放手执行的 Mission 把你明确保存的事实（例如年龄门槛）用于匹配的低/中风险表单。"
                "这不会授权 CAPTCHA、明确要求本人的 MFA、生物识别/KYC、法律签名、付款或不同的声明。"},
    {"group": "Autonomy", "key": "MAX_AUTO_AUTH_RISK",
     "label": "Maximum automatic authorization risk", "label_zh": "自动授权最高风险",
     "type": "select", "default": "medium",
     "options": [
         {"value": "low", "label": "Low only", "label_zh": "仅低风险"},
         {"value": "medium", "label": "Low and medium", "label_zh": "低风险和中风险"}],
     "hint": "The ceiling for delegable standing authorizations. High/critical identity, legal, "
             "security and spending boundaries remain Needs You even in Hands-off mode.",
     "hint_zh": "可委托长期授权的风险上限。即使在放手执行模式，高/严重级身份、法律、安全和支出边界"
                "仍进入 Needs You。"},
    {"group": "Autonomy", "key": "DEFER_MISSING_AUTHORIZATIONS",
     "label": "Keep working while authorization waits", "label_zh": "等待授权时继续其他工作",
     "type": "bool", "default": "on",
     "hint": "Put a missing authorization in Needs You and continue independent Mission work. "
             "The whole Mission pauses only when every remaining path depends on it.",
     "hint_zh": "把缺失授权放入 Needs You，同时继续不依赖它的 Mission 工作；只有所有剩余路径都依赖"
                "该授权时，整个 Mission 才暂停。"},

    {"group": "Limits", "key": "MAX_TURNS", "label": "Max turns", "type": "number", "default": "50", "min": "1", "max": "120",
     "hint": "Hard cap on tool/response turns for one message before collie stops and reports back. Info-hunt + build tasks routinely need 20-30; subscription routes still have plan limits and may have separate billing rules."},
    {"group": "Limits", "key": "MAX_COST", "label": "Budget: stop past $", "type": "number", "default": "0", "min": "0", "step": "0.01",
     "hint": "Abort a run once metered spend crosses this many dollars. 0 = no budget cap. (Subscription providers cost $0 regardless.)"},
    {"group": "Limits", "key": "MAX_TOTAL_TOKENS", "label": "Budget: stop past tokens", "type": "number", "default": "0", "min": "0",
     "hint": "Abort a run once total tokens (in+out) cross this number. 0 = no token cap."},

    {"group": "Privacy", "key": "REDACT_SECRETS", "label": "Redact secrets from model input", "type": "bool", "default": "on",
     "hint": "API keys, tokens and private-key blocks found in tool output are replaced with {{SECRET:…}} placeholders before being sent to ANY cloud provider; tools substitute the real value back at execution time, so key-using workflows (deploys, curl auth) still run. Turn off only if a task truly needs the model to see raw secret text."},

    {"group": "Reliability", "key": "RETRIES", "label": "Transient-error retries", "type": "number", "default": "3", "min": "0", "max": "10",
     "hint": "How many times to retry a failed API call (rate limits, 5xx, dropped streams) before giving up."},
    {"group": "Reliability", "key": "RETRY_BASE", "label": "Retry backoff base (s)", "type": "number", "default": "2", "min": "0", "step": "0.5",
     "hint": "Base seconds for exponential backoff between retries (2 → ~2s, 4s, 8s …)."},
    {"group": "Reliability", "key": "OVERFLOW_RECOVERY", "label": "Recover from context overflow", "type": "bool", "default": "on",
     "hint": "When the context window fills, auto-compact and retry the turn instead of erroring out. Recommended on."},

    {"group": "Skills", "key": "SKILL_DIRS", "label": "Extra skill dirs", "type": "text", "default": "",
     "hint": "Colon-separated folders of custom skills to load in addition to the built-ins (e.g. /home/me/skills:/team/skills)."},

    {"group": "Remote", "key": "REMOTE", "label": "Phone remote access", "label_zh": "手机远程访问", "type": "bool", "default": "off",
     "hint": "Let your phone drive this Collie from anywhere, via the relay. When on, remote starts automatically whenever Collie's web server runs — manage paired devices on the /remote panel. Off cuts all remote access.",
     "hint_zh": "让手机在任何地方通过 relay 控制这台 Collie。开启后，每次 Collie 的 web 服务启动都会自动开远程；在 /remote 面板管理已配对设备。关闭即切断所有远程访问。"},
]
# ---- panel localization (zh) ----------------------------------------------------------------
# label/hint translations applied onto SCHEMA at import; the GUI picks label_zh/hint_zh when the
# resolved language is zh and falls back to English for anything missing. Inline label_zh on an
# entry (e.g. LANG/PROVIDER above) wins over this table.
_ZH = {
    "PROVIDER": {"label": "模型提供方",
                 "hint": "补全来自哪里。☁ = 第三方云:你的提示词、代码片段和工具输出会按该厂商的数据政策发送给它(密钥只从对应环境变量读取,collie 不存储;下方的密钥脱敏会把凭据挡在任何厂商可见内容之外)。订阅路由仍受套餐限额和各自计费政策约束;Ollama/mock 完全本地。",
                 "options": {"anthropic": "Anthropic API(API key,按量计费)",
                             "claude-agent-sdk": "Claude Agent SDK(你的 Claude 套餐,Collie harness)",
                             "anthropic-oauth": "Claude 直连(实验性,计费未验证)",
                             "claude-cli": "Claude CLI(你已登录的 CLI)",
                             "ollama": "Ollama(本地模型 — 数据不出本机)",
                             "openai-compat": "OpenAI 兼容端点",
                             "mock": "Mock(离线示例 — 仅测试)"}},
    "MODEL": {"label": "模型", "hint": "可选的精确模型锁定。留空即 Auto:Collie 不跨 provider,按任务选择模型和推理强度。"},
    "TEMPERATURE": {"label": "采样温度", "hint": "随机性。0 = 确定且可复现(适合代码);≈1 更发散。留空用提供方默认(Claude ≈ 1.0)。"},
    "MAX_TOKENS": {"label": "单轮最大输出 tokens", "hint": "模型单轮可生成的 token 上限。留空 = 提供方默认;长文件/大计划可调高。"},
    "WEBSEARCH": {"label": "网页搜索", "hint": "允许 collie 搜网(免密引擎/SearXNG)。若下方本地 Chrome 桥在线,则优先用真 Chrome。"},
    "BROWSER_BRIDGE": {"label": "用我的本地 Chrome(扩展桥)",
                       "hint": "通过浏览器扩展驱动你真实登录的 Chrome,登录态页面(搜索、文档)直接可用。推荐 Auto。",
                       "options": {"auto": "自动 — 扩展在线就用", "1": "总是开", "0": "关"}},
    "PLAN_FIRST": {"label": "多文件编辑前先计划", "hint": "大型任务先写好范围/计划再动文件。更慢但在牵连面大的改动上更稳。"},
    "MCP_MANAGE": {"label": "允许管理 MCP 服务器", "hint": "让 Collie 自己增删、重新启用 MCP 服务器——也就是它能给自己接上这些服务器提供的工具,远程服务器还会用到你的凭据。默认关:Collie 会先问你,你同意了才动手。查看列表和把某个服务器关掉不需要这个权限。"},
    "WALLPAPER": {"label": "登录时启动动态桌面", "hint": "把 Collie 的动态壁纸(时钟、天气、音乐、应用坞、命令栏)贴在桌面图标背后,开机自动启动。关掉就移除自启、恢复你原来的壁纸——仅 Windows。"},
    "DESKTOP_CONTROL": {"label": "控制桌面应用", "hint": "让 Collie 驱动你的原生应用——点按钮、填输入框、启动应用、用菜单——Windows 走 UI Automation,macOS 走 System Events,后台执行。会加上 desktop_* 工具。很强,默认关。Windows 和 macOS 都支持(macOS 需授予辅助功能权限)。"},
    "EMBED": {"label": "语义模型",
              "hint": "记忆召回背后的语义模型。auto 用 granite(进程内 ONNX),依赖/模型不可用时降级为 BM25 关键词召回——绝不退回 hash(实测比 BM25 还差)。改动后需要 `collie mem reembed`。",
              "options": {"auto": "自动(granite 语义 → 不可用则 BM25)", "granite": "granite-107m(Apache,55MB,多语言 — 默认)",
                          "bge-m3": "bge-m3(MIT,2.2GB,最强中文 — 质量)", "e5": "multilingual-e5-small(MIT,118MB)",
                          "bm25": "仅 BM25(无模型,关键词 + 始终新鲜)"}},
    "RECENCY_HALFLIFE": {"label": "时效半衰期(天)", "hint": "新记忆有轻度加权,每 N 天减半——端口会换、决定会翻,新事实用来破平。相关性仍占主导。0 = 关闭时间加权。"},
    "RERANK": {"label": "重排器(cross-encoder)", "hint": "召回候选与查询联合重打分,top-k 更准。更精确,每轮略慢。"},
    "DISTILL": {"label": "把对话蒸馏成记忆", "hint": "边跑边把长轮次总结为紧凑事实,未来召回更便宜更准。"},
    "MAX_TURNS": {"label": "最大轮数", "hint": "单条消息的工具/回复轮数硬上限。信息搜寻+构建类任务常要 20-30;订阅计费下多轮 $0,调高是安全的。"},
    "MAX_COST": {"label": "预算:超过 $ 即停", "hint": "按量计费花费越线即中止。0 = 不设上限。(订阅提供方恒为 $0。)"},
    "MAX_TOTAL_TOKENS": {"label": "预算:超过 tokens 即停", "hint": "总 tokens(入+出)越线即中止。0 = 不设上限。"},
    "REDACT_SECRETS": {"label": "向模型输入脱敏密钥", "hint": "工具输出中发现的 API key、token、私钥块在发给任何云厂商前替换为 {{SECRET:…}} 占位符;工具执行时替换回真值,部署/curl 鉴权等流程不受影响。仅当任务确实需要模型看到明文密钥时才关。"},
    "RETRIES": {"label": "瞬时错误重试次数", "hint": "API 调用失败(限流、5xx、断流)时的重试次数。"},
    "RETRY_BASE": {"label": "重试退避基数(秒)", "hint": "指数退避的基数(2 → 约 2s、4s、8s…)。"},
    "OVERFLOW_RECOVERY": {"label": "上下文溢出自动恢复", "hint": "上下文占满时自动压缩并重试该轮,而不是直接报错。建议开。"},
    "SKILL_DIRS": {"label": "额外 skill 目录", "hint": "冒号分隔的自定义 skill 目录,在内置之外加载(如 /home/me/skills:/team/skills)。"},
}
# group headers, for the panel
GROUPS_ZH = {"Identity": "身份", "General": "通用", "Model": "模型", "Tools": "工具", "Desktop": "桌面", "Remote": "远程",
             "Retrieval": "检索", "Autonomy": "自主", "Limits": "限额", "Privacy": "隐私", "Reliability": "可靠性", "Skills": "技能"}
for _s in SCHEMA:
    _t = _ZH.get(_s["key"])
    if not _t:
        continue
    _s.setdefault("label_zh", _t.get("label"))
    if _t.get("hint"):
        _s.setdefault("hint_zh", _t["hint"])
    if _t.get("options") and isinstance(_s.get("options"), list):
        for _o in _s["options"]:
            if isinstance(_o, dict) and _o.get("value") in _t["options"]:
                _o.setdefault("label_zh", _t["options"][_o["value"]])

_KEYS = {s["key"] for s in SCHEMA}


def normalize_companion_name(value, allow_empty=True) -> str:
    """Return one safe, human display name or raise ``ValueError``.

    Names are data, never markup.  NFC makes the same visible Unicode spelling stable for avatar
    hashing; whitespace is collapsed so a rename cannot create a visually different identity with
    invisible padding.  Formatting/control characters (including bidi overrides) and angle
    brackets are rejected instead of silently rewritten: accepting them would make audit rows and
    device switchers ambiguous even when every HTML call site escapes correctly.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("companion name must be text")
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not name:
        if allow_empty:
            return ""
        raise ValueError("companion name is required")
    if len(name) > 32:
        raise ValueError("companion name must be 32 characters or fewer")
    if "<" in name or ">" in name or any(unicodedata.category(ch).startswith("C") for ch in name):
        raise ValueError("companion name contains unsupported characters")
    return name


def _load():
    """settings.json, mtime-cached so a Settings-panel save takes effect on the next get().

    A failed read KEEPS the last good values and forces a re-read next call. Both halves were
    wrong before, and together they LATCHED: any transient failure — the panel's atomic save
    racing a reader, a scanner holding the file for a moment — blanked the cache, while the
    cached mtime was left at the last good value. The very next call therefore saw an unchanged
    mtime, skipped the reload, and served {} for the rest of the process's life. apply() then
    popped every COLLIE_<KEY> it had injected, and a running `collie web` fell to the mock
    provider mid-conversation: seven real turns, then canned "Based on the tool output:"
    fixtures, with settings.json on disk correct the whole time and the panel still reporting
    the right values. A MISSING file is a different thing — nothing is saved, and {} is the
    honest answer to that.
    """
    try:
        mt = os.path.getmtime(_PATH)
        if mt != _cache["mtime"]:
            with open(_PATH, encoding="utf-8") as f:
                _cache["data"] = json.load(f) or {}
            _cache["mtime"] = mt
    except FileNotFoundError:
        _cache["data"] = {}            # nothing saved yet — a real answer, not a failure
        _cache["mtime"] = -1.0
    except (OSError, ValueError):
        _cache["mtime"] = -1.0         # transient/mid-write: retry next call, keep what we had
    return _cache["data"]


def get(key, default=None):
    """env COLLIE_<KEY>  >  settings.json[key]  >  default. Returns str or default."""
    env = os.environ.get("COLLIE_" + key)
    if env is not None and env != "":
        if key == "COMPANION_NAME":
            try:
                return normalize_companion_name(env, allow_empty=False)
            except ValueError:
                return default
        return env
    v = _load().get(key)
    if v is not None and v != "":
        if key == "COMPANION_NAME":
            try:
                return normalize_companion_name(v, allow_empty=False)
            except ValueError:
                return default
        return str(v)
    return default


def all_values():
    """Current effective value for every SCHEMA knob (for the panel to show + prefill)."""
    return {s["key"]: get(s["key"], s["default"]) for s in SCHEMA}


def pinned(key):
    """True when COLLIE_<KEY> was set before we started, so saving this knob cannot change anything.

    A hard-set env var winning over the panel is the right rule — but silently is not. A server
    started with COLLIE_PROVIDER=mock accepts every model the picker sends, writes it to
    settings.json, reports it back, and keeps answering from the canned provider: the picker looks
    broken and the replies look like the model is broken. Whoever renders a control for a knob asks
    this first, so the answer can be "something else is holding this" rather than nothing at all.
    """
    return ("COLLIE_" + key) in _HARD_ENV


def apply():
    """Inject saved settings into os.environ (as COLLIE_<KEY>) for keys the user did NOT hard-set
    via a real env var — so every existing os.environ.get('COLLIE_X') read picks up the Settings
    panel with zero call-site changes, while an explicit env override stays authoritative. Re-reads
    settings.json (mtime-cached) so a panel save takes effect on the next call. Call per web request
    / at CLI start."""
    data = _load()
    injected = []
    for s in SCHEMA:
        envk = "COLLIE_" + s["key"]
        if envk in _HARD_ENV:
            continue
        v = data.get(s["key"])
        if v is not None and v != "":
            os.environ[envk] = str(v)
            injected.append(envk)      # tell any child this came from the panel, not from the user
        else:
            # Clearing a setting in the panel must REVERT within a long-lived process, not linger until
            # restart — code that reads os.environ directly (COLLIE_MAX_TOKENS / _MAX_COST / force ratios)
            # kept a stale cap otherwise. Only drop env WE injected; a hard-set env stays (guarded above).
            os.environ.pop(envk, None)
    # Carried across a fork so a child can tell panel-injected vars from a real user override.
    os.environ[_INJECTED_ENV] = ",".join(injected)


def save(values: dict) -> dict:
    """Persist only known keys (ignore junk); empty string clears a key back to its default."""
    clean = {k: v for k, v in (values or {}).items() if k in _KEYS and v not in (None, "")}
    if "COMPANION_NAME" in clean:
        clean["COMPANION_NAME"] = normalize_companion_name(clean["COMPANION_NAME"], allow_empty=False)
    if "PROFILE_AGE_BAND" in clean and str(clean["PROFILE_AGE_BAND"]) not in {
            "unset", "16", "18", "21"}:
        raise ValueError("age eligibility must be unset, 16, 18, or 21")
    if "MAX_AUTO_AUTH_RISK" in clean and str(clean["MAX_AUTO_AUTH_RISK"]) not in {
            "low", "medium"}:
        raise ValueError("automatic authorization risk must be low or medium")
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (_PATH, os.getpid(), os.urandom(4).hex())   # unique per writer: a fixed
    with open(tmp, "w", encoding="utf-8") as f:                        # .tmp let concurrent panel saves
        json.dump(clean, f, indent=2)                                  # interleave (fixed in sessions.py)
    os.replace(tmp, _PATH)
    _cache["mtime"] = -1.0    # force reload next get()
    return clean


def _read_uncached() -> dict:
    """settings.json, read NOW, past the cache. Raises if it exists and will not parse.

    update() must not merge into a guess. A merge that starts from {} is a REPLACE, and that is
    exactly how a settings file holding LANG, PROVIDER, MODEL and WALLPAPER became one holding LANG
    alone: one unreadable read left the cache empty, and the next panel save — a language change —
    committed the emptiness to disk. The provider went with it, and the web server fell to the mock
    model mid-conversation.

    _load() no longer keeps emptiness, but a process whose FIRST read fails has no last-good values
    to keep, and that window is still wide enough to lose a file in. So the one path that can
    DELETE settings reads the file itself and lets a failure fail the save: loud, and the values
    are still on disk. The alternative was silent, and they were not.
    """
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}                     # nothing saved yet — the one honest empty


def update(partial: dict) -> dict:
    """MERGE known keys into the saved settings, unlike save() which replaces the whole file.
    Used by the model picker so switching (PROVIDER/MODEL) never clobbers LANG/tools/etc."""
    data = dict(_read_uncached())
    for k, v in (partial or {}).items():
        if k in _KEYS:
            data[k] = v
    return save(data)
