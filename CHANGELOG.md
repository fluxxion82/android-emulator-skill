# Changelog

Release history of the Android Emulator Skill. The entries for v0.9.0 and
v0.8.0 were moved here from the skill's `SKILL.md` (its "Status & Roadmap"
section) word for word; their figures and their "above" / "below" are as that
file stood at v0.9.0 (f9ab727). Earlier releases are described in the release
notes on GitHub. What the skill does not do yet is kept with the skill, in
`android-emulator-skill/skills/android-emulator-skill/references/known-gaps.md`.

## v0.9.0

**v0.9.0 — most known gaps closed, instrumented tests added.** v0.8.0 closed
with a Known-gaps list.
This release removes the gaps a test can pin and states the ones it keeps. Each
removed gap is pinned by a named test that is red on v0.8.0's status commit
(f69c236) and green here; see the v0.9.0 release notes for the list.

- **Fixture-policy debt is zero.** `KNOWN_VIOLATIONS` in
  `tests/test_fixture_policy.py` is empty (v0.8.0 left two entries): no test
  feeds a long inline string of tool output to a parser, and the ratchet fails
  the next one that does.
- **Three recorded profiles carry most of the suite.** `emulator-api35` (157
  fixtures, an Android 15 emulator) is the primary profile; `emulator-api33`
  (67, an Android 13 userdebug emulator) and `gradle-8.13` (35, Gradle 8.13 /
  AGP 8.13.2 scaffolds) were recorded for this release. Three smaller profiles
  from earlier releases still feed tests: `pixel4xl-api33` (15, a phone),
  `gradle-7.5.1` (5) and `gpxpy-1.6.2` (2). `emulator-api33`
  recorded what API 33 does differently and what v0.8.0 had not recorded:
  logcat holding `-B` records back from a live stream (so `log_monitor` and
  `anr_watcher` read live by dumps there), the uid and process tables behind
  `anr_watcher --package`'s attribution by uid, two-user `dumpsys package` and
  `pm grant --user` output for `privacy_manager --user`, a `--cat` read past its
  cap, adb echoing agent-supplied text on stderr, and the offline and
  unauthorized device states.
