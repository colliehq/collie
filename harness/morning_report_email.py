"""The morning report as an email: the approved v4 phone design, made of tables and inline styles.

:func:`render` takes a saved report (see :mod:`morning_report`) and returns the subject, an HTML
body, a plain-text body and the inline images.  It sends nothing and knows no transport.

Email clients are the constraint, so the page is built the way mail has to be built: nested
``role="presentation"`` tables, every style inline, no ``<style>`` block, no script, no web
font, no remote image.  Rounded corners, the gradient header and the overlapping first card
degrade to square, flat and stacked in Outlook's Word renderer, and nothing is lost in reading.
The header wears the morning's weather: the Open-Meteo code and day/night pick one of the seven
skies the desktop draws, with dark text on bright skies and light text on rain, storm and night.

**The dog.**  The avatar is an inline MIME part referenced as ``cid:collie-avatar``.  Gmail
drops ``data:`` image URIs from message bodies, and Outlook for Windows does not render them
at all; a remote image is blocked by default until the reader clicks "download pictures", and
would tell a server each time the mail is opened.  A ``Content-ID`` part in a
``multipart/related`` message is shown by Gmail (web and apps), Outlook (desktop and web) and
Apple Mail without fetching anything.  A browser preview cannot resolve ``cid:``, so
``avatar="data"`` embeds the same PNG for :command:`collie report preview` only.

**Trust.**  Every string is HTML-escaped, including the ones Collie wrote, and a link is drawn
only for an ``https`` URL that passed :func:`report_signals._https`; anything else is shown as
text.  Empty sections are not drawn; a section that left signals out says how many.
"""
from __future__ import annotations

import base64
import datetime as _dt
import html

from . import morning_report as mr
from . import report_signals as rs

AVATAR_CID = "collie-avatar"
AVATAR_SIZE = 96
FONT = "-apple-system,'Segoe UI',system-ui,sans-serif"
ROUND = "ui-rounded,'SF Pro Rounded','Segoe UI',system-ui,sans-serif"
INK, MUTED, FAINT, LINE, PAGE = "#14213d", "#5b6682", "#8792ab", "#f0f3f9", "#f6f8fc"

#: sky -> (fallback colour, gradient, text is light)
SKIES = {
    "clear": ("#8fcaff", "linear-gradient(170deg,#58aaff 0%,#8fcaff 40%,#d6ecff 68%,#ffe2ad 100%)", False),
    "cloudy": ("#b8cde3", "linear-gradient(170deg,#8fb3d9 0%,#b8cde3 45%,#dfe7ef 75%,#efe6d8 100%)", False),
    "rain": ("#56687e", "linear-gradient(170deg,#3d4f66 0%,#56687e 45%,#76869a 80%,#8a96a5 100%)", True),
    "storm": ("#2f3a4f", "linear-gradient(170deg,#1f2636 0%,#2f3a4f 50%,#48536a 100%)", True),
    "snow": ("#d4e0ec", "linear-gradient(170deg,#b9cbe0 0%,#d4e0ec 45%,#eef3f8 80%,#f7f9fc 100%)", False),
    "fog": ("#d9dee3", "linear-gradient(170deg,#c7ced6 0%,#d9dee3 50%,#e9ebe8 100%)", False),
    "night": ("#131c45", "linear-gradient(170deg,#070b1f 0%,#131c45 50%,#2a2f6b 85%,#3d3470 100%)", True),
}
_DARK_TEXT = {"when": "rgba(20,33,61,.62)", "say": INK, "more": "#23355c",
              "dot": "rgba(20,33,61,.16)", "label": "rgba(20,33,61,.7)"}
_LIGHT_TEXT = {"when": "rgba(244,247,255,.72)", "say": "#f4f7ff", "more": "rgba(244,247,255,.88)",
               "dot": "rgba(255,255,255,.3)", "label": "rgba(244,247,255,.82)"}

