# Reviewed session-storage change

The native Claude review found false history divergence after loading legacy tool-call
records. Repeated checkpoints could append the same history again. The first proposed
fix normalized both records and replaced the stored one, losing unknown tool fields
and malformed legacy entries. A follow-up kept the stored prefix but still normalized
new incoming fields away. Both intermediate patches/reports remain in local review
artifacts; neither was merged.

The merged implementation compares exact records first, then their JSON form, then
only the stored record's existing loader projection against the unmodified incoming
serialized record. Matching prefixes retain the original stored records. New incoming
metadata, false/zero arguments and actual differences remain distinct. The loader's
existing compatibility behavior is unchanged; unknown data stays durable even when
the loader cannot expose it as a current ToolCall.

Independent regressions for incoming metadata and false/zero arguments failed four
times on the follow-up candidate, then passed after this correction. The focused
Windows Python 3.14 suite passed 325 tests with 2 existing skips. The same source passed
52 storage/resume tests in network-disabled Python 3.12.11 Linux, using read-only
source and pytest dependency mounts. Deep-nesting coverage is conditional on the
interpreter being able to read that document; it does not promise unlimited depth.

The writer now encodes the JSON once before opening the temporary file. Flush, fsync,
permissions, bounded Windows replacement retries and atomic replacement are retained.
This adds a transient full-document string allocation; it does not change the format
or eliminate full-file rewrites.

An independent 22.9-second interleaved A/B profile (`storage-profile.json`) verified
identical output bytes at all six sizes. On this Windows machine, the 5,000-message
rich profile's complete checkpoint median changed from 60.47 to 42.72 ms; the lean
profile changed from 22.42 to 14.26 ms. The 100-message rich case was essentially flat
(10.06 vs 10.17 ms). Concurrent local activity and filesystem behavior limit absolute
timing claims. These are synthetic storage measurements, not model benchmark attempts.

At 1,000 growing rich messages, the final journal was about 836 KiB but accumulated
408 MiB of writes. Append-oriented storage remains future work. Directory fsync on
POSIX, broader loader evolution and eliminating inbox polling's full journal decode
were not changed. No claims are made about power-loss testing or all platforms.
