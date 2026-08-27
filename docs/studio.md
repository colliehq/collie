# Studio

Open **More tools → Studio** in `collie web`, or visit `/studio` on the local Collie server. Studio
collects workflows that need an explicit review boundary instead of hiding them behind chat prose.

## Record a workflow as a Skill

Choose a current session, name the workflow, and start recording. Collie records bounded structural
events such as tool calls, edits, browser actions, and verification evidence; it does not store the
token stream. Secret-shaped values and credential fields are redacted before the draft is written.

Stopping creates a draft `SKILL.md`. **Dry replay** shows the exact event plan without executing it.
The draft becomes eligible for approval only after at least one evaluation contains the recorded
event shapes and verification evidence. **Approve Skill** writes the reviewed bytes under the
project's `.collie/skills/` directory. Recorded approvals are never replayed, and the installed Skill
continues to use the ordinary Collie permission gates.

## Migration center

The migration center inventories local Claude, Cursor, Codex, Pi, and Hermes directories. A dry-run
records the source digest, destination, size, and any conflict for each selected instruction,
setting, Skill, session archive, or hook file.

Apply copies only files whose source digest still matches the reviewed plan. Identical destinations
are skipped and different destinations are reported as conflicts; Collie never overwrites them.
Imported Skills use an `imported-SOURCE-NAME` namespace. Other artifacts remain in
`~/.collie/imports/SOURCE/` for review. Optional sync remembers only the approved source and item
types and applies the same no-overwrite rule on later scans.

## Task graph and file ownership

Plan tasks already support stable IDs, `depends_on`, owners, and file lists. Studio adds execution
coordination:

- a task is ready only when every dependency is complete;
- claiming it creates a five-minute renewable lease and returns an unguessable token;
- one owner can hold one task at a time;
- two live claims cannot own the same normalized file;
- completing or releasing requires the claim token; and
- an expired claim returns to pending instead of staying falsely in progress.

Claim tokens stay in the browser's session storage. Reading the plan graph never exposes their
stored hashes.

## Anchored comments and partial rework

Comments can point at a file/diff line range, normalized image rectangle, meeting timestamp, or
session message. Selecting comments produces a Build prompt containing only those anchors. Collie
re-reads the current artifacts because an old anchor may be stale; creating a comment itself grants
no write authority.

## Session time travel and Handoff

The timeline can fork a conversation at any message boundary without changing its source. Forks
retain parent ID, fork index, lineage, and workspace metadata.

Handoff to **isolated** creates a named Git worktree and records its base commit. Handoff back to
**local** is an explicit operation: the main checkout must be clean, Collie builds a binary-safe diff
from the recorded base, runs `git apply --check`, and only then applies it. Conflicts leave both
workspaces untouched. The isolated worktree remains available for comparison and recovery unless it
is explicitly removed.