_WORDS = {
    "en": {"wins": "While you slept", "yours": "Quick ones for you", "ready": "Ready to go",
           "ready_note": "I prepared these. Nothing leaves until you say so.",
           "projects": "Your projects", "reads": "Good reads with your coffee",
           "care": "You'll care: ", "things": "%d things today", "thing": "1 thing today",
           "things_of": "%d of %d things today",
           "clear": "All clear today", "more": "+ %d more", "review": "Review & send",
           "open": "Open", "in_drafts": "It's in your Gmail drafts.",
           "not_drafted": "I wrote a reply; it isn't in your Gmail drafts yet.",
           "bye": "Have a great %s! Reply anytime.", "signed": "%s, your Collie",
           "read": "Read %s at %s.", "not_read": "Not read: %s.", "and": " and "},
    "zh": {"wins": "你睡着的时候", "yours": "几件小事等你", "ready": "都准备好了",
           "ready_note": "这些我已经准备好了，你点头之前什么都不会发出去。",
           "projects": "你的项目", "reads": "配咖啡读一读", "care": "你会在意：",
           "things": "今天 %d 件事", "thing": "今天 1 件事", "things_of": "今天 %d 件事（共 %d 件）", "clear": "今天没有要做的事",
           "more": "还有 %d 条", "review": "看看再发", "open": "打开",
           "in_drafts": "已经放进你的 Gmail 草稿箱。",
           "not_drafted": "回复我写好了，还没放进 Gmail 草稿箱。",
           "bye": "祝你%s愉快！有事随时回我。", "signed": "你的 Collie · %s",
           "read": "%s 读取：%s。", "not_read": "没读到：%s。", "and": "、"},
}
_SOURCES = {"en": {"gmail": "Gmail", "calendar": "Calendar", "github": "GitHub",
                   "local": "This computer", "collie": "Collie", "news": "News"},
            "zh": {"gmail": "Gmail", "calendar": "日历", "github": "GitHub", "local": "本机",
                   "collie": "Collie", "news": "新闻"}}
_WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _href(value):
    return rs._https(value) if isinstance(value, str) else ""


def _zh(report):
    return str(report.get("language") or "").startswith("zh")


def _words(report):
    return _WORDS["zh" if _zh(report) else "en"]


def _local(report, when):
    minutes = report.get("utc_offset_minutes")
    minutes = int(minutes) if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) \
        else 0
    return _dt.datetime.fromtimestamp(float(when), _dt.timezone(_dt.timedelta(minutes=minutes)))


def _clock(report, when):
    moment = _local(report, when)
    if _zh(report):
        return moment.strftime("%H:%M")
    hour = moment.hour % 12 or 12
    return "%d:%02d %s" % (hour, moment.minute, "AM" if moment.hour < 12 else "PM")


def _weekday(report, moment):
    return _WEEKDAYS_ZH[moment.weekday()] if _zh(report) else moment.strftime("%A")


def _date_line(report):
    moment = _local(report, report.get("generated_at") or 0)
    weather = mr.weather_words(report.get("weather"), "zh" if _zh(report) else "en")
    if _zh(report):
        bits = ["%d月%d日 %s" % (moment.month, moment.day, _weekday(report, moment))]
    else:
        bits = [moment.strftime("%A"), "%d %s" % (moment.day, moment.strftime("%B"))]
    return " · ".join(bits + ([weather] if weather else []))


def _meta(report, item):
    """Where an item came from, and when when that helps: ``Gmail · 5:08 AM``."""
    names = _SOURCES["zh" if _zh(report) else "en"]
    bits = [names.get(source, source) for source in item.get("sources") or []][:2]
    when = item.get("when")
    if isinstance(when, (int, float)) and not isinstance(when, bool) and when > 0:
        now = float(report.get("generated_at") or when)
        moment, today = _local(report, when), _local(report, now)
        if moment.date() == today.date():
            bits.append(_clock(report, when))
        elif now - 36 * 3600 <= when <= now + 8 * 86400:
            bits.append("%s %s" % (_weekday(report, moment)[:3] if not _zh(report)
                                   else _weekday(report, moment), _clock(report, when)))
    return " · ".join(bit for bit in bits if bit)


def _sections(report):
    sections = report.get("sections") if isinstance(report.get("sections"), dict) else {}
    out = {}
    for name in mr.SECTIONS:
        part = sections.get(name) if isinstance(sections.get(name), dict) else {}
        items = [row for row in (part.get("items") or []) if isinstance(row, dict)]
        more = part.get("more")
        out[name] = (items, int(more) if isinstance(more, int) and not isinstance(more, bool)
                     and more > 0 else 0)
    return out


