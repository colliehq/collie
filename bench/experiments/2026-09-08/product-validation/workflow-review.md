# Reviewed web submission recovery

Product commit `33e296fb` integrates the native Claude Code workflow review and the
root review corrections. Its source candidate was committed as `96cbad62`; the initial
integrated release-note/source pin is `4eaad5f6`. Follow-up `1a0f548a` adds attachment-only
recovery and replaces obsolete implementation-string checks. Native review runs and local mock
experiments are not included in the 166 coding benchmark attempts.

The original failure occurred when one window sent a request before learning that
another window owned the conversation's active execution lease. The managed endpoint
refused it before starting another run, but the optimistic composer had already been
cleared. Refetching the transcript then erased the only visible copy of the request.

A confirmed busy refusal now queues the captured text, attachments, contexts and
configuration against the original conversation. Leaving the conversation before the
answer keeps a bounded listener, suppressing callbacks into the newly selected task.
A delayed accepted start leaves the existing server run alone. No uncertain stream
submission is automatically replayed.

Unconfirmed requests have separate identities and retained rows. The same words with
different attachments remain separate requests. Reloads preserve the request in that
tab's sessionStorage; an image storage failure preserves the missing-file count and
blocks sending until the file is reattached or explicitly omitted. New-task recovery
restores the captured configuration as well as the composer; Pack application grants
are still not remembered. Complete storage unavailability is shown honestly as a
window-only copy.

Root review found and corrected additional issues in the native candidate:

- The retained-record map was initialized after the first queue render. It is now
  initialized before use; malformed stored entries are ignored and in-flight flags
  are reset when the page reloads.
- Reloading while the queue POST awaited confirmation could still lose the original
  request, as could typing a newer draft before a failed acknowledgement. Two new
  tests failed on the candidate and passed after tracking outstanding saves. The
  request id survives reload, and a late ACK never clears newer text or attachments.
- A durable inbox listing reconciles that exact id automatically, removing an
  unconfirmed row without a second POST. Unknown stream requests remain distinct from
  requests with a proven inbox receipt.
- Malformed terminal frames, pre-start errors and upload failures now retain the
  current thread's request too, without overwriting a newer composer draft.
- The attachment-quota test initially injected its storage failure only on the next
  page load. It now affects the original write and specifically refuses image-bearing
  records while permitting the metadata fallback. The setup test uses the valid
  Review intent, rather than a value unavailable in the product selector.

Initial focused validation: 75 tests passed in 118.88 seconds, covering the new busy-send
suite and existing run-status, workspace-draft and task-continuity suites. Earlier
failures are retained locally; `queue-boundary-before.log` preserves the two actual
data-loss regressions. Final integrated full regression and installed-wheel results
are separate source-pinned artifacts in this directory.

The first integrated full run had 3,650 passing tests, 17 skips and one obsolete string
assertion requiring the upload callback to return without retaining anything. That
assertion and another inline composer-assignment check were removed in favor of the
actual browser behavior tests. Their surrounding setup and mobile-dialog checks remain.

Root review then tested attachment-only submissions, a supported direct-stream input.
Two navigation/reload cases failed because retention required nonempty text. The fix
preserves an image-bearing submission with empty text and returns it to the composer,
including in an existing thread where the text-based inbox cannot accept it. A request
with only missing images offers reattachment or discard, not an empty send. All 24
busy-send cases passed after this fix, and the existing UI-site checks passed. The
final full run and rebuilt wheel use the resulting source pin, not the earlier candidate.

The actual retained-row surface was inspected at 1280 px English and 390 px Chinese.
Both had no page errors or horizontal overflow, and the newer composer draft remained
visible and usable. The screenshots use real product UI resources and synthetic local
server responses; they are not model-run screenshots. Earlier native review scripts
also exercised real mock-backend queue and two-window flows, but their intermediate
reports are not used as proof of the final revision.

Limitations: sessionStorage supports same-tab reload recovery, not a guarantee after
closing the tab or clearing browser data. Uploads can reach the server before a run
starts. Unacknowledged streams after the 30-second detached-listener bound remain
uncertain. Inbox ids prevent a queue retry becoming a duplicate, but a stream whose
acceptance is unknowable still needs a human decision. Browser storage remains bounded,
and attachment garbage collection is unchanged. Timing-based UI fixtures may need
larger allowances on substantially slower machines.
