# Live Copilot

Live Copilot is a top-level way to work with Collie, not a meeting or interview plug-in. During an
explicit session, Collie maintains a small current-context model from the signals you enable:

1. Collie's own first-party UI can capture microphone and meeting/system audio and retain transcript
   text rather than audio chunks.
2. Low-privacy environment awareness can notice foreground application names. A separate switch can
   include a bounded set of active-interface accessibility control types and labels; labels may
   contain visible page or document text. Neither mode logs keys, clipboard content, field values,
   window titles, or screenshots.
3. The configured model continuously compresses recent events into a short current understanding
   and a few timely cues. A cue has no authority and never runs itself.

The task description before starting is optional. You can type a natural request such as “start a
Live Session for this system-design interview” in the ordinary desktop composer; Collie starts the
mode without making you navigate to the Live page. Keep the main window minimized if you prefer.

While the session is active, press **Ctrl+Alt+Space** from any Windows application. Collie freezes
the exact foreground process/window and its bounded accessibility labels *before* focus changes,
then opens only a small top-of-screen capsule. The capsule immediately listens using Windows' local
speech recognizer (Chinese and English when those recognizers are installed). Only recognized command
text goes to the configured Collie model. You can say “write what I just said here” or “finish this
design module”; the generated task is explicitly targeted back to the prior window rather than the
capsule. Short work runs interactively under the ordinary Gate. Long work can become a durable
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

Confirm that every participant agrees before enabling conversation capture. The UI requests
microphone and system audio in one start flow. Each short audio chunk is deleted after the configured
speech service returns text; it is never placed in the agent prompt. Transcript and derived state
stay under Collie's private local state directory, while text sent for speech/understanding follows
the destinations disclosed in the UI.

Capsule command recognition is different from continuous conversation capture: it is one-shot,
starts only after the user presses the shortcut or microphone button, and uses the local Windows
speech engine. The explicit handoff retains the foreground process name, PID/window handle, window
title, and bounded control types/labels so a command can return to the right surface. It does not
retain field values, keys, clipboard content, or a screenshot.

Stopping the session clears capture, continuous-understanding, and optional work-surface authority.
An interactive task or Mission then continues under Collie's ordinary permission, budget, evidence,
and recovery boundaries.