def _provenance(report):
    """``(read sentence, not-read sentence)`` from the sources the report was built from."""
    words = _words(report)
    rows = ((report.get("provenance") or {}).get("sources") or [])
    read, missing = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        label = str(row.get("label") or row.get("name") or "")
        if row.get("name") == "local" and isinstance(stats.get("repos"), int):
            label = ("%d 个本地仓库" if _zh(report) else "%d local repos") % stats["repos"]
        elif row.get("name") == "news" and isinstance(stats.get("feeds"), int):
            count = stats["feeds"]
            label = ("%d 个新闻源" % count) if _zh(report) else \
                "%d news feed%s" % (count, "" if count == 1 else "s")
        if row.get("state") in ("ok", "partial"):
            read.append(label)
        else:
            reason = str(row.get("reason") or "")
            missing.append("%s（%s）" % (label, reason) if _zh(report) and reason else
                           "%s (%s)" % (label, reason) if reason else label)
    joined = ""
    if read:
        joined = read[0] if len(read) == 1 else (
            "、".join(read) if _zh(report) else ", ".join(read[:-1]) + words["and"] + read[-1])
    at = _clock(report, (report.get("provenance") or {}).get("read_at")
                or report.get("generated_at") or 0)
    first = ""
    if joined:
        first = (words["read"] % (at, joined)) if _zh(report) else (words["read"] % (joined, at))
    second = words["not_read"] % ("；".join(missing) if _zh(report) else "; ".join(missing)) \
        if missing else ""
    return first, second


def subject(report):
    moment = _local(report, report.get("generated_at") or 0)
    day = "%d月%d日" % (moment.month, moment.day) if _zh(report) else \
        "%s %d %s" % (moment.strftime("%a"), moment.day, moment.strftime("%b"))
    headline = rs.daily_brief._text(report.get("headline"), 140) or rs.daily_brief._text(
        report.get("greeting"), 80)
    return "%s · %s" % (headline, day) if headline else day


# ---------------------------------------------------------------- HTML pieces


def _card(title, rows_html, *, note="", more=0, report=None, first=False):
    words = _words(report)
    parts = ['<tr><td style="padding:%s">' % ("0 12px" if first else "14px 12px 0"),
             '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
             'style="background:#ffffff;border-radius:22px;%s">'
             % ("margin-top:-34px;box-shadow:0 12px 30px rgba(40,90,160,.12)" if first else
                "box-shadow:0 12px 30px rgba(40,90,160,.08)"),
             '<tr><td style="padding:20px 20px %s;font:800 18px/1.3 %s;color:%s">%s</td></tr>'
             % ("2px" if note else "6px", ROUND, INK, _esc(title))]
    if note:
        parts.append('<tr><td style="padding:2px 20px 4px;font-size:13.5px;line-height:1.5;'
                     'color:%s">%s</td></tr>' % (MUTED, _esc(note)))
    parts.append(rows_html)
    if more:
        parts.append('<tr><td style="padding:0 20px 18px;font-size:13px;color:%s">%s</td></tr>'
                     % (FAINT, _esc(words["more"] % more)))
    else:
        parts.append('<tr><td style="padding:0 0 12px;font-size:0;line-height:0">&nbsp;</td></tr>')
    parts.append("</table></td></tr>")
    return "".join(parts)


def _title(text, link, *, size="15.5px"):
    body = '<b style="font-weight:650">%s</b>' % _esc(text)
    href = _href(link)
    if href:
        body = '<a href="%s" style="color:%s;text-decoration:none">%s</a>' % (_esc(href), INK, body)
    return '<div style="font-size:%s;line-height:1.45;color:%s">%s</div>' % (size, INK, body)


def _muted(text, *, colour=MUTED, size="13.5px", pad="0"):
    return '<div style="font-size:%s;line-height:1.5;color:%s;padding-top:%s">%s</div>' % (
        size, colour, pad, _esc(text))


