# Live Copilot

Live Copilot is a top-level way to work with Collie, not a meeting or interview plug-in. During an
explicit session, Collie maintains a small current-context model from the signals you enable:

1. Collie's own first-party UI can capture microphone and meeting/system audio and retain transcript
   text rather than audio chunks.
2. Window awareness records foreground application names and window titles. Interface awareness
   adds a bounded set of accessibility control types and labels; labels may contain visible page or
   document text. Content-free activity pulses say that the user interacted and which control type
   had focus. None of these signals contains raw keys, clipboard content, passwords, field values,
   or screenshots.
3. The configured model continuously compresses recent events into a short current understanding
   and a few timely cues. A cue has no authority and never runs itself.

The task description before starting is optional. You can type a natural request such as “start a
Live Session for this system-design interview” in the ordinary desktop composer; Collie starts the
mode without making you navigate to the Live page. Keep the main window minimized if you prefer.

While the session is active, press **Ctrl+Alt+Space** from any Windows application. Collie freezes
the exact foreground process/window and its bounded accessibility labels *before* focus changes,
then opens only a small top-of-screen capsule. In the normal Windows app, holding the second mouse
side button (X2) also opens the capsule; releasing it ends that recording. Capsule audio uses the
configured Live transcription route. Local SenseVoice is preferred when its model, optional
`speech` dependencies, and ffmpeg are available; otherwise check the speech destination shown in
Live before enabling capture. Only recognized command text goes to the configured Collie model.
You can say “write what I just said here” or “finish this
design module”; the generated task is explicitly targeted back to the prior window rather than the
capsule. The exact recognized or typed command is the authenticated authority for that turn, so
ordinary work on the captured target does not require a second blanket approval. A commit still has
to be explicitly named, and purchases, secrets, security changes, a different target, and ambiguous
irreversible work remain gated. Long work can become a durable
Mission while the full Collie window stays out of the way.

## Optional work surfaces

Collie recognizes Miro, FigJam, Excalidraw, tldraw, Eraser, Lucid, Whimsical, Microsoft Whiteboard,
Canva, diagrams.net, CoderPad, and HackerRank drawing surfaces. Miro, FigJam, Excalidraw, and tldraw
use their editable canvas controls through Collie's attached browser bridge. Eraser can use its
official MCP connection. Other recognized boards receive provider-aware guidance and a portable
diagram plan instead of pretending an unverified write succeeded.

Board operations use a small provider-neutral model of nodes and edges. Preview the plan first;
applying it adds bounded content and never deletes existing objects. The attached tab is rechecked
before each write so authority cannot silently move to another page.

## Session conduct and privacy

Confirm that every participant agrees before enabling conversation capture. In the native Windows
app the local microphone recognizer continues while the Live session is active, even when the Live
page is closed; meeting/system audio still requires the operating system's visible share picker.
In a browser, the UI requests microphone and system audio in one start flow. Each short audio chunk
is deleted after the configured
speech service returns text; it is never placed in the agent prompt. Transcript and derived state
stay under Collie's private local state directory, while text sent for speech/understanding follows
the destinations disclosed in the UI.

Capsule command recognition is different from continuous conversation capture: it is one-shot
and starts after the user presses the shortcut, side button, or microphone button. The explicit
handoff retains the foreground process name, PID/window handle, window
title, and bounded control types/labels so a command can return to the right surface. It does not
retain field values, keys, clipboard content, or a screenshot.

The Live log retains up to 1,200 bounded events within a 4 MiB state limit; the page shows the latest
120. Older events may be trimmed sooner to stay within the byte limit. Understanding is
prewarmed from changed context every few seconds rather than waiting for the capsule. Stopping the
session clears capture, continuous-understanding, and optional work-surface authority.
An interactive task or Mission then continues under Collie's ordinary permission, budget, evidence,
and recovery boundaries.

## Review and export

After stopping, the Live page keeps the last session's summary, cues, log, and background task
records visible. Refreshing the page preserves this review. Live-only cue actions and board edits
are disabled for an ended session.

Choose **Export Markdown** to save the starting context, AI-generated summary, notes, visible
suggestions, and last recorded background task states. Check **Include context log** to also
include all retained text events. Audio, screenshots, and avatar join credentials are not exported.
The review is available in English and Chinese and requires no new model request.

Only the latest session is retained here. Export before starting another session if you want to
keep its review. The exported task states are snapshots; open the corresponding Mission for its
current state and execution evidence.
