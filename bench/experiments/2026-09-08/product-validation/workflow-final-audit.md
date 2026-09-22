# Final native review disposition

An additional native Claude Code read-only review inspected integrated source
`4eaad5f6` for request loss, repeated execution and mutation of the wrong task. It
completed in 329.565 seconds and reported no high-severity regression. It inspected
the actual UI and backend acceptance paths; it did not execute tests. The 75 focused
tests and final source/wheel checks are separate controller-run evidence.

The review confirmed that inbox retry handling also compares resolved attachment
content, so re-uploaded opaque asset ids do not alone make a same-content retry a
new request. The retained request id is preserved, and inbox listings include
completed entries as well as pending entries for acknowledgement reconciliation.

It noted the conservative behavior on pagehide: a save that has not posted yet is
retained instead of continuing from an abandoned document. This also applies when
navigation places a document in the browser back/forward cache. A return may therefore
need a manual resend even though the payload remains intact. Real back/forward-cache
behavior and iOS app switching were not exercised; the review's broader iOS event
claim is not adopted as a verified finding. No late behavioral change was made on
the basis of this untested browser-specific suggestion.

Two statements in the raw native report require qualification:

- The server emits start before inference, but absence of a received start frame is
  not proof that inference never happened. The implemented client correctly retains
  such submissions as unknown. Only the explicit busy refusal proves that this stream
  did not start another run.
- A pruned inbox entry does not immediately permit the same id to execute again;
  tombstone identity/digest receipts remain after entry compaction. Those receipts
  also have bounded retention. No guarantee of eternal deduplication is claimed.

Other acknowledged limits are unchanged: retained rows belong to the originating
thread, unknown direct streams have no inbox receipt to reconcile against, and tab
closure is outside the same-tab reload guarantee. The review does not prove all
browser interleavings safe or replace execution tests. Its original report and trace
are preserved locally; this disposition is the reviewed export.

After this review, the root controller independently found and fixed image-only
retention in `1a0f548a`, with two failing-then-passing browser cases. This audit is not
claimed as a review of that later patch; its focused regression and rebuilt package
are separate evidence.