def _button(label, link, *, soft=False):
    href = _href(link)
    if not href:
        return ""
    style = ("background:#eaf3ff;color:#1d63c7" if soft else
             "background:#ff8a3d;background-image:linear-gradient(100deg,#ffb547,#ff8a3d);"
             "color:#ffffff")
    return ('<div style="padding:10px 0 2px"><a href="%s" style="display:inline-block;'
            'padding:10px 18px;border-radius:999px;%s;font-weight:700;font-size:14px;'
            'text-decoration:none">%s</a></div>' % (_esc(href), style, _esc(label)))


def _row(content, index, *, last_pad="4px"):
    border = "border-top:1px solid %s;" % LINE if index else ""
    return '<tr><td style="padding:%s 20px %s;%s">%s</td></tr>' % (
        "12px" if index else "10px", last_pad, border, content)


def _wins_html(report, items):
    rows = []
    for item in items:
        detail = item.get("detail") or ""
        title = '<b style="font-weight:650">%s</b>' % _esc(item.get("title"))
        href = _href(item.get("link"))
        if href:
            title = '<a href="%s" style="color:%s;text-decoration:none">%s</a>' % (
                _esc(href), INK, title)
        rows.append(
            '<tr><td width="28" valign="top" style="padding:6px 0"><div style="width:20px;'
            'height:20px;border-radius:50%%;background:#e3f7ee;color:#1fa971;font:800 13px/20px '
            'system-ui,sans-serif;text-align:center">&#10003;</div></td>'
            '<td style="padding:6px 0;font-size:15px;line-height:1.45;color:%s">%s%s</td></tr>'
            % (INK, title, (' <span style="color:%s">%s</span>' % (MUTED, _esc(detail)))
               if detail else ""))
    return ('<tr><td style="padding:6px 20px 6px"><table role="presentation" width="100%%" '
            'cellpadding="0" cellspacing="0" border="0">%s</table></td></tr>' % "".join(rows))


def _yours_html(report, items):
    out = []
    for index, item in enumerate(items):
        content = _title(item.get("title"), item.get("link"))
        if item.get("detail"):
            content += _muted(item["detail"])
        meta = _meta(report, item)
        if meta:
            content += _muted(meta, colour=FAINT, size="12.5px", pad="2px")
        out.append(_row(content, index))
    return "".join(out)


def _ready_html(report, items):
    words = _words(report)
    out = []
    for index, item in enumerate(items):
        content = _title(item.get("title"), "")
        if item.get("detail"):
            content += _muted(item["detail"])
        draft = item.get("draft") if isinstance(item.get("draft"), dict) else None
        if draft and draft.get("state") == "created":
            button = _button(words["review"], draft.get("open_url"))
            content += button or _muted(words["in_drafts"], pad="4px")
        elif draft:
            content += _muted(words["not_drafted"], pad="4px")
        else:
            content += _button(words["open"], item.get("link"), soft=True)
        out.append(_row(content, index))
    return "".join(out)


def _projects_html(report, items):
    rows = []
    for index, item in enumerate(items):
        name = '<b style="font-weight:650">%s</b>' % _esc(item.get("project"))
        href = _href(item.get("link"))
        if href:
            name = '<a href="%s" style="color:%s;text-decoration:none">%s</a>' % (_esc(href), INK, name)
        rows.append('<tr><td style="padding:7px 0;%s">%s <span style="color:%s">· %s</span></td></tr>'
                    % ("border-top:1px solid %s;" % LINE if index else "", name, MUTED,
                       _esc(item.get("line"))))
    return ('<tr><td style="padding:4px 20px 6px"><table role="presentation" width="100%%" '
            'cellpadding="0" cellspacing="0" border="0" style="font-size:14.5px;line-height:1.45;'
            'color:%s">%s</table></td></tr>' % (INK, "".join(rows)))


def _reads_html(report, items):
    words = _words(report)
    out = []
    for index, item in enumerate(items):
        href = _href(item.get("link"))
        title = _esc(item.get("title"))
        if href:
            title = ('<a href="%s" style="font-size:15.5px;line-height:1.45;color:%s;'
                     'text-decoration:none;font-weight:650">%s</a>' % (_esc(href), INK, title))
        else:
            title = '<span style="font-size:15.5px;line-height:1.45;font-weight:650">%s</span>' % title
        content = title
        if item.get("why_you_care"):
            content += _muted(words["care"] + item["why_you_care"], colour="#1d63c7", size="14px",
                              pad="3px")
        if item.get("publisher"):
            content += _muted(item["publisher"], colour=FAINT, size="12.5px", pad="2px")
        out.append(_row(content, index))
    return "".join(out)


