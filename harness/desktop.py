"""Collie ambient-desktop backend — the widgets on the live wallpaper.

Everything here is pure ctypes / PowerShell / .NET so it works inside the frozen embeddable
python with ZERO pip installs (winsdk / psutil / PIL are all absent there).

Config lives at ~/.collie/desktop.json and is the single source of truth for which widgets are
on and where they sit. It's plain JSON on purpose: Collie itself can edit it in response to
"put the clock bottom-left" / "add a music widget", and the wallpaper page polls it and
re-renders — so the desktop is agent-manageable, not hard-coded.
"""
import os, json, hashlib, subprocess, ctypes, time

HOME = os.path.expanduser("~")
COLLIE_DIR = os.path.join(HOME, ".collie")
CONFIG_PATH = os.path.join(COLLIE_DIR, "desktop.json")
ICON_DIR = os.path.join(COLLIE_DIR, "dock-icons")
_NOWIN = 0x08000000  # CREATE_NO_WINDOW — never flash a console

# ── config ──────────────────────────────────────────────────────────────────────────────────
# slots: tl tr bl br  (four corners) + center.  The input/composer is a fixed element, not a slot.
DEFAULT_CONFIG = {
    "widgets": {
        "brand":    {"on": True,  "slot": "center"},
        "clock":    {"on": True,  "slot": "tr"},
        "launcher": {"on": True,  "slot": "bl", "apps": []},   # apps auto-seeded on first load
        "music":    {"on": True,  "slot": "tr"},               # stacks under the clock, top-right
        "system":   {"on": False, "slot": "br"},               # CPU chip off by default
        "projects": {"on": False, "slot": "bl"},
    }
}


def _seed_apps():
    """A sensible starter dock: the user's real, present apps — never invent paths that 404."""
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Microsoft VS Code\Code.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Collie\Collie.exe"),
        r"C:\Windows\System32\WindowsTerminal.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WindowsApps\wt.exe"),
    ]
    out, seen = [], set()
    for p in cands:
        if p and os.path.exists(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append({"label": os.path.splitext(os.path.basename(p))[0], "path": p})
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f) or {}
        for k, v in (saved.get("widgets") or {}).items():
            cfg["widgets"].setdefault(k, {}).update(v)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # seed the dock once if empty so the launcher isn't blank on a fresh install
    la = cfg["widgets"].get("launcher", {})
    if not la.get("apps"):
        la["apps"] = _seed_apps()
    return cfg


def save_config(cfg):
    os.makedirs(COLLIE_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    return cfg


def config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


# ── launcher ────────────────────────────────────────────────────────────────────────────────
def launch(target):
    """Open an app path or a URL. Returns True on success."""
    if not target:
        return False
    try:
        if target.lower().startswith(("http://", "https://")):
            os.startfile(target)
        elif os.path.exists(target):
            os.startfile(target)
        else:
            return False
        return True
    except Exception:
        return False


def icon_png(path):
    """Extract an app's icon to a cached 48px PNG (via .NET); return the file path or None.
    Cheap after the first call — keyed by the exe path so it's extracted once."""
    if not path or not os.path.exists(path):
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    key = hashlib.md5(path.lower().encode("utf-8")).hexdigest()[:16]
    out = os.path.join(ICON_DIR, key + ".png")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Add-Type -AssemblyName System.Drawing;"
        "$i=[System.Drawing.Icon]::ExtractAssociatedIcon('%s');"
        "if($i){$b=$i.ToBitmap();$b.Save('%s',[System.Drawing.Imaging.ImageFormat]::Png)}"
        % (path.replace("'", "''"), out.replace("'", "''"))
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       creationflags=_NOWIN, timeout=8,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return out if os.path.exists(out) and os.path.getsize(out) > 0 else None


# ── media control ───────────────────────────────────────────────────────────────────────────
_VK = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
       "mute": 0xAD, "volup": 0xAF, "voldown": 0xAE}


