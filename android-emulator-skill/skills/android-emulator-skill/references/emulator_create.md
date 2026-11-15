# emulator_create.py

Create AVDs dynamically

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Create by device type and API level
- List available device definitions
- List available system images
- `--abi` defaults to this host's: `arm64-v8a` on an arm64/aarch64 machine
  (an Apple-silicon Mac's emulator boots nothing else), `x86_64` otherwise;
  `--help` states the value it will use. A Python running under Rosetta
  reports `x86_64`, so the default is wrong there: pass `--abi arm64-v8a`
  explicitly
- Checks the image is installed before creating. No `sdkmanager` (or no
  `avdmanager`) is an error naming where it was looked for, exit 1 — never
  an empty listing, and never a create of an image nobody could check
- Every refusal — a device definition not found, no API level, an image
  not installed (the error names the installed ones), a create avdmanager
  refused, a missing `--device` — is the shared `{"success": false,
  "error": ..., "remedy": ...}` under `--json` (it was `{"success": false,
  "message": ...}`), `Error:` / `Remedy:` on stderr otherwise, exit 1
- Reads both `sdkmanager` listing formats: the Java tool's pipe table
  (every cmdline-tools before 23, and CI images) and the slash-path columns
  of cmdline-tools 23's Android-CLI wrapper. It read only the second, so
  with the Java tool an installed image was reported as not installed

## Example: a fresh emulator for one test run

```bash
# 1. Create fresh emulator
python3 "$SKILL_DIR/scripts/emulator_create.py" --device pixel_7 --api 34 --name test-device

# 2. Boot emulator
python3 "$SKILL_DIR/scripts/emulator_boot.py" --avd test-device --wait-ready

# 3. Build and test: unit tests, then instrumented tests on the booted emulator
python3 "$SKILL_DIR/scripts/build_and_test.py" --project . --test
python3 "$SKILL_DIR/scripts/build_and_test.py" --project . --instrumented --serial emulator-5554

# 4. Run UI tests
# ... your test scripts ...

# 5. Cleanup
python3 "$SKILL_DIR/scripts/emulator_shutdown.py" --serial emulator-5554
python3 "$SKILL_DIR/scripts/emulator_delete.py" --name test-device
```