def _count(report):
    """``(listed, total)`` things to do.  The dots are the listed ones, never more."""
    def number(value):
        return int(value) if isinstance(value, int) and not isinstance(value, bool) \
            and value > 0 else 0
    listed = number(report.get("things_today"))
    return listed, max(listed, number(report.get("things_total")))


def _things_label(report):
    words = _words(report)
    listed, total = _count(report)
    if not listed:
        return words["clear"]
    if total > listed:
        return words["things_of"] % (listed, total)
    return words["thing"] if listed == 1 else words["things"] % listed


def _header(report, sky_name):
    fallback, gradient, light = SKIES.get(sky_name, SKIES["clear"])
    tone = _LIGHT_TEXT if light else _DARK_TEXT
    things = _count(report)[0]
    if things:
        dots = "".join('<td style="padding-right:6px"><div style="width:12px;height:12px;'
                       'border-radius:50%%;background:%s"></div></td>' % tone["dot"]
                       for _ in range(min(things, 8)))
    else:
        dots = ""
    label = _things_label(report)
    return (
        '<tr><td style="padding:0"><table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0" bgcolor="%s" style="background:%s;background-image:%s">'
        '<tr><td style="padding:28px 24px 6px;font-size:12px;font-weight:600;letter-spacing:.12em;'
        'text-transform:uppercase;color:%s">%s</td></tr>'
        '<tr><td style="padding:2px 24px 10px;font:800 32px/1.08 %s;color:%s;'
        'letter-spacing:-.02em">%s</td></tr>'
        '<tr><td style="padding:0 24px 14px;font:500 16.5px/1.5 %s;color:%s">%s</td></tr>'
        '<tr><td style="padding:0 24px 56px"><table role="presentation" cellpadding="0" '
        'cellspacing="0" border="0"><tr>%s<td style="padding-left:%s;font:700 13px/1 %s;'
        'color:%s">%s</td></tr></table></td></tr></table></td></tr>'
        % (fallback, fallback, gradient, tone["when"], _esc(_date_line(report)), ROUND,
           tone["say"], _esc(report.get("greeting")), ROUND, tone["more"],
           _esc(" ".join(bit for bit in (report.get("headline"), report.get("summary")) if bit)),
           dots, "4px" if dots else "0", ROUND, tone["label"], _esc(label)))


def _footer(report, avatar_src):
    words = _words(report)
    companion = (report.get("profile") or {}).get("companion") or "Collie"
    moment = _local(report, report.get("generated_at") or 0)
    first, second = _provenance(report)
    image = ('<td valign="top" style="padding-right:12px"><img alt="" width="42" height="42" '
             'src="%s" style="border-radius:50%%;background:#ffffff;border:1px solid #e6eaf2;'
             'display:block"></td>' % _esc(avatar_src)) if avatar_src else ""
    return (
        '<tr><td style="padding:26px 24px 34px"><table role="presentation" cellpadding="0" '
        'cellspacing="0" border="0"><tr>%s<td style="font:600 15.5px/1.45 %s;color:%s">%s<br>'
        '<span style="color:%s;font-weight:500">%s</span></td></tr></table>'
        '<div style="font-size:12px;line-height:1.6;color:%s;padding-top:16px">%s</div>'
        '</td></tr>'
        % (image, ROUND, INK, _esc(words["bye"] % _weekday(report, moment)), MUTED,
           _esc(words["signed"] % companion), FAINT,
           _esc(" ".join(bit for bit in (first, second) if bit))))


def _avatar(report):
    from . import avatar
    companion = (report.get("profile") or {}).get("companion") or "Collie"
    try:
        return avatar.png(companion, size=AVATAR_SIZE, plate=False)
    except Exception:                                 # noqa: BLE001 - a face is decoration
        return b""


# ---------------------------------------------------------------- plain text


