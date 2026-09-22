# Procedural memory and learned workflows

Collie can learn the shape of repeated work without recording the contents of your
screen or turning observation into permission.

This is different from fact memory:

- **Semantic memory** stores reviewed facts and preferences.
- **Episodic history** stores session and audit receipts.
- **Procedural memory** notices repeated action sequences across separate sessions.
- **Preference memory** remains an explicitly reviewable kind of semantic memory.

## Privacy boundary

Raw procedural observations are device-only. A row contains an application, action,
object category, privacy-reduced object identity, outcome, session, project, and a few
allowlisted operational labels.

Collie does not put screen pixels, page or email bodies, typed text, clipboard contents,
prompts, model responses, credentials, URL paths/query strings, or complete shell
commands in this journal. Web targets are reduced to their origin. Commands are reduced
to the executable. Files inside a project use a relative path; external files use only
their basename.

Password managers, private/incognito windows, password fields, and user-configured
applications are excluded. Learning can be paused immediately and raw observations
expire after 30 days by default.

    collie routine status
    collie routine pause
    collie routine resume
    collie routine exclude --app "My private app"
    collie routine retention --days 14
    collie routine purge --yes

Purge deletes raw observations. It does not silently delete an accepted derived
workflow.

## Discovery and review

Discovery mines action sequences that appeared in at least two distinct sessions.
Candidate summaries and local embeddings are derived on the device.

    collie routine discover
    collie routine candidates
    collie routine candidates --query "test a release"
    collie routine accept routine_ID --yes
    collie routine dismiss routine_ID --yes
    collie routine workflows

Accept and dismiss are explicit, confirmed review actions. An accepted learned workflow
has no authority: it is useful context and a future automation draft, not an executable
permission. Frequency, confidence, model output, web content, and synced state can never
grant authority.

## Multi-device sync

Only derived candidates and accepted workflows are eligible for Connected Mode. They
are sealed, encrypted on the device, and decrypted only by another device holding the
user's recovery key. The Online adapter has no code path that stages or imports the raw
event table.

Imported routines are restricted to the same user and are forced back to zero authority
at the receiving-device boundary.
