# Known gaps

What the skill does not do yet, or does with a limit that is stated rather
than closed. The script index is [SKILL.md](../SKILL.md), each script's detail is
the file beside this one named after it, and what a failure looks like is
SKILL.md's **Failures** section.

- Windows is untested. No CI job runs there; `build_and_test.py` always runs
  the Unix `gradlew` wrapper (never `gradlew.bat`), `android_health_check.sh`
  needs Bash, and `anr_watcher.py` is Unix-only (macOS, Linux) as a whole: its
  session storage locks with `fcntl.flock`, and on a host whose Python has no
  `fcntl` (Windows) every mode, `--since` included, refuses at once (exit 1,
  the one refusal object under `--json`). Its live readers differ by level: on
  API 33 they read by dumps through temporary files, with no `select()`; on
  every other level they stream, waiting on adb's pipe with `select()`, which
  on Windows takes only sockets. Whether the remaining scripts work there has
  not been observed.
- `accessibility_audit` has no contrast check. Contrast needs pixel sampling
  from a screenshot, which is not implemented.
- `anr_watcher --package` cannot attribute jank or StrictMode written by an
  isolated process (`android:isolatedProcess`), one an app zygote spawned,
  or the package's SDK sandbox: each runs under a uid of its own that no
  package's listing names, so its records are listed under
  `isolated_unattributed` / `sdk_sandbox_unattributed`, never counted for the
  package, and `--diff` concludes nothing about jank beside them. A coverage
  limit, said rather than closed. Unrecorded end to end: no recorded process
  table holds such a process, so the classifier is tested on uids derived
  from the package's recorded uid and AOSP's app-id ranges.
- `anr_watcher --package` asks the package's uids (and the whole uid table)
  once per watch or session and never refreshes them: install, reinstall or
  add a user for the package during a read and its records under the new
  uid go uncounted. A `Start proc` naming the package under such a uid is
  said (`package_uid_changed`), but one the read never sees — lost in a
  restarted stream's gap — is not; start a new read after changing the
  install.
- A capture by `log_monitor`, or by `anr_watcher`'s live readers on API 33,
  is what logd delivered, never a certificate that the window is complete.
  logd may drop records it prunes while a reader is
  behind -- Android 13's buffer prunes while it flushes to a slow reader
  and does not disconnect it -- on every level and for every reader, stream
  or dump; nothing on our side can tell a pruned record from one never
  logged. On API 33 the dump-made read (described in
  [log_monitor.md](log_monitor.md)) adds its own limits: the
  records it continues from show only that logd still held them when the
  next dump began, and only in their own buffers (main, system, crash and
  kernel each turn over on their own, so another one turning over within a
  poll interval loses records unseen -- on a busy device enlarge its rings
  with `adb logcat -G`); a buffer that turns over past a record the read
  already took after its system record is refused although nothing unread
  may be lost; without a new system record, the remembered suffix and the
  required dump grow until turnover or the byte cap refuses continuation;
  and a buffer with no system record at the open gets a marker line written
  into it (see [log_monitor.md](log_monitor.md)), so `--clear` on API 33 leaves one
  `aes-log-monitor` line
  in the device's log. Android 13's `SerializedLogBuffer::Log` numbers a
  record before it takes `logd_lock`, so a record from logd's own kernel
  (LogKlog) or audit (LogAudit) thread can be stored behind records a read
  already took -- the window is the time between numbering and storing:
  inside the remembered suffix that is a loud lost refusal; just before the
  cursor that one record is not captured. Listener writers -- every app and
  system process -- keep their order among themselves, but under the same
  race one of their records can also land before a kernel or audit cursor
  and be missed; the default buffer selection is kept, as the stream path
  reads it. The read is placed by a
  system-written record, so an app resending a record
  -- byte-identical, with or without pruning -- can no longer make it
  resume from the copy: the earlier limit for a resend is gone. It follows
  logd's order, not timestamps, so a writer whose clock is far behind (or
  ahead), or a device clock that steps, no longer opens a gap either.
- Without `--package`, an ANR session names only a process started while it
  runs (system_server's `Start proc` is the only evidence it takes); an app
  already running when `--start` is called is keyed by pid, listed under
  `unnamed_pids`, and `--diff` does not compare its jank. Under `--package`
  its jank and StrictMode are the package's by uid.
- `build_and_test --instrumented` runs one device and one variant per
  invocation, and one module when `--module` is given (without it the task
  runs in every module, and every module's connected reports for the
  variant are read): no orchestrator, no sharding, no `am instrument -r -w`
  protocol -- Gradle's JUnit XML is the one source. Its results layout is
  recorded for an unflavored and a one-dimension flavored variant on AGP
  8.13.2; any other layout is not guessed at, so its run is refused as no
  test results rather than read from somewhere it did not clean.

Removed rather than repaired: `clipboard.py` — see "Platform limitations
worth knowing" in [SKILL.md](../SKILL.md); writing the clipboard is not
reachable from adb on any modern Android, so the script could not be made to
do what it advertised.