def _text(report, parts):
    words = _words(report)
    lines = [str(report.get("greeting") or ""), _date_line(report), ""]
    lines += [bit for bit in (report.get("headline"), report.get("summary")) if bit]
    lines.append(_things_label(report))
    for name, formatter in (("wins", None), ("yours", None), ("ready", None),
                            ("projects", None), ("reads", None)):
        items, more = parts[name]
        if not items:
            continue
        lines += ["", words[name].upper() if not _zh(report) else words[name]]
        if name == "ready":
            lines.append(words["ready_note"])
        for item in items:
            if name == "projects":
                lines.append("- %s: %s" % (item.get("project"), item.get("line")))
                continue
            head = "- %s" % item.get("title")
            detail = item.get("detail") or ""
            lines.append(head + (" — %s" % detail if detail else ""))
            if name == "yours":
                meta = _meta(report, item)
                if meta:
                    lines.append("  (%s)" % meta)
            if name == "ready":
                draft = item.get("draft") if isinstance(item.get("draft"), dict) else None
                href = _href((draft or {}).get("open_url")) if draft and \
                    draft.get("state") == "created" else _href(item.get("link"))
                if href:
                    lines.append("  %s: %s" % (words["review"] if draft else words["open"], href))
            if name == "reads":
                if item.get("why_you_care"):
                    lines.append("  " + words["care"] + item["why_you_care"])
                href = _href(item.get("link"))
                if href:
                    lines.append("  %s%s" % (href, " (%s)" % item["publisher"]
                                            if item.get("publisher") else ""))
            elif name == "wins" and _href(item.get("link")):
                lines.append("  " + _href(item.get("link")))
        if more:
            lines.append("  " + words["more"] % more)
    moment = _local(report, report.get("generated_at") or 0)
    companion = (report.get("profile") or {}).get("companion") or "Collie"
    first, second = _provenance(report)
    lines += ["", words["bye"] % _weekday(report, moment), words["signed"] % companion, ""]
    lines += [bit for bit in (first, second) if bit]
    return "\n".join(str(line) for line in lines).strip() + "\n"


# ---------------------------------------------------------------- render


def render(report, *, avatar="cid"):
    """``{"subject", "html", "text", "inline"}`` for one saved report.

    ``avatar="cid"`` (email) references the dog as ``cid:collie-avatar`` and returns the PNG in
    ``inline``; ``"data"`` (a browser preview) embeds it; ``"none"`` leaves it out.
    """
    words = _words(report)
    parts = _sections(report)
    png = _avatar(report) if avatar in ("cid", "data") else b""
    inline, src = [], ""
    if png and avatar == "cid":
        inline.append({"cid": AVATAR_CID, "filename": "collie.png", "content_type": "image/png",
                       "data": png})
        src = "cid:" + AVATAR_CID
    elif png and avatar == "data":
        src = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    sky_name = mr.sky(report.get("weather"))
    body = [_header(report, sky_name)]
    builders = (("wins", _wins_html, ""), ("yours", _yours_html, ""),
                ("ready", _ready_html, words["ready_note"]), ("projects", _projects_html, ""),
                ("reads", _reads_html, ""))
    first = True
    for name, builder, note in builders:
        items, more = parts[name]
        if not items:
            continue
        body.append(_card(words[name], builder(report, items), note=note, more=more,
                          report=report, first=first))
        first = False
    body.append(_footer(report, src))
    title = subject(report)
    preheader = rs.daily_brief._text(report.get("summary") or report.get("headline"), 200)
    page = (
        '<!doctype html><html lang="%s"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" '
        'content="light"><title>%s</title></head>'
        '<body style="margin:0;padding:0;background:%s">'
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">%s</div>'
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="%s" style="background:%s"><tr><td align="center" style="padding:0">'
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;border-collapse:collapse;background:%s;font-family:%s;color:%s">'
        '%s</table></td></tr></table></body></html>'
        % ("zh-CN" if _zh(report) else "en", _esc(title), PAGE, _esc(preheader), PAGE, PAGE,
           PAGE, FONT, INK, "".join(body)))
    return {"subject": title, "html": page, "text": _text(report, parts), "inline": inline}
