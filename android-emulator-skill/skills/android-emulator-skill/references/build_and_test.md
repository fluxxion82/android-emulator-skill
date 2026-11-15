# build_and_test.py

Gradle build/test automation with progressive disclosure (backed by the `gradle/` subpackage)

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- One-line summary + a result ID by default; drill in on demand
- Parses JUnit XML for pass/fail counts + failed test names; extracts Gradle/Kotlin/Java errors & warnings
- A Gradle failure's `location` (`file`, `line`) is its `* Where:` line, in the one form recorded
  (`Build file '<path>' line: N`, a build script that failed to evaluate). A failure without one,
  or with a form not recorded (a settings file, an applied script, an init script), has a null
  location
- For `--module ... --test`, the module's report directory is Gradle's own answer: it runs
  `./gradlew -q <module>:properties --property projectDir` first (one extra configuration of the
  build per such run; a build without `--test` does not ask) and never reads the settings script
  itself, so any `projectDir`/`setProjectDir`/`includeFlat`/settings-plugin mapping is honoured.
  A module Gradle does not know, a Gradle failure or timeout (`ANDROID_EMU_BUILD_TIMEOUT`), an
  answer without exactly one `projectDir:` line, and a directory that is missing or outside
  `--project` are refused by name before any report is deleted or the test task starts
- `--test` reads only the reports *this* run wrote: it deletes the test task's old `TEST-*.xml` reports
  first, after proving their resolved paths stay inside the selected module and exact task directory,
  and reruns the task (`--rerun`, Gradle 7.6+; an older wrapper is refused by name). It passes only
  if Gradle exits 0 **and** they hold no failure or error — `ignoreFailures = true` cannot turn failing
  tests into a pass. An unreadable or malformed report refuses the partial verdict. A well-formed empty
  suite contributes no tests alongside nonempty reports; if every collected report has zero testcases,
  the existing no-results rule refuses the run. A run that wrote no test results is a failure, not "0 tests
  passed". If Gradle times out, its partial log is cached and the command refuses with a remedy to raise
  `ANDROID_EMU_BUILD_TIMEOUT`, `ANDROID_EMU_TEST_TIMEOUT` or `ANDROID_EMU_INSTRUMENTED_TEST_TIMEOUT` for the
  active mode, or inspect the stuck task
- Build ids look like `build-20251028-143052-1a2b3c4d` (time plus a random 8-hex-digit suffix, so two
  builds in one second cannot practically share one); pass the whole id back. An id another script
  cached (a `container --cat` id) is not a build: refused as not found (exit 1), never an empty list.
  `--get-errors`/`--get-warnings`/`--get-log` on an
  entry whose cache file cannot be read refuse with one error naming the file as corrupt, not expired
  (exit 1); the file is kept for inspection, not deleted. `--list-builds` skips such a file
- `--instrumented` (not with `--test`) runs `connected<Variant>AndroidTest` (module-qualified with
  `--module`) on **one** device, under the same verdict rules: Gradle exit 0, results this run wrote, no
  failure or error. The device is resolved *before* Gradle starts — `--serial` as everywhere else, or
  the one attached device — and pinned with `ANDROID_SERIAL` in Gradle's environment, because a
  connected task without it runs on every attached device. No device attached exits **3**, several
  and no `--serial` exits **1**, and Gradle is not started for either. AGP's own "Connected device with
  serial '…' not found!" (the device left between the resolve and the run) is the same device error,
  exit 3, with the log cached -- read only from a failed run that wrote no report at all (a
  zero-testcase or flavored one counts as written): AGP raises it
  while choosing the device, before any test runs, so a run with a report of its own had its device
  and is judged by that report whatever its log says. The task always gets `--rerun`; measured on AGP 8.13.2 an identical
  second run was not UP-TO-DATE anyway. AGP uninstalls the app and test APKs when the run ends.
  Timeout: `ANDROID_EMU_INSTRUMENTED_TEST_TIMEOUT` (default 3600s; recorded runs took 8–36 s on a warm
  local arm64 daemon, and a cold one builds two APKs first)
- `--suite` is Gradle's `--tests` filter for `--test`, and for `--instrumented` a fully qualified test
  class passed as the runner's argument (`-Pandroid.testInstrumentationRunnerArguments.class=<suite>`),
  never `--tests`
- Where each mode reads its JUnit XML: `--test` from `<module>/build/test-results/<task>/TEST-*.xml`;
  `--instrumented` from AGP's `<module>/build/outputs/androidTest-results/connected/<variant>/TEST-*.xml`,
  or `connected/<buildType>/flavors/<flavor>/` for a flavored variant (both layouts recorded with AGP
  8.13.2). Only the `TEST-*.xml` directly in that directory is deleted before the run and read after
  it — not the per-device UTP directory or `.pb` files beside it — with the same symlink and
  containment refusals as the unit root. The `TEST-*.xml` in that directory's own `flavors/<flavor>/`
  directories are deleted and checked the same way, so any there afterwards are this run's, but they
  are never read as its verdict (a flavored `proDebug` run leaves its sibling `flavors/free/` alone)
- An instrumented run that produced no test result is refused, never "0 passed", with an
  instrumented remedy (tests under `src/androidTest`, a `testInstrumentationRunner`, a `--suite` that
  exists, the device attached). That includes a `--suite` naming a class the test APK does not have:
  AGP reports it as one failing `initializationError` and exits 1, and it is refused as no test
  results, not reported as a failing test. A red run names the failing test in every mode;
  `--variant` naming a build type of a flavored project (`debug` where the variants are `freeDebug`
  and `proDebug`) leaves reports only under `connected/debug/flavors/<flavor>/`, none of its own: it
  is refused as no test results, whether Gradle exited 0 or failed, with a remedy naming the
  variants to pass instead (`--variant freeDebug` or `--variant proDebug`), one per invocation;
  `--json` adds `serial` and `device_task` to a unit run's keys

## Where the code is

The build system lives in the `gradle/` subpackage (`builder`, `results`, `cache`,
`config`, `reporter`), used by `build_and_test.py`.
