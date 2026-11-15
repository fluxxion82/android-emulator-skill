# android_health_check.sh

Verify the environment (ANDROID_HOME, adb, emulator, avdmanager, sdkmanager, java, Python 3.12+, Pillow); lists connected devices and AVDs. Exits non-zero if adb is missing or the JDK is below 17; an argument it does not take is a usage error (exit 2, the one refusal object under `--json`), refused before anything is checked. Quick Start step 0.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- The JDK floor is 17 (AGP 8 runs Gradle on nothing older). The major is
  read from the first line of `java -version` (both streams) that STARTS
  `java version "` or `openjdk version "` -- not from its first line,
  since startup notices such as `Picked up JAVA_TOOL_OPTIONS: ...` can come
  before the banner -- including the pre-Java-9
  `"1.8.0_392"` form (major 8); `--json` carries it as `java_major`, or
  `null` when java is missing or its banner has no readable version
  (that case is a warning, not a failure).
- Every probe is bounded (10 s), the Python version and Pillow probes
  included. A `python3` on PATH that cannot run is a warning saying so,
  not "Python 0.0 found"
- `--json` prints one object on stdout (progress goes to stderr). When
  `python3` cannot encode it, a Bash encoder does; when the report cannot
  be emitted at all (no temporary directory, or a check record that could
  not be written or read back), it exits 1 with one `{"success": false,
  "error": ..., "remedy": ...}` line — never an empty stdout
- When Python runs, encoder stdout is parsed as one complete report with the expected keys;
  without Python, the Bash guard enforces one brace-balanced top-level object. Concatenated
  startup-hook output is discarded before the Bash fallback is used