- **Instrumented tests are a feature (#64).** `build_and_test --instrumented`
  runs the connected test task on one device and one variant per invocation.
  The device is resolved before Gradle starts (none attached: exit 3; several
  and no `--serial`: exit 1) and handed to Gradle as `ANDROID_SERIAL`; the
  verdict is read from that run's connected JUnit XML. Verified live in the
  emulator lane.
- **A per-item batch is one object (#57).** `status_bar`, `appearance`,
  `privacy_manager` grant/revoke/reset and the `emulator_*` `--all` / `--old`
  modes answer one object whose `items` each carry `success`, `error` and
  `remedy`; exit 0 only when every item succeeded. Breaking: they printed
  `results` (`emulator_shutdown`, counts only). Usage errors joined the
  schema in #56 (exit 2, one object under
  `--json`). The Failures entry's "Not in the schema yet" paragraph is gone.
- **`anr_watcher` is Unix-only (#61).** Its session storage locks with
  `fcntl.flock`; on a host whose Python has no `fcntl` (Windows) every mode
  refuses at once (exit 1) rather than run unlocked, and `logs anr`, which
  runs it, refuses the same way. `android_health_check.sh` needs Bash, and
  `build_and_test.py` always runs the Unix `gradlew` wrapper, never
  `gradlew.bat`. Nothing is run on Windows in CI, so Windows is untested, not
  supported (Known gaps).
- **`container --get-cached ID`** prints the content a `--cat` above 8 KiB
  cached. The id that `--cat` printed had no reader before. A reader is
  refused an entry of another script's type before that entry's age is
  looked at, so `container`'s one-hour reader cannot expire and delete a
  build `build_and_test` still keeps for a day; `container` clears only its
  own expired entries.
- **`android_health_check.sh` refuses an argument it does not take** (exit
  2, the one object under `--json`) before it needs any tool; it ignored
  one. The usage sweep now covers every script on disk, the shell script
  included.
- **`test_recorder` discloses a damaged `steps.jsonl` (#67).** `--stop`,
  `--get-details` and `--list` say the recording is incomplete and count
  what was lost, and `--stop` exits 1; damaged lines were skipped silently.
- **`model_inspector --schema` is tested on a schema Room exported (#69).**
  It reports views and FTS tables, and `--raw` takes a table or view name.
  Breaking: `entity_name` is gone from `--json`; Room's export never names
  the class.
- **The test toolchain is pinned (#68).** `dev-constraints.txt` states each
  version once; every workflow install and the documented commands take
  theirs from it, and a test fails one written any other way.
- **`localization_audit` compares `<plurals>` placeholders item by item
  (#71).** A language with more plural categories than English (Serbian,
  Arabic) is no longer a mismatch; an item that drops or adds an argument
  still is, and the finding names its `quantity`.

Tests: 3,110 → 4,916 passed at the release commit, 0 xfailed; 13 skipped
(a per-profile check for a fixture `emulator-api33` did not record, and one
keyboard case the primary profile cannot exercise) and 28 deselected (the
emulator lane's `-m emulator` tests).

## v0.8.0

**v0.8.0 — the diagnostics stop lying.** v0.7.0 proved the agent loop closes
from the skill's own output. This release goes after the numbers an agent
reads: a verdict, a count or "nothing found" that the device did not actually
produce. Each fix below started as a strict-xfail spec written against a
recorded fixture, and was red before the fix.

- **One error boundary for every script** (`common/cli.py`). A refusal is one
  `{"success": false, "error", "remedy"}` object under `--json` (or
  `Error:`/`Remedy:` on stderr), and exits 1, or 3 when the device is
  unavailable. An AST guard enumerates all 32 Python scripts, and the exit-code sweep
  asserts the payload of 40 documented modes twice (39 at this release;
  v0.9.0 added the health check's): once with every tool failing and once
  with none installed.
- **"Could not look" is no longer "nothing there."** `emulator_create`,
  `emulator_erase`, `device_list` and `android_health_check.sh` refuse a
  missing or failing SDK tool or AVD home instead of returning an empty list.
  `emulator_create` defaults to the host's ABI, never skips its image check,
  and reads both `sdkmanager` formats. The Java tool's pipe table had parsed to
  nothing, which kept the lane's AVD lifecycle test skipped on every CI run.
- **Build verdicts come from this run's tests.** `build_and_test` deletes the
  test task's old JUnit reports, reruns the task, and passes only on Gradle
  exit 0 with no `<failure>`/`<error>`. A run that wrote no results is refused,
  never reported as "0 passed". A Gradle older than 7.6 is refused by name.
- **Log windows belong to the app.** `log_monitor --app` and
  `app_state_capture` keep only lines from processes shown to be the app's,
  including a crashed one that has since restarted. A pid counts only on
  evidence that system_server wrote and whose uid matches the package; both
  read logd's records (`-B`), so no tag or message an app writes can forge
  attribution. The price is coverage: a process the window saw no such
  evidence for -- one started before the read, a private `pkg:name` process
  -- is left out, and the window says so (`attribution.complete: false`,
  `unresolved_pids`) rather than guessing. `--severity` validates its names,
  and counts are line counts. Snapshots no longer land in the caller's working
  directory.
- **One ANR is one event.** `anr_watcher` reads logd's records too, so the
  `Reason:`/`PID:` lines arrive inside their report, and a WindowManager-only
  ANR still counts, but only as system_server wrote it — the record's pid,
  which no tag can forge — and as an ANR of a window: the window's title is
  the app's text, so it names, classifies and settles nothing. A window's
  report that ActivityManager's report of the same broadcast ANR may already
  account for is counted and marked `possible_duplicate`, never merged on
  the strength of a title. An ANR is
  filed under a package only when its process is shown to run as that
  package's uid (ActivityManager names the process, which an app may name
  after another package). Jank is keyed by process, and a pid is named only
  from system_server's own `Start proc` record for it, seen in the same
  stream; `--package` matches that identity whole (since v0.9.0, jank and
  StrictMode are the package's by each record's uid instead). On the stream
  path (every level but API 33), a session's capture losses — a record cut by
  the stop, a stream restart's gap — mark it incomplete, and the one record
  `-T 1` replays when it starts is shown, never counted. On API 33 (since
  v0.9.0) the session reads by dumps from a cursor that excludes its opening
  record, so nothing is replayed (`ambiguous_tail` stays empty), a failed dump
  retried from the checkpoint within the same worker is counted in
  `dump_restarts` without marking the summary incomplete, and a lost cursor or
  another boot ends the session incomplete. `--diff` calls nothing resolved,
  new or stable on the strength of a side that may lack it — saved incomplete, or holding
  evidence it did not count (for jank and StrictMode, what `--package`
  dropped for want of a name, or did not record) — and says so in its
  verdict: "inconclusive" when nothing else changed, the change with its
  caveats otherwise. It never emits `Infinity`. The live readers no longer
  swallow adb's exit status: losses are disclosed; a `--watch` stream that
  ends by itself is a refusal, and a session's is restarted, while a restart
  is left, unless adb or `adb get-state` names a device error, then refused
  at `--stop` — adb's own words typed first (a device error is exit 3), in
  `--watch`, `--since` and the session worker alike. A session's lifecycle
  is serialised under a per-session lock (meta and summary; events are
  appended unlocked), each transition owned by the worker or the CLI and
  applied only from the state it expects: the worker publishes how it ended
  before it exits (unless it cannot have the lock in 5 minutes),
  `--stop` answers with a summary only once the session has ended
  (otherwise it refuses, exit 1, and `--stop --abandon` saves what was
  read, marked incomplete, its events frozen at the abandonment), and
  `--start` succeeds only on a worker that is recording. A worker of v0.8.0
  main before this lifecycle (fb5de9c and earlier: a session with no
  `lifecycle` key), which takes no lock, is waited for until it is gone
  before its session is answered.
- **App files are read honestly.** `adb exec-out run-as … cat` exits 0 with
  the error text as content (measured), so `container` and `model_inspector`
  now read through `adb shell -T` and refuse an adb client too old to honour
  it. Device file names are treated as untrusted input: directories are
  listed as NUL-terminated records (toybox find), so a name holding a newline
  cannot forge or hide an entry; unsafe, undecodable or CR/LF-carrying names
  are refused and reported, and exports stay inside their directory.
- **The required emulator lane can no longer pass without testing.** It fails
  on any skip, exits 3 with diagnostics when the device is lost, retries only
  that case once, and counts the retry in the step summary.

Tests: 1,653 → 3,110, and 0 xfailed. Fourteen fix PRs landed between the
version bump and publication, each from an independent review of the merged
code; the entries above describe what shipped. Fixture-policy debt is down to
two entries, and the boundary guard's debt list is empty.

The correction remains fixture-driven: `tests/fixtures/recorded/` holds verbatim
output captured from real devices on two API levels, parser tests consume it,
and a known defect, when one exists, is pinned with `xfail(strict=True)` so
fixing it forces the marker's removal. None is pinned in v0.8.0. See
`CLAUDE.md` in the source repository.