def media(cmd):
    """Send a global media key — controls Spotify / YouTube / any player, no focus needed."""
    vk = _VK.get(cmd)
    if not vk:
        return False
    try:
        u = ctypes.windll.user32
        u.keybd_event(vk, 0, 1, 0)      # KEYEVENTF_EXTENDEDKEY
        u.keybd_event(vk, 0, 1 | 2, 0)  # + KEYEVENTF_KEYUP
        return True
    except Exception:
        return False


_NP_CACHE = {"t": 0.0, "v": None}


def nowplaying():
    """Best-effort current track via the Windows media session (GSMTC), through PowerShell WinRT.
    Fragile + slowish, so cache ~3s. Returns {title,artist,app,playing} or None (widget then shows
    just the transport controls)."""
    now = time.time()
    if now - _NP_CACHE["t"] < 3.0:
        return _NP_CACHE["v"]
    _NP_CACHE["t"] = now
    ps = r'''
$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
function AW($op,$t){ $m=[System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'GetAwaiter' -and $_.GetParameters().Count -eq 1}|select -First 1
  $g=$m.MakeGenericMethod($t).Invoke($null,@($op)); while(-not $g.IsCompleted){Start-Sleep -Milliseconds 20}; $g.GetResult() }
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]|Out-Null
$mgr=AW ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$s=$mgr.GetCurrentSession()
if($s){ $p=AW ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
  $pb=$s.GetPlaybackInfo(); $st=[int]$pb.PlaybackStatus
  $o=[ordered]@{title=$p.Title;artist=$p.Artist;app=$s.SourceAppUserModelId;playing=($st -eq 4)}
  $o|ConvertTo-Json -Compress }
'''
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           creationflags=_NOWIN, timeout=6,
                           capture_output=True, text=True)
        out = (r.stdout or "").strip()
        v = json.loads(out) if out.startswith("{") else None
        if v and not (v.get("title") or v.get("artist")):
            v = None
        _NP_CACHE["v"] = v
        return v
    except Exception:
        _NP_CACHE["v"] = None
        return None


# ── system glance ───────────────────────────────────────────────────────────────────────────
class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("Reserved1", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def _ft(ft):
    return (ft.high << 32) | ft.low


_CPU_PREV = {"idle": 0, "busy": 0}


def _cpu_percent():
    """CPU load from GetSystemTimes deltas between calls — no sleep, no psutil."""
    idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        return None
    i, k, u = _ft(idle), _ft(kern), _ft(user)
    busy = (k + u) - i          # kernel includes idle; busy = total - idle
    total = k + u
    di = i - _CPU_PREV["idle"]
    dt = total - _CPU_PREV["busy"]
    _CPU_PREV["idle"], _CPU_PREV["busy"] = i, total
    if dt <= 0:
        return None             # first sample / no delta yet
    return max(0, min(100, round((1 - di / dt) * 100)))


def sysinfo():
    out = {}
    try:
        st = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            pct = st.BatteryLifePercent
            out["battery"] = None if pct == 255 else int(pct)
            out["charging"] = (st.ACLineStatus == 1)
            out["has_battery"] = not (st.BatteryFlag & 128)   # 128 = no system battery
    except Exception:
        pass
    try:
        c = _cpu_percent()
        if c is not None:
            out["cpu"] = c
    except Exception:
        pass
    return out


# ── projects ────────────────────────────────────────────────────────────────────────────────
def projects(limit=8):
    """Reuse Collie's repo discovery for a quick-open list."""
    try:
        from . import codemap
        repos = codemap.discover_repos(HOME) or []
    except Exception:
        repos = []
    junk = (os.path.join(HOME, "AppData").lower(), (os.environ.get("TEMP", "") or "").lower(),
            os.path.join(HOME, "AppData", "Local", "Temp").lower())
    out = []
    for r in repos:
        root = r.get("root") if isinstance(r, dict) else getattr(r, "root", None)
        if not root:
            continue
        low = root.lower()
        if any(j and low.startswith(j) for j in junk) or "\\temp\\" in low:
            continue          # skip throwaway git dirs under Temp/AppData
        out.append({"name": os.path.basename(root.rstrip("/\\")), "root": root})
        if len(out) >= limit:
            break
    return out


# ── music playback ──────────────────────────────────────────────────────────────────────────
# "放点 lofi" should just PLAY — not make the coding agent hedge. The desktop composer routes a
# music-intent straight here: known moods open a stable autoplaying stream, anything else lands on
# a YouTube Music search for the cleaned query.
# mood → a good YouTube SEARCH phrase (resolved live, so no dead ids). Anything not listed searches verbatim.
_MOODS = [
    (("lofi", "lo-fi", "lo fi", "chill beat", "study beat"), "lofi hip hop radio"),
    (("focus", "study", "专注", "concentration"),            "focus music concentration"),
    (("sleep", "睡", "白噪", "ambient"),                      "ambient sleep music"),
    (("rain", "雨声", "雨"),                                  "rain sounds for sleeping"),
    (("jazz", "爵士"),                                        "relaxing jazz music"),
    (("piano", "钢琴"),                                       "relaxing piano music"),
    (("classical", "古典"),                                   "classical music"),
]
_PLAY_VERBS = ("帮我", "放点", "放首", "放一首", "放一点", "来点", "来首", "播放", "放", "put on", "play some", "play")


_YTDLP = os.path.join(COLLIE_DIR, "yt-dlp.exe")


def _ensure_ytdlp():
    """yt-dlp.exe lives in ~/.collie; fetch the standalone build once (~18MB) if missing."""
    if os.path.exists(_YTDLP) and os.path.getsize(_YTDLP) > 1_000_000:
        return _YTDLP
    os.makedirs(COLLIE_DIR, exist_ok=True)
    import urllib.request
    url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    tmp = _YTDLP + ".part"
    try:
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, _YTDLP)
        return _YTDLP
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


