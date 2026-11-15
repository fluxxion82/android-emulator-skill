# anr_watcher.py

Record & summarise Android ANRs/jank from logcat (Choreographer skipped-frames + ActivityManager ANRs) with session-based progressive disclosure.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Session mode: `--start [--package PKG]` → id (the bare id as text, for
  `SID=$(...)`; `{"success": true, "session_id": ...}` under `--json`); `--stop ID [--abandon]` → token-tight summary; `--get-details ID [--cluster N]`; `--list-sessions`; `--clear-sessions`; `--diff A B`
- **A session's lifecycle is serialised** (`common/anr_sessions.py`):
  `pending` → (the worker claims it) `opening` → (its read open)
  `running` ⇄ `deciding` → `stopped` | `crashed`; `opening` → `crashed`
  (the opening failed) | `cancelled` (a stop came first); `pending` →
  `cancelled` (a `--stop` or a `--start` that gave up on its worker); or
  `opening`/`running`/`deciding` → `abandoned` (`--stop --abandon`). Every read-modify-write of a session's `meta.json` and
  `summary.json` is made under its lock (`flock` on
  `anr-sessions/locks/<session>.lock`, outside the session's directory, so
  deleting a session never unlinks a lock a process holds; every opener
  takes `locks/.base` first and lets it go once it holds the session's; the
  wait for it is bounded, not the hold, and a wait past the bound is a
  refusal, exit 1, "run the same command again"), by one owner per
  transition, and only from the state it expects — otherwise it is
  refused, never applied — and goes to a temporary file of its own,
  renamed into place. The worker owns its claim, the opening's end,
  `deciding`, its counters and its end, which it publishes before it exits (a stop, a
  device lost, restarts exhausted, a stream that broke, an error nothing
  foresaw; a SIGTERM meanwhile only asks for a stop): `stopped`, or
  `crashed` with its failure, in one write — unless it cannot have the
  lock within 5 minutes or the session was deleted, when `--stop` finds
  it gone without having said, and `--abandon` is the way out.
  `--list-sessions` and `--get-details` give that answer too, judging the
  worker as `--stop` does (its token and recorded start time, not pid
  liveness): the listing keeps the persisted `status` (`running` /
  `deciding`) and adds `"unpublished_end": true` with
  `unpublished_end_reason` in JSON (`false` / `null` for every other
  session), and in text shows `gone` and `worker gone, capture end
  unpublished: <why>`; `--get-details` on such a session, which has no
  summary, refuses (exit 1) naming `--stop ID --abandon` — it said `Run
  --stop ID first`, and that `--stop` refuses. The CLI
  owns `cancelled`, `abandoned` and `summary.json`. `events.jsonl` is not
  locked: the worker appends to it as it reads. A worker whose claim is
  refused — its session cancelled, already claimed, or not its token —
  exits writing nothing; one whose session is abandoned or deleted stops
  at its next meta write (a counter write while its stream is quiet, at
  most every 5 s and skipped when the lock is busy, or its next
  transition), and may append events until then, which the session's
  answers ignore (below). The lock needs a filesystem that implements
  `flock` — a local one; a home on a network filesystem serialises only as
  far as its server and mount options honour it — and telling the worker
  from another process needs `ps`. A worker of an earlier version (a
  session with no `lifecycle` in its meta) takes none of these locks:
  while its recorded pid may be it, `--stop` trusts nothing the session
  says — it signals it and waits until it is gone, and refuses (exit 1,
  `--abandon` included, which cannot fence it) while it lives or cannot
  be identified; `--get-details` and `--diff` refuse such a session too
  (exit 1, naming the pid: they answered 0 from a snapshot the old worker
  could still change), and `--list-sessions` marks it `provisional`
  (`"provisional": true` and `provisional_reason` in JSON, a
  `provisional:` note in text). Refused rather than answered as
  provisional, because an exit 0 is what an agent acts on. Four
  reviews had closed one timing window at a time: a stop
  during the reader's end-of-stream grace skipped the decision (exit 0
  over a device loss); `--stop` answered at exit 0 while the worker was
  still to publish a failure, wrote back a meta it had read after the
  worker had persisted one, and took an unrecorded start time for
  another process; a worker claimed a session `--stop` had already
  answered for; `--start` took a failure published under `running` for
  a started session; a fifth found a `stopped` meta trusted while that
  version's worker ran on (exit 0, then the device loss it persisted),
  and an abandoned session's answer growing with every event appended
  after the abandonment
