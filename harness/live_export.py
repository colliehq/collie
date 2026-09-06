"""Portable, text-only reviews of retained Live context; no provider calls."""
from __future__ import annotations

import datetime as dt
import html
import re


def _markdown(value) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    # A transcript or model answer is content, not Markdown/HTML instructions.
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", html.escape(text, quote=False))


def _timestamp(value) -> str:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC") if value else "—"
    except (ValueError, TypeError, OverflowError, OSError):
        return "—"


def render_review(value: dict, *, include_events=False, language="en") -> str:
    zh = str(language).lower().startswith("zh")

    def label(en, cn):
        return cn if zh else en

    lines = ["# " + label("Collie Live session review", "Collie Live 会话回顾"), "",
             "- " + label("Session", "会话") + ": " + _markdown(value.get("session_id")),
             "- " + label("Started", "开始时间") + ": " + _timestamp(value.get("started_at_ms")),
             "- " + label("Ended", "结束时间") + ": " + _timestamp(value.get("ended_at_ms")),
             "- " + label("Status", "状态") + ": " + (
                 label("Active", "进行中") if value.get("active") else label("Ended", "已结束")),
             "", label("This review contains retained context, not a complete recording. Older signals "
                       "may have been trimmed. Audio and screenshots are not included.",
                       "此回顾包含当前保留的上下文，不是完整录音记录；较早信号可能已被裁剪。不包含音频和截图。")]

    def section(title, rows):
        if rows:
            lines.extend(["", "## " + title, "", *rows])

    section(label("Starting context", "起始上下文"),
            [_markdown(value["context"])] if value.get("context") else [])
    section(label("Summary (AI generated)", "摘要（AI 生成）"),
            [_markdown(value["summary"])] if value.get("summary") else [label(
                "No summary was generated for this session.", "本次会话尚未生成摘要。")])
    section(label("Notes", "笔记"), ["- " + _markdown(row.get("text"))
            for row in value.get("notes") or [] if row.get("text")])
    cues = ["- " + _markdown(row.get("text")) for row in value.get("suggestions") or []
            if row.get("text") and not row.get("dismissed")]
    if cues:
        section(label("Suggestions", "建议"), [label(
            "Suggestions are not evidence of completed work.", "以下是建议，不代表任务已经完成。"), "", *cues])
    work = []
    for row in value.get("work") or []:
        work.append("- %s — %s: %s; %s: %s" % (
            _markdown(row.get("goal")), label("Mission", "任务编号"),
            _markdown(row.get("mission_id")), label("Last recorded status", "最后记录状态"),
            _markdown(row.get("state") or "unknown")))
    section(label("Background work", "后台任务"), work)
    if include_events:
        section(label("Retained context log", "保留的上下文日志"), [
            "- %s · %s: %s" % (_timestamp(row.get("at_ms")),
                _markdown(row.get("speaker") or row.get("source") or "context"),
                _markdown(row.get("text")))
            for row in value.get("events") or [] if row.get("text")])
    return "\n".join(lines) + "\n"
