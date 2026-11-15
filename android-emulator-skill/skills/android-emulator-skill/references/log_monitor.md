# log_monitor.py

Real-time logcat monitoring

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Reads logd's records (`logcat -B`), never text: each record whole, with
  the pid and uid logd took from the writing socket's credentials -- the
  only metadata an app cannot write (the tid and time are the writer's
  own). In text, a tag an app chooses can hold a newline and a whole
  header, so one app record printed a second line "from" system_server.
  Each record is printed back as `-v threadtime -v usec` would print it,
  in this host's local time, with the control characters of its tag and
  the line-ending and terminal controls of its message escaped (`\r` as
  `\\r`, ESC as `\\x1b`; TAB is kept) -- so what it prints or saves reads
  back as the same lines in any reader
- On API 33 (Android 13) a live read is made of dumps, not a stream:
  that release's logcat writes `-B` records through a 4 KiB stdio buffer
  and flushes only after a text record, so a stream stopped by its reader
  loses what it held back (recorded on the API 33 emulator: a quiet 5 s
  stream delivered no byte; `--duration 3s` was `total_lines: 0`, a
  success). There a capture is a series of dumps, nominally one poll
  apart (a read that takes longer than a poll is followed at once by a
  catch-up one): the read opens on the newest record a system process
  wrote (`logcat -d -B -T 1`, the tail doubled until it holds one: the
  anchor; what followed it was logged before the read opened and is not
  the window). When the buffer holds no system record to anchor on --
  empty or only apps', mostly after `--clear` -- log_monitor writes one
  marker line to the device's log, `adb shell log -t aes-log-monitor
  anchor-<128-bit hex>` (as shell: a frame no app can write, made unique
  by the nonce), finds it by that nonce (the tail doubled from 1; not in
  the whole buffer: refused) and reads what follows it; it says so on
  stderr, in the text summary (`Anchor written:`) and as `anchor_written:
  true` in the JSON (`false` when it wrote none; the key exists on API 33
  only). No read continues from apps' records alone: copies of one
  datagram can reproduce them byte for byte. Then every
  `ANDROID_EMU_LOG_POLL_SECONDS` (1), whatever
  the mode, takes what logd delivered after the records it has read, and
  once more at the end of a `--duration` or at Ctrl-C -- printing as it
  goes under `--follow`. The read is positioned by logd's order from the
  newest system-written record it has reached, whose identity an app
  cannot forge; timestamps are not used to position it. A system record
  is one whose uid -- logd's, from the writing socket's credentials, not
  the writer's say -- is below 10000 in its user (`uid % 100000`): root,
  system_server, the daemons, and shell (2000), which `adb shell log`
  writes as. An app can send one datagram twice, time and payload
  included, and logd keeps two byte-identical frames; so an app's record
  never places the read. Each read dumps the newest N records (`logcat
  -d -B -T N`, N at least 64 and enough for twice the last read's new
  records), finds that system record by its frame byte for byte, checks
  the records it had read after it are next, frame for frame, and keeps
  everything after them; when the N do not reach it, N doubles and the
  dump is asked again. The newest system record in the dump is the next
  read's place; with none among the new records, the place stays and
  the new records join what it checks. Without a new system record, the
  remembered suffix and the required dump grow until turnover or the
  byte cap refuses continuation. An app resending its own records
  -- the anchor's neighbours, or all of them -- adds records the read
  takes; it cannot move the read. A record's time is its writer's (a
  native app can stamp one anywhere, and the device's clock can step), so
  no stamp and no clock step moves the read or hides a record from it,
  and the device's clock is never asked. That system record found twice
  or more in a dump: where to resume is a guess, refused -- loud, never
  silent, and only possible if a system writer duplicated a frame, which
  system writers do not do. Trusted writers must not replay complete
  frames; duplicate detection covers copies present in the searched dump.
  Absent from a dump that came back with fewer
  than N records -- the whole buffer -- or a record read after it missing
  or different: logd's buffer turned over past it, refused. A doubled
  tail that would pass
  `ANDROID_EMU_LOG_DUMP_CAP_BYTES` (twice the last dump's bytes over the
  cap) is refused before it is asked. Every dump ends the same way, in
  one order: stopped if still running (killed, then reaped within 2 s;
  on an interruption, the child is stopped before the interruption
  propagates, or stderr says it could not be); the last 64 KiB of its
  stderr read; a device error in adb's own words there -- whatever its
  exit status -- is exit 3; then `adb get-state`, which proves adb still
  sees the device (gone or not in state `device`: exit 3; the question
  itself failing: exit 1); only then the verdict -- could not be
  stopped, waiting for it failed, its stderr could not be read (adb's
  own words then go unchecked, and the refusal says so), output past the
  cap, past its deadline, a non-zero exit, its output could not be read:
  refused (exit 1). Every refusal carries what was read under `partial`.
  The records found prove logd still held them when that dump began;
  they do not prove the window is complete: a capture is what logd
  delivered, and logd may drop records it prunes while any reader is
  behind (true of every logd reader, streams included). Zero lines means
  no record logd delivered after the anchor passed the filters (`--app`,
  `--severity`). The bounds, each separate: each dump of the opening
  search gets at most 15 s and never more than the `--duration`, the
  marker write (when there is one) at most 15 s; each poll's dump's own
  deadline is 5 s plus the seconds since the last read (about one poll),
  at most 60 s, the same for every doubling within one read; its output
  is checked against `ANDROID_EMU_LOG_DUMP_CAP_BYTES` (32 MiB) every 50
  ms -- a detection threshold, not a maximum: the file can overshoot it
  by what adb writes in that time and before the kill lands, and is then
  never read; a dump still running at its end gets 2 s more to be
  reaped; and before any verdict, `adb get-state` bounded by
  `ANDROID_EMU_LOG_STATE_TIMEOUT` (15 s) -- plus, when adb's stderr names
  a missing device, the bounded `adb devices` its message lists the
  attached devices from. The per-dump check proves adb still sees the
  device, not that logcat works on it; a logcat that stops answering is
  refused by the dump's own verdict. Every other level streams, as
  before. The level is asked once (`getprop ro.build.version.sdk`),
  before `--clear` or any other logcat call; a device that does not
  answer a number is refused (exit 1), not guessed to stream. This read,
  its bounds and its five-step dump ending live in
  `common/dump_read.py`, shared with `anr_watcher`'s live readers on API
  33
- A stream that ends inside a record is never a complete capture: on a
  `--last` dump, or a live read that ended by itself, the device is asked
  again (gone: exit 3) and otherwise the capture is refused (exit 1). Only
  when the read was stopped by its own `--duration` or an interrupt is
  the record that was arriving dropped -- and counted
  (`statistics.cut_records`, `attribution.complete: false`, a stderr
  note, an `INCOMPLETE` line in the text summary)
- Filter by app package: the records of each process the device vouches
  was the app's, inside that process's life, that logd stamped with one of
  the package's uids. A pid counts when its uid is the package's: the one
  `pidof` names (its uid checked with `ps`), or one system_server's
  `Start proc` / `Killing` record names with the package's uid (`has
  died`, which has no uid, only ends a life). Only system_server's own
  records count (logd's pid, checked against `pidof system_server`), and
  only a sentence that opens the record's message; unknown system_server
  or uid, nothing counts. A crash's own `Process: <pkg>, PID: N` record is
  not evidence: any app can write one. So a crash before a restart stays
  in the window (`app_pids`, and `attribution` -- what decided it -- in the
  JSON). A live read holds back the package uid's records of a pid not
  yet named, keeps them if it is (stored in input order; marked `[held
  back]` under `--follow`), up to `ANDROID_EMU_LOG_PENDING_CAP` (2000)
  lines, and counts what it dropped. An app with no process in the window
  is refused, not widened to the whole device
- Says when the window is incomplete: records logd stamped with the
  package's uid whose process nothing in the window ties to the package
  -- a process that started before a `--last` window and is gone by its
  end, such as a crash read too late (`has died` names no uid) -- are
  left out, counted and named (`attribution.unresolved_lines` /
  `unresolved_pids`, `attribution.complete: false`, a stderr note, and an
  `Attribution: INCOMPLETE` line in the text summary). Counts are of what
  is shown; widen `--last` to include the start
- A live read (`--follow`, `--duration`) starts at the end of the device's
  buffer (`logcat -T 1`, which first prints the one most recent record,
  every line of it) and reads what happens from then on; it used to replay
  the whole buffer first. Use `--last` for history. A process already
  running when a live read begins counts from the first record the read
  sees, so a replayed buffer could hand it the older records of another
  process that had the same pid
- A device that is not there is never a quiet capture: every mode asks
  `adb get-state` first, and a live read that ends by itself asks again.
  (A streaming `adb logcat` aimed at a device that is not attached waits
  instead of failing, so `--duration` used to report it as zero lines.)
  Unavailable is exit 3, as everywhere else
- A live read (`--follow`, `--duration`) ends only when it is stopped: by
  its `--duration`, or an interrupt, and either is the capture. One that
  ends by itself -- whatever adb's exit status, cut or not -- cannot show
  it observed what was asked: exit 3 when the device went, otherwise a
  refusal (exit 1) that keeps what it read, its line count in the error
  and its JSON summary under `partial` (`--json`). It used to be the
  capture, with a note on stderr
- A `--last` dump has a deadline of its own: twice `ANDROID_EMU_ADB_TIMEOUT`
  (60 s by default). A dump that has not ended by then -- a device lost after
  the first question (`logcat -d` waits for it too), a stream that stalls
  mid-record -- is stopped, the device is asked again (gone: exit 3), and
  otherwise the dump is refused (exit 1), never reported as the window. It
  used to wait for as long as the stall lasted. `--follow` (and no mode
  flag) reads until interrupted
- Identity is the uid (every user's uid for the package). A process
  another package runs under the same shared uid, named after this
  package, counts as this app's: the device does not tell them apart
- Filter by severity: `error`, `warning`, `info`, `debug`, `verbose`; any
  other name (`warn`) is a usage error (exit 2). The floor is applied on
  the host (`-B` ignores filterspecs), after attribution, so
  `--severity error` still sees the records that name the app's processes
- Smart deduplication. In the JSON, `statistics.errors` /
  `statistics.warnings` count LINES (E/F, W); `statistics.unique_errors` /
  `unique_warnings` count distinct messages; the top-level `errors` /
  `warnings` are those distinct messages as a list, capped at
  `ANDROID_EMU_LOG_JSON_CAP` (20)
- Duration-based or follow mode. `--duration` / `--last` need a unit
  (`30s`, `5m`, `1h`); a bare number is rejected as a usage error (exit 2)
- Save logs to file