- **Pruning removes only sessions that have ended.** The TTL
  (`ANDROID_EMU_ANR_SESSION_TTL_HOURS`) and the aggregate cap
  (`ANDROID_EMU_ANR_TOTAL_CAP_MB`), both applied on every `--start`, and
  `--clear-sessions` remove a `stopped`, `crashed`, `cancelled` or
  `abandoned` session — each under its lock, skipped while another command
  holds it — and never a `pending`, `running` or `deciding` one, nor an
  earlier version's whose worker cannot be shown gone; nothing goes by age
  alone. A session directory with no `meta.json` whose lock is free is a
  creation that died half-way, and is removed. `--start` creates a session
  under its lock, from before the directory exists until its meta does, so
  a prune running beside it cannot take it for one. A `pending` session
  whose `--start` died before its worker claimed it stays until `--stop`
  cancels it; a `running` one whose worker died without saying how it
  ended stays until `--stop --abandon`. Only directories named as
  `--start` names them (`anr-YYYYMMDD-HHMMSS-XXXX`) are sessions to any of
  these, or to `--list-sessions`: `locks/` and anything else kept there
  are left alone. The cap is best effort: when the sessions it may not
  remove exceed it on their own, `--start` still starts, and warns on
  stderr (`warning: ANR sessions hold N bytes over
  ANDROID_EMU_ANR_TOTAL_CAP_MB (M MB): the K session(s) left have not
  ended, or another command holds them, ...`); stdout stays the id
- An unknown session, a session with no summary yet, or a `--cluster` past
  the end exits 1 and says what to run instead (the refusal object under
  `--json`) — it does not print the sentence and exit 0
- `--older-than` takes `30s`/`5m`/`24h`/`7d` and a session id is
  `anr-YYYYMMDD-HHMMSS-XXXX`; either malformed is a usage error (exit 2)
  for every mode that takes one, `--stop` included, and `--clear-sessions`
  deletes nothing when it rejects one
- Every reader — `--watch`, `--since` and a session's worker — reads
  logd's records (`logcat -B`, `common/logcat.py`), never text: each
  record carries the pid and uid logd took from the writing socket, and
  an ANR report arrives whole, as one record. Each event keeps that uid
  (`"uid"` in `--watch`/`--since` JSON and in a session's
  `events.jsonl`; `uid=` in `--get-details --cluster N`); an event an
  earlier version wrote has none and still loads
- An ANR is counted once per incident as far as the log can show it.
  ActivityManager logs an ANR as ONE record of many lines; its `PID:` (the
  stalled app's own pid — the record's pid is system_server's) and
  `Reason:` (what timed out) arrive as the event's `app_pid` and `reason`,
  not as further ANRs, and the event is out as soon as the record is read.
  WindowManager's `ANR in Window{<hash> u0 <title>}` record is a report
  *to* ActivityManager: it is dropped when ActivityManager names the same
  WINDOW, by its hash, within 10 s (by log time, either side) — the
  Reason: of its report of an input ANR (recorded: 0.46 s after
  WindowManager's), or its `Skipping duplicate ANR` verdict (recorded:
  after a broadcast ANR, whose own report names no window). Otherwise it
  is an event of its own — a stream cut between the two, or one of
  ActivityManager's early returns — emitted once 10 s of system_server's
  log, or of quiet, have passed or the stream ends, so it can print after
  later events. One that ActivityManager's report of a window-less
  (broadcast) ANR within 10 s may answer is kept and marked
  `possible_duplicate` (a session counts them, `possible_duplicates`, and
  says so in a `Possible duplicate:` line): only the window's title could
  tie them, and the title used to settle it — so any title made a separate
  ANR vanish. Only system_server's records move that clock: a record's
  time is its writer's, and an app's record dated a minute ahead released
  a held report early, so one ANR counted twice. An ActivityManager record
  arriving more than 10 s after WindowManager's report would still be
  counted twice
- **A window names no package.** What follows the hash in `Window{…}` is
  the window's TITLE, which the app sets (an activity's defaults to its
  component, which is what the recordings show) — a title reading
  `com.example.victim/…` made an ANR of that app, and one reading
  `Skipped 80 frames!` made the report jank. The title is display text:
  nothing in it is classified or settles anything, and an ANR WindowManager
  alone reported is an ANR of a window — `anr_package: null`, `identity:
  "window"`, the title kept as `window`, no symbol taken from it
- **ActivityManager names a process, not a package.** `ANR in <name>` is
  the stalled process's name, and an app may name its process after any
  package (`android:process`; recorded: the fixture app's own stall,
  reported as `ANR in com.example.victim`). So an ANR is filed under the
  package (`"attribution": "verified"`) only when its `PID:` is shown to
  run as one of that package's uids: by system_server's `Start proc
  <pid>:<name>/<uid>` earlier in what was read, or — without one — by one
  bounded `ps -o PID,UID -p <pid>` now, which cannot prove who held the
  pid when the ANR was written if it has been reused since. Otherwise it
  is an ANR of a process: `anr_package: null`, `identity: "process"`, the
  name kept as `process_name`, `attribution` `unverified` or
  `contradicted`. An ANR whose process was already gone and not started
  while it was read stays unattributed
