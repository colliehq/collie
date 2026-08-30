# System-design interviews

Interview mode joins three surfaces without turning them into one hidden automation:

1. **VocalCode** remains the user-started, local capture and transcription app.
2. **Collie** reads only a bounded tail of the current session's transcript when transcript sharing
   is enabled, helps structure requirements and trade-offs, and keeps concise design notes.
3. **Your attached Chrome board tab** receives editable shapes only while board editing is enabled.

Open **More → System design interview** in the desktop app. Confirm participant consent, start the
session, and then attach the active Chrome board tab. Transcript sharing and board editing are
independent switches; stopping the session clears both permissions.

## Board compatibility

Collie recognizes Miro, FigJam, Excalidraw, tldraw, Eraser, Lucid, Whimsical, Microsoft Whiteboard,
Canva, diagrams.net, CoderPad, and HackerRank drawing surfaces. Miro, FigJam, Excalidraw, and tldraw
use their editable canvas controls through Collie's attached browser bridge. Eraser can use its
official MCP connection. Other recognized boards receive provider-aware guidance and a portable
diagram plan instead of pretending an unverified write succeeded.

Board operations use a small provider-neutral model of nodes and edges. Preview the plan first;
applying it adds bounded content and never deletes existing objects. The attached tab is rechecked
before each write so authority cannot silently move to another page.

## Interview conduct and privacy

Use the feature only when the interview's rules allow assistance and every participant has agreed to
recording and AI use. Collie is designed as a visible collaborator: it will not hide itself, evade
monitoring, impersonate you, or provide a stealth mode.

VocalCode transcript files stay under its own local application-data directory. Collie reads a
bounded recent slice from a meeting created for the current session and does not ingest audio. If
you enable transcript sharing, that text can be sent to the model provider configured in Collie.
Stopping the session immediately clears transcript-sharing and board-editing authority.
