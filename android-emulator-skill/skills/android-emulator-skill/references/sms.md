# sms.py

Deliver an inbound SMS to an emulator and **prove it arrived**. Emulator-only.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- `--send` reads the inbox back and reports `accepted` and `delivered` separately: the console's `OK` means the command was taken, not that a message exists. Delivery is asynchronous (~2s measured), so it polls.
- `--otp` extracts a one-time code from the newest message and prints the message it came from, so the heuristic can be checked.