- `--diff` compares neither kind (both are listed under `not_comparable`,
  as is an app's ANR whose only possible counterpart is one), and
  `--package` drops both — counted in a session's `unattributed_anrs` (an
  `Unattributed:` line in text), and said on stderr by `--watch` /
  `--since`. In text, each such line says it too (`[unverified: process
  <name> names no verified package]`, `[an ANR of a window: its title
  names no package]`, `[possible duplicate of the ANR at <time>]`), as do
  `--get-details --cluster N` (the stored `attribution`) and the
  `--since` summary ("ANR/jank events: 2, including 1 possible
  duplicate; 1 unverified")
- An ANR report counts only as system_server wrote it: an `ANR in` record,
  and ActivityManager's `Skipping duplicate ANR` verdict, must carry
  exactly the right tag, open the record's message, and have been written
  by system_server — the record's pid, checked against `pidof
  system_server` (asked once, bounded at 3 s, in every mode). Any app can
  log under those tags, and one could forge an ANR for another package or
  make a real one vanish; in text, an app's TAG with a newline in it
  printed a whole line headed with system_server's pid (recorded:
  logcat_tag_injection), which as a record is the app's. When
  system_server's pid is unknown nothing is counted, and it is said: the
  session summary carries `"verification": "system_server pid unknown —
  ANR records could not be authenticated"` and `unverified_anr_lines`, its
  text forms an `Unverified:` line ("no *verified* ANR/jank"), and
  `--watch` / `--since` a `warning:` on stderr (and, in text, "No verified
  ANR/jank events")
- Jank clusters by process, not by frame count: one screen dropping 31 and
  120 frames is one cluster, two apps janking alike are two. A session
  names the process behind a jank line and keys it by that name
  (`identity: "package"`), so an app restarted mid-way is still one site.
  The name comes ONLY from a `Start proc <pid>:<name>/<uid>` record earlier
  in the same stream, read by `common/log_lifecycle.py` — the same reading
  `log_monitor --app` uses: written by system_server (the record's pid,
  against `pidof system_server`, asked of the device once), under
  ActivityManager's tag exactly, the exact opening of the record's
  message, and a process that is exactly a package
  running under one of its uids (`cmd package list packages -U`, once per
  package) — any app can log under ActivityManager's tag, and one can name
  its own process after another package. A private process
  (`<package>:<name>`) is not named. Each lookup is bounded at 3 s; an
  unknown answer names nothing. A name ends at system_server's `Killing` /
  `has died` for the pid, and every stream (re)start forgets them all. The
  device is never asked who holds a pid now: `ps … ELAPSED` was judged
  against when a line was *read*, and a line read late was given a
  recycled pid's new owner. **So only a process started while the session
  runs is named**: an app already running when `--start` was called (warm
  in the background) is keyed by pid. A name is a label: under
  `--package`, whose a jank or StrictMode record is goes by its uid
  (below), named or not. Jank whose process could not be named is keyed
  by pid (`identity: "pid"`) — unless `--package` attributed it by uid,
  which keys it by the package — and the summary lists those pids —
  `"unnamed_pids": {"count": N, "pids": [...]}` and one `Unnamed:`
  sentence in text; `--diff` lists a pid-keyed cluster under
  `not_comparable` instead of calling it resolved or new, and so does a
  named cluster whose only possible counterpart in the other session is
  such a pid-keyed cluster of the same kind and text
- `--stop` signals the session's worker only once the process at its
  recorded pid is shown to still be it: the random token `--start` put on
  its command line, and the start time `ps` gave when it claimed the pid.
  Anything else holding that pid is never signalled. A live pid that can
  be shown neither to be the worker nor not to be — `ps` could not say,
  the signal was refused, or the claim's start time was not recorded
  (`ps` did not answer then; that is no proof of a later process) — is
  not taken for a worker that ended: `--stop` refuses (exit 1, naming the
  pid and why), saves nothing and keeps the pid, for a later `--stop`
- `--stop` answers with a summary only once the session has ended: its
  worker published `stopped` or `crashed`, or the session was cancelled
  or abandoned. It waits for the signalled worker to publish its end (up
  to 5.5 s: a lookup under way, the drain, a margin; and, for a worker
  deciding how its stream ended, until that decision's own bound, about
  31 s from when it began). How reading ended is fixed when it ends, so
  a stop that arrives during the reader's end-of-stream grace no longer
  skips deciding how the stream ended. A worker still at work at the end
  of the wait is refused (exit 1: "had not finished", run `--stop`
  again), and so is a worker gone without having said how its capture
  ended — killed, or dead before it could, so whether its stream failed
  is unknown (exit 1) — and nothing is saved either way: a summary saved
  then answered at exit 0 while the worker went on to persist a device
  loss. `--stop --abandon` is the way out when this version's worker
  never will say (gone, stuck, or a pid that cannot be identified) — not
  for an earlier version's worker while it may be alive (below): the
  session is
  marked `abandoned` — no meta write of the worker's is applied after
  that — and what was read is saved with `"incomplete": true`, the
  reason beginning "abandoned with --abandon", at exit 0. The worker may
  still append raw event lines after the fence (that file is not
  locked); the abandonment records how much of `events.jsonl` was
  there, to its last whole line, and every answer of the session —
  `--stop`, `--get-details`, `--cluster`, `--raw` — reads that much
  only, so a later `--stop` gives the same answer (it rebuilt the
  summary from the grown file: event count 1, then 2). It never claims
  completeness. A pending
  session (no worker has claimed it) is cancelled: `--stop` saves an
  empty summary marked incomplete and refuses (exit 1), and a worker
  that claims it later is refused. Two `--stop`s of a session that has
  ended give the same answer, and a repeated `--stop` keeps what the
  first said was missing. The summary is saved with `"incomplete": true`,
  and `incomplete_reason` saying why, whenever the capture may be
  missing events: the stream broke or ended by itself unrestarted, a
  record was cut by the stop (`cut_records`), the stream died and was
  restarted (`stream_restarts`: `-T 1` replays only the newest record
  logged while it was down), ANR reports could not be authenticated, or
  the session was abandoned or cancelled — so `--diff` concludes nothing
  from what such a capture lacks. A session whose stream failed is also
  refused (below): the summary is saved, and `--stop` exits 3 or 1.
  Every text form of an
  incomplete summary says so too (`--stop`, `--get-details`,
  `--get-details --cluster N`, whose JSON carries `incomplete`), and "no
  ANR/jank" there reads "… in the part captured". A token budget or
  `--terse` cuts clusters, never a disclosure: the one-line form keeps
  them as a bracketed clause (`[stream broken; 1 record cut by the stop;
  1 stream restart losing a gap; 2 ANR records unverified; 1 pid unnamed;
  1 jank/StrictMode record dropped unnamed; no uid attribution: package
  uids unknown; uid shared with 2 packages; 1 isolated jank/StrictMode
  record not counted; 1 record may predate the session, not counted]`)
- `--diff` never says "no change" about what it could not see: with
  clusters set aside as not comparable, or a session saved incomplete, and
  nothing else changed, the verdict is `inconclusive: …` naming why; a
  verdict that did find a change carries the same caveats after it. A
  cluster one session has and the other, saved incomplete, lacks is never
  called resolved or new — the missing events may simply be unwritten — but
  listed under `unconfirmed` with the session it is in and the one it is
  `absent_from`; what a complete session lacks is still resolved or new.
  So too a session holding evidence it did not count — an `ambiguous_tail`,
  or `unattributed_anrs` — whose missing cluster may be right there: its
  absences are `unconfirmed`, and the verdict names what it did not count
  (a counted ANR against the same ANR held only as either read
  "improvement: 1 resolved"). And a `--package` session that left jank or
  StrictMode uncounted that may be the package's — dropped because
  nothing could show whose it was (`unnamed_dropped`, below), written by
  an isolated process or the package's SDK sandbox
  (`isolated_unattributed`, `sdk_sandbox_unattributed`, below), or under
  a uid the package changed to during the session
  (`package_uid_changed`, below): a jank or
  StrictMode cluster absent from it is `unconfirmed` (the same jank, its
  process started before the capture, read as "improvement: 1
  resolved"), and the verdict says what it left uncounted, even when
  nothing is missing — the uncounted record may be a cluster the other
  session lacks. Its ANR clusters still compare: a dropped jank record is
  no ANR. When one side attributed `--package`'s jank and StrictMode by
  uid and the other did not — a summary an earlier version saved
  (`uid_attribution` absent or `null`), or a session the device gave no
  uids for the package (`"package_uids_unknown"`) — the verdict says so:
  `inconclusive: <id> has no uid attribution (recorded by an earlier
  version, whose --package kept jank/StrictMode only when a Start proc it
  saw named the process)` in place of "no change", the same caveat after
  a verdict that found a change. A
  `--package` summary saved before drops were counted has no
  `unnamed_dropped` (`--stop` of a session an earlier version's worker
  ran gives `"unnamed_dropped": null`, and a `Dropped: not recorded`
  line): what it dropped is unknown, so it is read the same way, the
  verdict saying "`<id>` does not record what --package dropped". A
  session that did not filter by package dropped nothing, recorded or
  not.
  A cluster both hold does not drift either when one side is incomplete —
  its maximum there is only a lower bound — but is listed under
  `not_compared` with `"reason": "not comparable: <id> is incomplete"`;
  so is a jank or StrictMode cluster against a side that left such
  records uncounted (`"<id> dropped N jank/StrictMode records of M pids it
  never named, which --package could not attribute"`, `"<id> did not
  count N jank/StrictMode records of M isolated uids, which may be the
  package's"`, `"<id> did not count N jank/StrictMode records of the
  package's SDK sandbox"`, `"<id> saw the package's uid changed during
  the session (uid N), whose jank/StrictMode went uncounted"`)
- A session keeps the fingerprint version it was captured with — `--stop`
  on an older session does not restamp it — and `--diff` refuses (exit 1,
  naming both versions) two sessions captured under different versions.
  This release's fingerprint is version 3 (an ANR of a window or of a
  process is filed under no package); version 2 filed a WindowManager-only
  ANR under its title's package, so a v2 session is not compared with a
  v3 one. Sessions recorded before versions were kept are version 1
- `--diff --json`: each `drift` entry carries `from_zero`; when it is true
  (the cluster's max duration was 0 in the first session) `delta_pct` is
  `null` — never the non-JSON `Infinity` — and the entry counts as worsened
- A live stream (`--start`, `--watch`) starts at the end of the device's
  buffer (`logcat -B -T 1`, which first prints the one most recent record);
  it used to replay the whole buffer as if it were happening now. **That
  one record may predate the read, so it is shown and never counted**:
  logcat refuses `-T 0` ("-T 0 invalid, setting to 1", recorded), and
  nothing in a record shows it was logged after `--start` — after `logcat
  -c` it is a live one. A session keeps what that record showed as
  `ambiguous_tail` (an `Ambiguous:` line in text), out of `event_count`,
  the clusters and `--diff`: counted, a stall from before the session read
  as its incident, and "resolved" in the next. So start the session before
  the scenario: a record logged just as `--start` returns may be that one,
  shown and not counted. `--watch` prints it with `"ambiguous_tail":
  true`. A session's
  restarted stream skips the records it replays that the session already
  read, whole, and reads one it never did (logged while the stream was
  down); nothing is compared by timestamp, so no clock change moves that
  boundary. `--since` reads its own window
- **On API 33 (Android 13) `--watch` and a session's worker read by
  dumps, never by a stream they stop** -- the read `log_monitor` makes
  there, shared (`common/dump_read.py`). That release's logcat holds `-B`
  records back in a 4 KiB stdio buffer and loses them when its reader
  stops it: recorded on the API 33 emulator
  (`logcat_stream_jank_records`), the fixture app's real jank (30 frames
  skipped) was withheld by a stream stopped just after it, and a session
  over that recording ended `event_count: 0`, `incomplete: false`;
  measured live, a jank line logged 2 s into a 4.5 s session ended
  `event_count: 0`, `total_lines: 0`, and `--watch --duration 6` showed
  nothing, at exit 0. Now the read opens on the newest record a system
  process wrote (a buffer with none gets one marker line, `aes-log-monitor:
  anchor-<128-bit hex>`, said on stderr by `--watch`; a session says it
  as `anchor_written` in its meta counters as soon as the opening ends,
  in its summary (`"anchor_written": true`, an `Anchor written:` line, a
  bracketed mark in the one-line form) and on its `--list-sessions` line,
  even when the session then fails; `false` when it wrote none -- the key
  is in a summary read by dumps only, as in log_monitor's JSON) and, every
  `ANDROID_EMU_LOG_POLL_SECONDS` (1 s, log_monitor's setting), takes what
  logd delivered after the records it has read, found by logd's own order
  -- never by a timestamp or an app's record -- then once more at
  `--duration`, at Ctrl-C, and at `--stop` before the worker publishes
  `stopped`. **Poll latency:** an event is printed (or written to the
  session's events file) about one poll after it is logged, plus the
  dump's own time (tens of ms on the emulator); the read at the end leaves
  none of it behind. Each poll's records go through the same pipeline as
  a streamed record (the assembler, `Start proc` naming, uid attribution
  under `--package`, the frame threshold); a poll that brings none is the
  read going quiet. It reads only what follows its opening record, so
  nothing it reads may predate it and `ambiguous_tail` stays empty there.
  A session's worker claims the session first -- it asks the device
  nothing before -- into a status of its own, `opening`, published with the
  bound it keeps: `ANDROID_EMU_LOG_STATE_TIMEOUT` (15 s) for the level, the
  boot id and the opening search together. One deadline, passed down:
  every question inside -- each dump, the `get-state` after one or after a
  getprop that timed out, the anchor's write, the failure's own question
  of the device -- gets only what is left, and none is asked once nothing
  is. Past it the opening can still take 14 s (a stopped dump's 2 s reap,
  adb's 10 s device listing in a refusal naming several devices, the 2 s
  write that publishes its end), so it stays `opening` at most 29 s with
  its worker running. It leaves by one write of its own: `running` (the
  write that publishes its checkpoint), `crashed` (the opening failed,
  with the reason) or `cancelled` (a stop came first: a dump under way
  stopped and reaped, nothing recorded). `--start` answers success only on
  `running`, waiting exactly that long, so what is logged once it has
  returned follows the opening; past it, it stops the session and refuses
  (exit 1), naming the opening and how long it ran, and a worker gone
  while it opened is refused as `--stop` refuses a worker gone. A stop
  -- `--stop`'s, or `--start`'s past its wait -- is recorded in the
  session's meta under its lock before the worker is signalled
  (`stop_requested_ms`), and the write that would publish `running` reads
  it under the same lock: a stop that takes the lock first makes the
  session `cancelled`, however late the worker handles its signal, even
  with its read already open; a publication that takes it first makes it
  `running`, and the stop an ordinary one. `--stop` waits for an opening
  exactly to the end it published, never longer, and refuses a cancelled
  one (nothing recorded); `--list-sessions` shows `opening` (`gone` once its
  worker is), and `--get-details` and `--diff` refuse it, as a running
  session. `--start`'s own 3 s is for the worker's claim alone. The level is
  asked once per read (`getprop ro.build.version.sdk`); a device that does
  not say is refused by `--watch` (exit 1, with `partial`) and ends a
  session with the reason (`--start` refuses it, its summary saved
  incomplete) -- never guessed to stream. Every other level streams as
  before
  - **A session's checkpoint.** Its cursor -- the system record's frame
    and the frames read after it, base64 -- with the device's serial and
    boot id (`/proc/sys/kernel/random/boot_id`; `ro.boot.bootreason` is
    no id) is kept in the session's meta (`line_counters.cursor`), written
    with every write of the worker's under the session's lock (every 5 s
    while it reads, as it decides how a read ended, and at its end). It
    covers the records read and processed: it moves on only once every
    record before it has gone through the pipeline, and before any write
    publishes it the events file is synced, on every path its end can
    take; a sync that fails, or a closing read that cannot write what it
    still held, puts the checkpoint back to the last one whose events were
    synced and ends the session, incomplete, saying so. A WindowManager
    report held for ActivityManager's (up to 10 s) lives in memory until
    its event is written, so a checkpoint may be past it: the checkpoint
    resumes only the same worker's read, where held reports survive, and
    a crashed or killed worker's session is never resumed from it. Every
    event is written by one function, which counts the held reports it
    could not write. The count is exact when the worker itself ends (any
    failure it can observe): the session ends `crashed`, incomplete,
    with `held_unwritten: N` in its meta counters and summary and "N held
    report(s) were not written" in its reason. When the worker is killed
    (SIGKILL: an unpublished end, `--stop`'s gone-worker answer) or the
    session abandoned (`--stop --abandon` saves what its events file
    holds), reports it held in memory, if any, are lost and their number
    is unknown: the gone-worker answer, the `--abandon` remedy and an
    abandoned session's incomplete reason say "reports the worker held for
    correlation, if any, were not written; their number is unknown". A dump that
    fails with the device still there ends that attempt, not the read: the
    worker publishes `deciding`, asks the device, and restarts the read
    (`ANDROID_EMU_ANR_MAX_RESTARTS`, default 3; counted as
    `dump_restarts`, noted in the events file) from the checkpoint the meta
    holds -- not from memory -- once the boot id says the device is in the
    same boot. The read is one logical read across it: the assembler keeps
    a WindowManager report it holds for ActivityManager's (recorded: an
    input ANR's two reports, 836 ms apart, split across a failed dump are
    one ANR, not two), and the names learned from the log are kept; only a
    lookup the device could not answer is asked again. Nothing is skipped
    as a replay: the cursor excludes what was read, and an app's
    byte-identical resend right after it is a new record. What was logged
    while the read was down follows the cursor and is read, so a restart
    does not make the summary incomplete
  - **What ends a session there**, `crashed` and incomplete with the
    reason -- `--stop` saves the summary, `--diff` concludes nothing from
    it, and `--stop` refuses (exit 3 for a device gone, else 1): the read
    lost its place in logd's buffer (its record turned over, is held
    twice, or is past `ANDROID_EMU_LOG_DUMP_CAP_BYTES`), a dump was not
    records, the device went, the restarts ran out, a restart found
    another boot or no checkpoint, or the events file could not be synced
    (the meta's checkpoint then the last one whose events were). `--watch`
    refuses the same failures
    (exit 3 or 1) with the events it had shown under `partial`, never a
    clean exit
  - Each dump is bounded as `log_monitor`'s are (below). The last read --
    at `--stop`, at Ctrl-C, at `--duration` -- may still ask the device
    who a pid is (each lookup at most 3 s), as any poll does, where a
    stream's drain asks nothing; `--stop` waits up to 5.5 s for the worker to
    publish its end, that read included, and refuses "had not finished"
    past it -- run `--stop` again. The capture contract is log_monitor's
    on that level (`references/known-gaps.md`): what logd delivered, never a certificate
    that the window is complete
- A stream that is not logd's records to its end is never read as if it
  were. A record still arriving when our own stop — `--duration`, Ctrl-C,
  `--stop` — ends the read, on a stream still open or one that ended, is
  lost and said: a `warning:` on stderr for `--watch`, and in a session
  `cut_records` (a `Cut:` line in text). One that ends inside a record by
  itself is typed from adb's own stderr first — a device error adb wrote
  as it ended is exit 3, even when the device is back by the time it is
  asked (it was a framing refusal, exit 1, where `log_monitor` said 3) —
  then makes `--watch` and `--since` ask `adb get-state` again — before
  adb's other exit statuses are judged — and exit 3 when the device went,
  or refuse (exit 1) naming the framing error and how adb exited. Bytes
  that are not records at all are refused (exit 1) with no device
  question — after what adb has said, read once adb has had 0.5 s to
  finish (the session worker's grace too), which may name a device error
  (exit 3). adb's stderr is read from its start and its end (64 KiB
  each): an error adb wrote first and then chattered past was lost with a
  tail alone. What lies between the two is scanned line by line as it is
  read, not kept: only adb's own lines (`adb: ` / `error: `, the lines
  the device-error classification reads) are, up to 64 KiB of distinct
  lines, so a device error anywhere in the stream is typed — unless more
  than that much of distinct adb-prefixed lines, or a single line over
  64 KiB, comes before it — exit 3 where, between the two windows of more
  than 128 KiB, it was exit 1 once `adb get-state` found the device back.
  A message shows adb's stderr shortened.
  A session's worker does not restart either kind of
  stream: it keeps each stream's stderr (it went to `/dev/null`) and reads
  what adb has said whether or not adb has exited, takes a device error
  adb wrote as the answer, and asks the device only without one; the
  session is marked crashed, and `--stop` saves what was read
  (`stream_failure` in the summary, a `Broken:` line in text, readable
  with `--get-details`, saying how adb ended it) and then refuses with
  that answer, raised again as the error it was — exit 3 when the device
  went, 1 for several devices and no `--serial` (naming the serials to
  pass) or anything else. A session's stream that ends by itself, whole,
  is asked about the same way: a device error — adb's words, or `adb
  get-state`'s — ends the session as that error (exit 3 at `--stop`);
  otherwise — the device there, or `adb get-state` unanswered — the
  stream is restarted (`ANDROID_EMU_ANR_MAX_RESTARTS`, default 3) until
  no restart is left, when the session ends as a stream failure `--stop`
  refuses (exit 1, saying how adb ended it). Either used to be restarted,
  then saved as an incomplete summary at exit 0. A stop that comes before
  a restart that was due saves the summary incomplete (what was logged
  after the stream ended is missing). Each stream's process is ended
  before the next starts
- `--package PKG` keeps an event only when its package is exactly PKG, by
  the identity the reader established: the package ActivityManager's ANR
  report names (or its component's), or, for jank and StrictMode, the
  record's uid. It used to match substrings of the message and tag, and
  the package's last segment alone, so the recorded Choreographer text
  "The application may…" passed `com.example.app` and
  `com.example.application` alike. **A jank or StrictMode record is the
  package's when its uid is one of the package's uids** — in every mode,
  `--watch`, `--since` and a session, and whether or not the process
  started while the read ran. Choreographer and StrictMode write from
  inside the app, and logd stamps each record with the writing process's
  uid, which no app can choose (recorded: every jank record of the
  fixture app on API 35 carries its uid 10205, every StrictMode record on
  API 33 its uid 10174). The package's uids are `cmd package list packages
  -U <package>`'s, one per user it is installed for, each kept whole: the
  package's uid in a second user counts, the same app id in a user it is
  not installed for does not. The event then reads `"app": "<package>"`
  (a session keys its jank by the package, so a restart or a warm start
  is one site). A record another uid wrote is not the package's and is
  dropped, as another package's ANR is. What it cannot decide, or decides
  with a doubt, is said:
  - **`shared_uid`**: when the device's whole uid table (`cmd package
    list packages -U`, unfiltered, asked once) gives the package's uid to
    other packages too (sharedUserId), their jank is the same uid's: it is
    counted, and the summary names them — `"shared_uid": ["<other>",
    ...]` (`null` when the table could not be read), a `Shared uid: the
    package's uid is also <others>'s (sharedUserId), so jank/StrictMode
    counted for it by uid may be theirs.` line in text. `--watch` /
    `--since` put `"shared_uid"` on each such event (`[uid shared with
    …]` in text) and warn on stderr
  - **`isolated_unattributed`**: an isolated process
    (`android:isolatedProcess`) or one an app zygote spawned runs under a
    uid of its own (app id 99000–99999 or 90000–98999) that no package's
    listing names, so its record may be the package's or any app's in
    that user. It is never counted: `"isolated_unattributed": {"records":
    N, "uids": [...]}` in the summary (records at or above the frame
    threshold), an `Isolated:` line in text, `warning: --package did not
    count N jank/StrictMode records from isolated processes (uid …)` on
    stderr from `--watch` / `--since`, and `--diff` concludes nothing
    about jank from such a session (above)
  - **`sdk_sandbox_unattributed`**: the package's own SDK sandbox (the
    Privacy Sandbox process that runs its SDKs) has the package's uid
    plus 10000 in the same user (app id 20000–29999). Its records are the
    SDKs', not the app's, and no listing names the uid: never counted,
    never dropped unsaid — `"sdk_sandbox_unattributed": {"records": N,
    "uids": [...]}`, an `SDK sandbox:` line in text, `warning: --package
    did not count N jank/StrictMode records from the package's SDK
    sandbox (uid …)` on stderr, and `--diff` concludes nothing about jank
    from such a session. Another app's sandbox is that app's (the mapping
    is fixed) and is dropped as another uid's. Dropped silently before,
    a sandbox's jank read as "improvement: 1 resolved"
  - **The package's uids are asked once per read** (`--watch`, `--since`,
    a session) and kept, as is the whole uid table. Installing,
    reinstalling, or adding a user for the package during a read gives it
    a uid that answer lacks, and its records then read as another uid's:
    start a new read after any of those. When system_server starts a
    process named exactly the package under such a uid, it is said —
    `warning: system_server started a process named <package> under uid
    N, which the package's uids as this read asked them at its start do
    not include … -- start a new read.` on stderr from `--watch` /
    `--since`; `"package_uid_changed": [N]`, a `Uid changed:` line in
    text, and no jank conclusion in `--diff` from a session. (Another app
    may name its process after the package too, which that sentence
    cannot rule out.)
  - **`unnamed_dropped`**: when the device did not give the package's
    uids, no record can be shown to be the package's — nor can a `Start
    proc` name its process — so every jank and StrictMode record is
    dropped, counted, never dropped unsaid: `"unnamed_dropped":
    {"records": N, "pids": [...]}` and `"uid_attribution":
    "package_uids_unknown"` in the summary, `Dropped:` and `No uid
    attribution:` lines in text, `warning: --package dropped N
    jank/StrictMode records` on stderr from `--watch` / `--since` (their
    text summary says so too), and `--diff` concludes nothing about jank
    from such a session (above)
  - **`uid_attribution`**: a session summary says how it attributed —
    `"uid"` (by each record's uid), `"package_uids_unknown"`,
    `"unfiltered"` (no `--package`), or `null`: not recorded, a summary
    an earlier version saved, which `--diff` names (above).
    `"events_without_uid": N` counts the jank/StrictMode events written
    with no uid (an earlier version's worker), a `No uid:` line in text
- Legacy: `--watch [--duration N]`, `--since 5m`. A logcat read that adb
  ends with a failure is a refusal, not "no ANRs": exit 3 when the device
  is unavailable (an unknown `--serial` is caught before streaming), 1
  otherwise, typed from adb's stderr as adb wrote it. A live stream ends
  only when it is stopped, so one that ends by itself — whatever adb's
  exit status, or none — makes `--watch` ask `adb get-state` again: exit 3
  when the device went, otherwise a refusal (exit 1), never success with
  nothing printed. A `--since` dump that hits its 60 s bound asks the same
  question first: adb waits rather than fail for a device that went
  ("- waiting for device -"), so the bound is how such a dump ends
- `adb logcat` does not fail when its device is not there — it waits — so
  `--watch`, `--since` and `--start` ask `adb get-state` first (bounded,
  with or without `--serial`) and refuse at once, exit 3, when adb names
  no device in state `device` (several and no `--serial` is exit 1);
  `--start` creates no session then. `--start` succeeds only on a
  worker that has claimed the session and is `running` — which holds no
  failure, since a failure is published only with the session's end. A
  worker deciding how its stream ended is waited for; one that registers
  and ends before `--start` answers — its stream broke at once — is not
  "did not register" (exit 1, the race CI met): `--start` stops the
  session, so its summary is saved, and refuses with what ended it, as
  `--stop` would (exit 3 for a device that went). A worker that has not
  claimed the session within 3 s is given up on: the session is
  cancelled (exit 1), so a worker that starts late records nothing into
  it. A worker that meets an error nothing foresaw ends the session
  `crashed` with it as the stream failure — beside the stream's own
  failure when there was one, not in its place — where it left it
  `running` with nothing said. Sessions written by v0.8.0 main load
  unchanged (the same files and keys; this version only adds keys): a
  stopped one is answered as saved, a crashed one is refused as before,
  and a `running` one whose worker is gone is refused with `--abandon`
  named, since that version's worker never said how a session ended
- **Compatibility boundary.** Upgrade quiescently: stop every ANR session
  with the old version before installing this one. A worker of v0.8.0
  main before the lifecycle landed (fb5de9c and earlier: its session has
  no `lifecycle` key, which is all the fence keys on) still recording is
  fenced only by waiting for it (above), and a
  `--start` of that version still in flight can claim a session this
  version cancelled — that version's claim checks nothing. Sessions this
  version writes are not for older binaries (no forward compatibility),
  and two versions writing one session is unsupported. That includes
  v0.8.0 as released: its worker writes `lifecycle` 2 into its meta, so
  the fence above cannot see it, and it locks `<session>/lock`
  inside the session's directory where this version locks
  `locks/<session>.lock`, so nothing excludes the two — stop its sessions
  before upgrading too. An older version's prune also takes `locks/` for a
  session with no meta and deletes it; this version makes it again, but a
  lock held at that moment then excludes no one. A `lock` file left in an
  old session's directory is just a file now. While an earlier
  version's worker may be alive or cannot be identified, `--stop` and
  `--stop --abandon` both refuse; with `ps` unavailable and the pid
  present that lasts until `ps` works again or the process exits —
  otherwise the only recovery is outside the skill: delete the session's
  directory once you have established yourself that the worker has ended
  (`--clear-sessions` keeps it, for the same reason). A session of the
  earlier version whose worker is gone answers from the summary it saved;
  run `--stop` first to rebuild it from what that worker persisted
- `ANDROID_EMU_ANR_MAX_RESTARTS`, `_TOTAL_CAP_MB`, `_DEFAULT_TOP_N` and
  `_BUDGET_TOKENS` take `0` (no restarts, no cap, no top-N cap, no budget);
  it used to be raised to 1. A restart happens unless adb or `adb
  get-state` names a device error; with `0`, a stream that ends by itself
  ends the session at once

## Where the code is

The clustering and session machinery lives in `common/anr_pipeline.py` and
`common/anr_sessions.py`.