# titles that are clearly NOT a song — reaction/gossip/podcast/etc. Down-rank hard.
_NOT_MUSIC = ("exposed", "expose", "drama", "reaction", "react", "review", "interview", "podcast",
              "news", "explained", "documentary", "trailer", "gameplay", "tutorial", "vlog",
              "commentary", "story time", "storytime", "tier list", "ranking", "breakdown",
              "analysis", "recap", "highlights", "compilation of", "top 10", "top 20")


# source registry — YouTube worldwide, Bilibili for mainland China (both via yt-dlp).
_SEARCH = {"youtube": "ytsearch", "bilibili": "bilisearch"}
_WATCH = {"youtube": "https://www.youtube.com/watch?v=%s", "bilibili": "https://www.bilibili.com/video/%s"}


def _pick_song(exe, terms, source, exclude=()):
    """Flat-search several candidates and pick the most song-like target URL (fast, metadata only).
    Skips any id in `exclude` (used by autoplay-next so it never repeats a track)."""
    pref = _SEARCH.get(source, "ytsearch")
    try:
        r = subprocess.run([exe, "-J", "--flat-playlist", pref + "8:" + terms],
                           creationflags=_NOWIN, timeout=25, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
        entries = (json.loads(r.stdout or "{}").get("entries")) or []
    except Exception:
        return None
    exclude = set(exclude or ())
    def score(e):
        t = (e.get("title") or "").lower()
        ch = (e.get("channel") or e.get("uploader") or "").lower()
        d = e.get("duration") or 0; s = 0
        if any(b in t for b in _NOT_MUSIC): s -= 100
        if 45 <= d <= 720: s += 12
        elif d > 1800: s -= 40
        if "topic" in ch: s += 22                       # YouTube auto-generated Topic = clean album audio
        if "audio" in t: s += 9
        if any(k in t for k in ("music video", "official video", "m/v", " mv", "live", "performance",
                                "cover", "remix", "sped up", "slowed", "8d", "nightcore")): s -= 10
        return s
    cands = [e for e in entries if e.get("id") and e.get("id") not in exclude]
    if not cands:
        return None
    cands.sort(key=score, reverse=True)
    best = cands[0]
    return best.get("url") or (_WATCH.get(source, _WATCH["youtube"]) % best["id"])


def _extract_one(exe, terms, source, exclude=()):
    """Pick + extract a direct audio URL from ONE source. Returns the yt-dlp info dict or None."""
    target = _pick_song(exe, terms, source, exclude) or (_SEARCH.get(source, "ytsearch") + "1:" + terms)
    try:
        r = subprocess.run(
            [exe, "-j", "-f", "bestaudio[acodec!=none]/bestaudio/best", "--no-playlist", target],
            creationflags=_NOWIN, timeout=45, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        line = (r.stdout or "").strip().splitlines()
        return json.loads(line[0]) if line else None
    except Exception:
        return None


def resolve_audio(query, artist="", title="", region="", exclude=()):
    """Search (music-biased) + extract a DIRECT audio stream URL. Region-aware: mainland China prefers
    Bilibili (YouTube is blocked there), elsewhere YouTube — and either falls back to the other.
    `exclude` = ids already played (autoplay-next skips them)."""
    exe = _ensure_ytdlp()
    if not exe:
        return {"ok": False, "error": "yt-dlp unavailable"}
    terms = ((artist + " " + title).strip() if title else _clean_terms(query))
    order = ["bilibili", "youtube"] if (region or "").upper() == "CN" else ["youtube", "bilibili"]
    for source in order:
        d = _extract_one(exe, terms, source, exclude)
        if not d:
            continue
        url = d.get("url")
        if not url:
            for f in reversed(d.get("formats") or []):
                if f.get("acodec") not in (None, "none") and f.get("url"):
                    url = f["url"]; break
        if url:
            return {"ok": True, "url": url, "title": d.get("title"), "uploader": d.get("uploader"),
                    "duration": d.get("duration"), "id": d.get("id"), "thumb": d.get("thumbnail"),
                    "terms": terms, "artist": artist, "songTitle": title, "source": source}
    return {"ok": False, "error": "no playable source", "terms": terms}


def _lyric_queries(title, artist):
    """Build a few ordered lrclib search phrases from a messy YouTube title, best guess first."""
    import re
    def strip_suffix(s):
        s = re.sub(r"(?i)\b(official\s*(music\s*)?video|official\s*audio|lyric[s]?\s*video|m/?v|hd|4k|"
                   r"full\s*version|official|feat\.?|ft\.?)\b", " ", s or "")
        return re.sub(r"\s+", " ", re.sub(r"[\[\]【】()（）「」『』\-–—_|/]", " ", s)).strip()
    def cjk_head(s):                                   # "晴天 Sunny Day" -> "晴天"; keep if it has CJK
        m = re.match(r"\s*([㐀-鿿぀-ヿ가-힣][^A-Za-z]*)", s or "")
        return (m.group(1).strip() if m else "")
    brs = re.findall(r"[【\[「『（(]([^】\]」』）)]+)[】\]」』）)]", title or "")   # song often in 【…】
    song = cjk_head(brs[0]) if brs else ""
    art = re.sub(r"(?i)\b(vevo|official|topic|channel|music)\b", " ", artist or "").strip()
    art = cjk_head(art) or art.split()[0] if art else ""
    cands = []
    if art and song: cands.append(art + " " + song)                # artist + song  (most precise)
    if song: cands.append(song)
    if brs: cands.append(strip_suffix(brs[0]))                     # full bracket content
    cands.append(strip_suffix(title))                              # whole cleaned title
    if art: cands.append(art + " " + strip_suffix(title))
    seen, out = set(), []
    for c in cands:
        c = re.sub(r"\s+", " ", c or "").strip()
        if c and c.lower() not in seen:
            seen.add(c.lower()); out.append(c)
    return out[:6]


def _tokens(s):
    import re
    return set(re.findall(r"[0-9a-z]{2,}|[一-鿿぀-ヿ가-힣]{2,}", (s or "").lower()))


def _parse_lrc(synced):
    """Parse LRC → [{t,text}]. Handles multiple timestamps per line and strips ALL leading tags."""
    import re
    out = []
    for raw in (synced or "").splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        text = re.sub(r"\[\d+:\d+(?:\.\d+)?\]", "", raw).strip()   # remove every [mm:ss] tag from text
        for mm, ss in tags:
            out.append({"t": round(int(mm) * 60 + float(ss), 2), "text": text})
    out.sort(key=lambda x: x["t"])
    return out


def _lrclib_get(artist, title, duration):
    """lrclib EXACT lookup by artist+track(+duration). The most accurate path — no fuzzy guessing."""
    import urllib.request, urllib.parse
    try:
        dur = int(float(duration or 0))
    except (TypeError, ValueError):
        dur = 0
    attempts = []
    if dur:
        attempts.append({"artist_name": artist, "track_name": title, "duration": str(dur)})
    attempts.append({"artist_name": artist, "track_name": title})     # no-duration fallback
    for p in attempts:
        try:
            u = "https://lrclib.net/api/get?" + urllib.parse.urlencode(p)
            req = urllib.request.Request(u, headers={"User-Agent": "collie-desktop/1.0"})
            hit = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
        except Exception:
            continue
        if hit and hit.get("syncedLyrics"):
            lines = _parse_lrc(hit["syncedLyrics"])
            if lines:
                return {"ok": True, "lines": lines, "exact": True, "trackName": hit.get("trackName"),
                        "artistName": hit.get("artistName"), "lrcDuration": hit.get("duration"),
                        "audioDuration": dur}
    return None


def lyrics(query, artist="", duration=0, title=""):
    """Timestamped lyrics from lrclib.net → [{t, text}] for karaoke sync. Tries several phrases from
    the (messy) title + artist; a hit must SHARE a token with the query; and among matches we pick the
    one whose DURATION is closest to the playing audio — so the timeline actually lines up (a YT MV vs
    the album cut can differ by many seconds, which is what makes lyrics drift)."""
    import urllib.request, urllib.parse
    # BEST path: exact structured lookup when the LLM gave us artist + song title
    if artist and title:
        exact = _lrclib_get(artist, title, duration)
        if exact:
            return exact
    want = _tokens((query or "") + " " + (title or "") + " " + (artist or ""))
    try:
        target = float(duration or 0)
    except (TypeError, ValueError):
        target = 0.0
    for q in _lyric_queries(title or query, artist):
        try:
            req = urllib.request.Request("https://lrclib.net/api/search?q=" + urllib.parse.quote(q),
                                         headers={"User-Agent": "collie-desktop/1.0"})
            arr = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        except Exception:
            continue
        valid = []
        for hit in (arr or []):
            if not hit.get("syncedLyrics"):
                continue
            # the SONG name must match — share a token with the trackName, not merely the artist
            # (else every 周杰伦 song "matches" 周杰伦 and duration-sort grabs the wrong one).
            song = _tokens(hit.get("trackName") or "")
            if want and song and not (want & song):
                continue
            valid.append(hit)
        if not valid:
            continue
        if target:
            valid.sort(key=lambda h: abs((h.get("duration") or 0) - target))   # closest version first
        hit = valid[0]
        lines = _parse_lrc(hit["syncedLyrics"])
        if lines:
            return {"ok": True, "lines": lines, "query": q, "trackName": hit.get("trackName"),
                    "artistName": hit.get("artistName"), "lrcDuration": hit.get("duration"), "audioDuration": target}
    return {"ok": False}


def _clean_terms(query):
    """Strip a leading play-verb and map a mood to a good search phrase."""
    q = (query or "").strip()
    for v in _PLAY_VERBS:
        if q.lower().startswith(v):
            q = q[len(v):].strip(" ，,、:：的"); break
    ql = q.lower()
    for keys, phrase in _MOODS:
        if any(k in ql for k in keys):
            return phrase
    return q or "lofi hip hop radio"


# ── music INTENT (fast LLM, reusing collie's front-door router provider) ─────────────────────
# Regex can't cover an open set of genres/artists/songs ("放点rap", "放点周杰伦"). So a cheap model
# decides, exactly like harness/router.py's classifier — just a tiny music-or-not head.
_MUSIC_SYS = (
    "You are a desktop command classifier. Decide if the user's message is a request to PLAY MUSIC "
    "and, if so, extract structured fields.\n"
    "PLAY MUSIC = start playing a song / genre / artist / playlist / radio / mood "
    "(e.g. '放点rap', '放点周杰伦', 'play some jazz', 'put on Taylor Swift', '来点钢琴曲', 'lofi', "
    "'我想听点轻松的音乐'). NOT music = questions, coding, opening apps, or anything else "
    "('放大字体', 'play the test suite', \"what's the weather\", '打开 VS Code').\n"
    "Fields (keep the user's own language for the names):\n"
    "- query: music search terms with the play-verb removed (always fill for music).\n"
    "- artist: the performer, IF a specific one is named, else empty.\n"
    "- title: the specific SONG name, IF one is named, else empty (empty for genre/mood/artist-only).\n"
    "Examples: '放点周杰伦稻香'→{music:true,query:'周杰伦 稻香',artist:'周杰伦',title:'稻香'}; "
    "'play taylor swift cruel summer'→{artist:'Taylor Swift',title:'Cruel Summer'}; "
    "'放点爵士'→{music:true,query:'爵士',artist:'',title:''}.\n"
    'Reply with STRICT JSON only: {"music": true|false, "query": "<terms>", "artist": "<name>", "title": "<song>"}')


def _router_provider():
    from . import settings
    settings.apply()
    from .providers import make_provider
    from .router import DEFAULT_ROUTER_MODEL
    name = settings.get("PROVIDER") or os.environ.get("COLLIE_PROVIDER", "mock")
    rmodel = os.environ.get("COLLIE_ROUTER_MODEL") or (
        DEFAULT_ROUTER_MODEL if name in ("anthropic-oauth", "anthropic") else None)
    return make_provider(name, rmodel)


def _json_obj(txt):
    import re
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(0)); return o if isinstance(o, dict) else None
    except Exception:
        return None


def music_intent(text):
    """{'music': bool, 'query': str} via a fast model — the open-set intent judgment regex can't do."""
    text = (text or "").strip()
    if not text:
        return {"music": False, "query": ""}
    try:
        comp = _router_provider().complete(_MUSIC_SYS, [{"role": "user", "content": text[:600]}], [])
        obj = _json_obj(getattr(comp, "text", "") or "")
        if obj and obj.get("music"):
            return {"music": True, "query": (obj.get("query") or text).strip(),
                    "artist": (obj.get("artist") or "").strip(), "title": (obj.get("title") or "").strip()}
        return {"music": False, "query": ""}
    except Exception as e:
        return {"music": False, "query": "", "error": str(e)}


def resolve(query):
    """Search YouTube for the request and return the top video id — so playback happens IN the
    wallpaper page (no browser popup, no dead hard-coded ids). No API key: parse the results HTML."""
    import urllib.request, urllib.parse, re
    terms = _clean_terms(query)
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(terms) + "&sp=EgIQAQ%253D%253D"  # sp = videos only
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore")
        seen, ids = set(), []
        for v in re.findall(r'"videoId":"([\w-]{11})"', html):   # several candidates: skip embed-blocked ones
            if v not in seen:
                seen.add(v); ids.append(v)
            if len(ids) >= 8:
                break
        if ids:
            return {"ok": True, "videoId": ids[0], "videoIds": ids, "terms": terms}
    except Exception:
        pass
    return {"ok": False, "terms": terms}


def open_project(root):
    """Open a repo in VS Code if available, else its folder."""
    if not root or not os.path.isdir(root):
        return False
    code = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Microsoft VS Code\Code.exe")
    try:
        if os.path.exists(code):
            subprocess.Popen([code, root], creationflags=_NOWIN)
        else:
            os.startfile(root)
        return True
    except Exception:
        return False
