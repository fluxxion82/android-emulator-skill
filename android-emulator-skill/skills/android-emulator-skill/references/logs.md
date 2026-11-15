# logs.py

One entry point for reading logs; routes on the question being asked.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- `logs.py tail` (main buffers) → `log_monitor.py`; `logs.py crashes` (`-b crash`) → `crash_triage.py`; `logs.py anr` (ANR/jank, incl. session mode) → `anr_watcher.py`.
- Arguments are passed through verbatim, so each verb takes the full flag set of the script it delegates to, and those scripts remain callable unchanged.
