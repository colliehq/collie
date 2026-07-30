"""User settings — one place to configure collie's many knobs, instead of remembering 35 env vars.

Layered precedence: an explicit env var (COLLIE_*) ALWAYS wins (so CLI/scripts stay authoritative),
then the saved settings.json (what the web Settings panel writes), then the code default. The web
GUI reads SCHEMA to render the panel and GET/POSTs the values; make_harness/_provider/_embedder read
`get()` so a saved setting takes effect on the next run with zero env fiddling.
"""
import json
import os
import time

_PATH = os.environ.get("COLLIE_SETTINGS_PATH") or os.path.expanduser("~/.collie/settings.json")
_cache = {"mtime": -1.0, "data": {}}
# env vars set BEFORE we ran are authoritative (a user's CLI `COLLIE_X=… collie …` must win over a
# saved panel value); apply() never overwrites these.
_HARD_ENV = {k for k in os.environ if k.startswith("COLLIE_")}


# Each knob: key (the settings.json field + the env var suffix COLLIE_<KEY>), label, type, default,
# and (for select/bool) options. Grouped for the panel. ONLY user-facing knobs — debug/internal
# env vars (COLLIE_DEBUG, COLLIE_RPC_PORT, COLLIE_SUBAGENT, …) are intentionally omitted.
# Types: select (options=[str] or [{value,label}]), text (optional list=[…] for a datalist of
# suggestions), number (optional min/max/step), bool (rendered as a toggle; stored "on"/"off").
# `hint` is the one-line help shown under the control — every knob gets one so nothing is a mystery.
SCHEMA = [
    # UI language: the web GUI chrome + this panel render in it. auto = follow the browser.
    # label_zh / hint_zh on any entry (and label_zh inside options) localize the panel — the GUI
    # picks them when the resolved language is zh; missing translations fall back to English.
    {"group": "General", "key": "LANG", "label": "Language", "label_zh": "界面语言", "type": "select",
     "default": "auto",
     "options": [
         {"value": "auto", "label": "Auto (follow browser)", "label_zh": "自动(跟随浏览器)"},
         {"value": "en", "label": "English"},
         {"value": "zh", "label": "简体中文"},
         {"value": "zh-tw", "label": "繁體中文"},
         {"value": "ja", "label": "日本語"},
         {"value": "ko", "label": "한국어"},
         {"value": "es", "label": "Español"},
         {"value": "fr", "label": "Français"},
         {"value": "de", "label": "Deutsch"},
         {"value": "pt", "label": "Português"},
         {"value": "ru", "label": "Русский"}],
     "hint": "Language of the web GUI. auto follows your browser's language.",
     "hint_zh": "Web 界面的显示语言。auto 跟随浏览器语言。"},
    # API key is the default; anthropic-oauth (Claude-Code header impersonation) is OPT-IN — it is
    # unsanctioned upstream (see CHANGELOG "BANNED"), so the user selects it deliberately, per run
    # or via this panel, never by silent default.
    {"group": "Model", "key": "PROVIDER", "label": "Provider", "label_zh": "模型提供方", "type": "select", "default": "anthropic",
     "options": [
         {"value": "anthropic", "label": "Anthropic API (API key, metered)"},
         {"value": "anthropic-oauth", "label": "Claude subscription (OAuth, $0/token)"},
         {"value": "codex-oauth", "label": "ChatGPT Codex subscription (OAuth, $0/token)"},
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
     "hint": "Where completions come from. ☁ = third-party cloud: your prompt, code excerpts and tool output are sent to that vendor under its data policy (keys are read from the named env var, never stored by collie; secret redaction below keeps credentials out of what any vendor sees). The two Claude-subscription options draw your flat plan; Ollama/mock stay fully local."},
    {"group": "Model", "key": "MODEL", "label": "Model", "type": "text", "default": "claude-opus-4-8",
     "list": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5",
              "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna",
              "gemini-2.5-pro", "gemini-2.5-flash", "gpt-4o-mini", "deepseek-chat", "deepseek-reasoner"],
     "hint": "Model id for the chosen provider. Tip: click the model pill (top bar) or type /model in chat for a searchable picker with live model discovery, auth badges and prices. Leave empty for the provider's default.",
     "hint_zh": "所选 provider 的模型 id。提示:点顶栏的模型标签、或在对话里输入 /model,可打开可搜索的选择器(实时发现模型 + 授权状态 + 价格)。留空用该 provider 的默认。"},
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
    {"group": "Desktop", "key": "WALLPAPER", "label": "Ambient desktop at login", "label_zh": "登录时启动靠近桌面", "type": "bool", "default": "off",
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

    {"group": "Limits", "key": "MAX_TURNS", "label": "Max turns", "type": "number", "default": "50", "min": "1", "max": "120",
     "hint": "Hard cap on tool/response turns for one message before collie stops and reports back. Info-hunt + build tasks routinely need 20-30; on a flat subscription extra turns cost $0, so high is safe."},
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
                 "hint": "补全来自哪里。☁ = 第三方云:你的提示词、代码片段和工具输出会按该厂商的数据政策发送给它(密钥只从对应环境变量读取,collie 不存储;下方的密钥脱敏会把凭据挡在任何厂商可见内容之外)。两个 Claude 订阅选项走包月;Ollama/mock 完全本地。",
                 "options": {"anthropic": "Anthropic API(API key,按量计费)",
                             "anthropic-oauth": "Claude 订阅(OAuth,$0/token)",
                             "claude-cli": "Claude CLI(你已登录的 CLI)",
                             "ollama": "Ollama(本地模型 — 数据不出本机)",
                             "openai-compat": "OpenAI 兼容端点",
                             "mock": "Mock(离线示例 — 仅测试)"}},
    "MODEL": {"label": "模型", "hint": "所选提供方的模型 id(如 claude-opus-4-8、gemini-2.5-flash、Ollama 标签)。留空用该提供方默认。"},
    "TEMPERATURE": {"label": "采样温度", "hint": "随机性。0 = 确定且可复现(适合代码);≈1 更发散。留空用提供方默认(Claude ≈ 1.0)。"},
    "MAX_TOKENS": {"label": "单轮最大输出 tokens", "hint": "模型单轮可生成的 token 上限。留空 = 提供方默认;长文件/大计划可调高。"},
    "WEBSEARCH": {"label": "网页搜索", "hint": "允许 collie 搜网(免密引擎/SearXNG)。若下方本地 Chrome 桥在线,则优先用真 Chrome。"},
    "BROWSER_BRIDGE": {"label": "用我的本地 Chrome(扩展桥)",
                       "hint": "通过浏览器扩展驱动你真实登录的 Chrome,登录态页面(搜索、文档)直接可用。推荐 Auto。",
                       "options": {"auto": "自动 — 扩展在线就用", "1": "总是开", "0": "关"}},
    "PLAN_FIRST": {"label": "多文件编辑前先计划", "hint": "大型任务先写好范围/计划再动文件。更慢但在牵连面大的改动上更稳。"},
    "MCP_MANAGE": {"label": "允许管理 MCP 服务器", "hint": "让 Collie 自己增删、重新启用 MCP 服务器——也就是它能给自己接上这些服务器提供的工具,远程服务器还会用到你的凭据。默认关:Collie 会先问你,你同意了才动手。查看列表和把某个服务器关掉不需要这个权限。"},
    "WALLPAPER": {"label": "登录时启动靠近桌面", "hint": "把 Collie 的动态壁纸(时钟、天气、音乐、应用坞、命令栏)贴在桌面图标背后,开机自动启动。关掉就移除自启、恢复你原来的壁纸——仅 Windows。"},
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
GROUPS_ZH = {"General": "通用", "Model": "模型", "Tools": "工具", "Desktop": "桌面", "Remote": "远程",
             "Retrieval": "检索", "Limits": "限额", "Privacy": "隐私", "Reliability": "可靠性", "Skills": "技能"}
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


def _load():
    """settings.json, mtime-cached so a Settings-panel save takes effect on the next get()."""
    try:
        mt = os.path.getmtime(_PATH)
        if mt != _cache["mtime"]:
            with open(_PATH, encoding="utf-8") as f:
                _cache["data"] = json.load(f) or {}
            _cache["mtime"] = mt
    except (OSError, ValueError):
        _cache["data"] = {}
    return _cache["data"]


def get(key, default=None):
    """env COLLIE_<KEY>  >  settings.json[key]  >  default. Returns str or default."""
    env = os.environ.get("COLLIE_" + key)
    if env is not None and env != "":
        return env
    v = _load().get(key)
    if v is not None and v != "":
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
    for s in SCHEMA:
        envk = "COLLIE_" + s["key"]
        if envk in _HARD_ENV:
            continue
        v = data.get(s["key"])
        if v is not None and v != "":
            os.environ[envk] = str(v)
        else:
            # Clearing a setting in the panel must REVERT within a long-lived process, not linger until
            # restart — code that reads os.environ directly (COLLIE_MAX_TOKENS / _MAX_COST / force ratios)
            # kept a stale cap otherwise. Only drop env WE injected; a hard-set env stays (guarded above).
            os.environ.pop(envk, None)


def save(values: dict) -> dict:
    """Persist only known keys (ignore junk); empty string clears a key back to its default."""
    clean = {k: v for k, v in (values or {}).items() if k in _KEYS and v not in (None, "")}
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (_PATH, os.getpid(), os.urandom(4).hex())   # unique per writer: a fixed
    with open(tmp, "w", encoding="utf-8") as f:                        # .tmp let concurrent panel saves
        json.dump(clean, f, indent=2)                                  # interleave (fixed in sessions.py)
    os.replace(tmp, _PATH)
    _cache["mtime"] = -1.0    # force reload next get()
    return clean


def update(partial: dict) -> dict:
    """MERGE known keys into the saved settings, unlike save() which replaces the whole file.
    Used by the model picker so switching (PROVIDER/MODEL) never clobbers LANG/tools/etc."""
    data = dict(_load())
    for k, v in (partial or {}).items():
        if k in _KEYS:
            data[k] = v
    return save(data)
