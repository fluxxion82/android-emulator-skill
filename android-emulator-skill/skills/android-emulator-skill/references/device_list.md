# device_list.py

List connected devices (`adb devices -l`) and defined AVDs (`emulator -list-avds`) with progressive disclosure.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- An empty inventory at exit 0 means the tools ran and found nothing. A
  missing or failing `adb` or `emulator` exits 1 with the remedy
  (`{"success": false, "error": ..., "remedy": ...}` under `--json`).
  `avdmanager` is the exception: it only adds target/ABI detail, so its
  absence is a warning on stderr (and in
  `warnings` in the JSON) rather than a failure
