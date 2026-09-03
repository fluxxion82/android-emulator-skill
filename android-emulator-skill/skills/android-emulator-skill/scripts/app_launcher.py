#!/usr/bin/env python3
"""
Android App Lifecycle Management

Manage app installation, launching, termination, and state inspection.

Key features:
- Launch apps by package name
- Terminate apps
- Install/uninstall APKs
- Deep link navigation
- List installed apps
- Check app state
"""

import argparse
import json as json_lib
import re
import sys
import time

from common import adb_exec
from common.device_utils import (
    get_current_activity,
    list_installed_packages,
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_float

# Delay between terminate and launch when restarting an app.
RELAUNCH_DELAY_SECONDS = env_float("ANDROID_EMU_RELAUNCH_DELAY_MS", 1000.0) / 1000.0

# `adb install` streams an APK to the device and lets the package manager
# verify and optimise it; a large debug build routinely runs past the 30s
# adb_exec default, so it gets its own budget rather than raising that default
# for every call in the skill. Uninstall touches package-manager state too but
# moves no bytes, so it needs far less.
INSTALL_TIMEOUT_SECONDS = 300
UNINSTALL_TIMEOUT_SECONDS = 60


def parse_extras(pairs: list[str] | None) -> dict[str, str]:
    """
    Parse repeatable ``KEY=VALUE`` strings into a dict of intent string extras.

    Args:
        pairs: List of ``KEY=VALUE`` strings (from ``--args``), or None

    Returns:
        Mapping of extra keys to values

    Raises:
        ValueError: If a pair is malformed (missing ``=`` or empty key)
    """
    extras: dict[str, str] = {}
    for pair in pairs or []:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ValueError(f"invalid --args '{pair}', expected KEY=VALUE")
        extras[key] = value
    return extras


class AppLauncher:
    """Manage Android app lifecycle."""

    def __init__(self, serial: str | None = None):
        """Initialize with optional device serial."""
        self.serial = serial

    def launch(
        self,
        package_name: str,
        activity: str | None = None,
        extras: dict[str, str] | None = None,
    ) -> tuple:
        """
        Launch app by package name.

        Args:
            package_name: App package name (e.g., "com.example.app")
            activity: Optional activity name (auto-detects launcher activity if None)
            extras: Optional intent string extras passed to "am start" as
                "--es KEY VALUE" pairs

        Returns:
            (success, message) tuple
        """
        try:
            # If no activity specified, try to get launcher activity
            if not activity:
                activity = self._get_launcher_activity(package_name)
                if not activity:
                    return False, (
                        f"Could not find launcher activity for {package_name}. "
                        "Specify --activity explicitly."
                    )

            # Build activity component name
            component = f"{package_name}/{activity}" if "/" not in activity else activity

            # Build intent string extras as "--es KEY VALUE" pairs.
            #
            # Every part is quoted for the shell running ON THE DEVICE: adb
            # joins the host argv into one string and the device's `sh -c`
            # re-parses it (common/device_utils.py:84-91). An extra value
            # carrying a space, `&` or `;` otherwise splits the command --
            # `--args note=a b` became two arguments and `--args x=a;id` ran
            # `id` on the device.
            extra_args = [
                quote_for_device_shell(part)
                for key, value in (extras or {}).items()
                for part in ("--es", key, value)
            ]

            # Launch activity
            result = adb_exec.run_adb(
                "shell",
                self.serial,
                "am",
                "start",
                "-n",
                quote_for_device_shell(component),
                "-a",
                "android.intent.action.MAIN",
                *extra_args,
                check=True,
            )

            if "Error" in result.stdout or "error" in result.stderr.lower():
                return False, f"Launch failed: {result.stdout or result.stderr}"

            return True, f"Launched: {package_name}"

        except adb_exec.AdbCommandError as e:
            return False, f"Launch failed: {e}"
        except adb_exec.AdbError:
            # The command never reached a device (ambiguous target, wrong
            # serial, offline, unauthorized). That is not "the launch failed";
            # re-raise so main() can print the remedy the error already names.
            raise
        except Exception as e:
            return False, f"Launch error: {e}"

    def terminate(self, package_name: str) -> tuple:
        """
        Terminate app by package name.

        Args:
            package_name: App package name

        Returns:
            (success, message) tuple
        """
        try:
            adb_exec.run_adb(
                "shell",
                self.serial,
                "am",
                "force-stop",
                quote_for_device_shell(package_name),
                check=True,
            )

            return True, f"Terminated: {package_name}"

        except adb_exec.AdbCommandError as e:
            return False, f"Terminate failed: {e}"

    def restart(
        self,
        package_name: str,
        activity: str | None = None,
        extras: dict[str, str] | None = None,
        delay: float = RELAUNCH_DELAY_SECONDS,
    ) -> tuple:
        """
        Restart app: terminate then launch, sleeping between the two.

        Args:
            package_name: App package name
            activity: Optional activity name (auto-detects launcher activity if None)
            extras: Optional intent string extras forwarded to the relaunch
            delay: Seconds to sleep between terminate and launch

        Returns:
            (success, message) tuple
        """
        term_success, term_message = self.terminate(package_name)
        if not term_success:
            return False, term_message

        if delay > 0:
            time.sleep(delay)

        launch_success, launch_message = self.launch(package_name, activity, extras)
        if not launch_success:
            return False, launch_message

        return True, f"Restarted: {package_name}"

    def install(self, apk_path: str, replace: bool = True) -> tuple:
        """
        Install APK.

        Args:
            apk_path: Path to APK file
            replace: Replace existing app if installed

        Returns:
            (success, message) tuple
        """
        try:
            install_args = ["-r", apk_path] if replace else [apk_path]
            result = adb_exec.run_adb(
                "install",
                self.serial,
                *install_args,
                timeout=INSTALL_TIMEOUT_SECONDS,
                check=True,
            )

            if "Success" in result.stdout:
                return True, f"Installed: {apk_path}"
            return False, f"Install failed: {result.stdout}"

        except adb_exec.AdbCommandError as e:
            return False, f"Install failed: {e}"

    def uninstall(self, package_name: str) -> tuple:
        """
        Uninstall app.

        Args:
            package_name: App package name

        Returns:
            (success, message) tuple
        """
        try:
            result = adb_exec.run_adb(
                "uninstall",
                self.serial,
                package_name,
                timeout=UNINSTALL_TIMEOUT_SECONDS,
                check=True,
            )

            if "Success" in result.stdout:
                return True, f"Uninstalled: {package_name}"
            return False, f"Uninstall failed: {result.stdout}"

        except adb_exec.AdbCommandError as e:
            return False, f"Uninstall failed: {e}"

    def open_url(self, url: str) -> tuple:
        """
        Open URL (deep link or web URL).

        The URL is quoted for the device shell. A query string is the reason
        this is not optional: ``adb shell am start -d https://x/?a=1&b=2``
        reaches the device as one string, whose ``&`` backgrounds ``am start``
        and truncates the URL at ``?a=1`` -- while this method still reported
        the whole URL as opened.

        Args:
            url: URL to open

        Returns:
            (success, message) tuple
        """
        try:
            result = adb_exec.run_adb(
                "shell",
                self.serial,
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                quote_for_device_shell(url),
                check=True,
            )

            if "Error" in result.stdout or "error" in result.stderr.lower():
                return False, f"Open URL failed: {result.stdout or result.stderr}"

            return True, f"Opened URL: {url}"

        except adb_exec.AdbCommandError as e:
            return False, f"Open URL failed: {e}"

    def list_packages(self, filter_text: str | None = None) -> tuple:
        """
        List installed packages.

        A failure raises. It used to be caught here -- every exception, down to
        "adb is not installed" -- and answered with an empty list, which
        ``main()`` then printed as ``Installed packages (0)`` and exited 0
        (C6). "Nothing is installed" and "I could not look" are different
        answers, and the agent reading this has no second signal to consult.

        Args:
            filter_text: Optional filter string

        Returns:
            (success, packages_list) tuple

        Raises:
            RuntimeError: the listing failed. ``adb_exec``'s errors subclass
                ``RuntimeError`` and carry their own remedy;
                ``list_installed_packages`` re-raises a non-zero ``pm list
                packages`` the same way. ``main()`` prints either and exits 1.
        """
        packages = list_installed_packages(self.serial)

        if filter_text:
            packages = [p for p in packages if filter_text.lower() in p.lower()]

        return True, packages

    def get_state(self, package_name: str) -> tuple:
        """
        Get app state.

        Like :meth:`list_packages`, a failure raises rather than being packed
        into the returned dict. The previous ``except Exception`` produced
        ``{"error": ...}``, which ``--state`` text mode then indexed for
        ``state['package']`` and crashed on, while ``--state --json`` printed
        the error and exited 0 (C6).

        Args:
            package_name: App package name

        Returns:
            (success, state_dict) tuple

        Raises:
            RuntimeError: the state could not be read; ``main()`` prints the
                remedy it carries and exits 1.
        """
        # Check if installed
        packages = list_installed_packages(self.serial)
        installed = package_name in packages

        if not installed:
            return True, {"package": package_name, "installed": False, "running": False}

        # Check if running (has process). `pidof` exits non-zero when the
        # app is not running, which is an answer rather than a failure, so
        # this deliberately does not pass check=True.
        result = adb_exec.run_adb(
            "shell", self.serial, "pidof", quote_for_device_shell(package_name)
        )
        running = result.ok and result.stdout.strip()

        # Get current activity. strict=True because `foreground` is part of
        # the answer this method returns: the non-strict lookup maps an adb
        # failure to None, which would be reported as "not in the foreground"
        # after nothing was asked (C6).
        current_activity = get_current_activity(self.serial, strict=True)
        is_foreground = current_activity and package_name in current_activity

        return True, {
            "package": package_name,
            "installed": True,
            "running": bool(running),
            "foreground": is_foreground,
            "current_activity": current_activity if is_foreground else None,
        }

    def _get_launcher_activity(self, package_name: str) -> str | None:
        """
        Get launcher activity for package.

        Asks the package manager the same question the launcher asks:
        ``cmd package resolve-activity --brief -c
        android.intent.category.LAUNCHER <package>``. Recorded output (see
        ``tests/fixtures/recorded/*/resolve_activity_launcher.txt``) is::

            priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
            com.android.settings/.Settings

        so the answer is the last non-blank line, and it is a single
        unambiguous component.

        Args:
            package_name: App package name

        Returns:
            The ``package/activity`` component, or None if nothing resolves.
        """
        # For Settings, use known activity
        if package_name == "com.android.settings":
            return ".Settings"

        result = adb_exec.run_adb(
            "shell",
            self.serial,
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            quote_for_device_shell(package_name),
        )

        return self._parse_resolved_component(result.stdout)

    @staticmethod
    def _parse_resolved_component(output: str) -> str | None:
        """Pull the ``package/activity`` component out of resolve-activity output.

        Anything that is not a bare component -- ``No activity found``, the
        ``priority=...`` header line -- yields None rather than a plausible
        looking fragment.

        Args:
            output: stdout from ``cmd package resolve-activity --brief``

        Returns:
            The component, or None when the output names no activity.
        """
        for line in reversed([ln.strip() for ln in output.splitlines() if ln.strip()]):
            if re.fullmatch(r"[\w.]+/[\w.$]+", line):
                return line
        return None


def _report_failure(args: argparse.Namespace, message: str) -> None:
    """Report a failed operation the way the agent reading it expects, and exit 1.

    One place, because C6 was the same contract kept in some branches and not
    others: ``--list`` and ``--state`` printed an empty-looking answer and
    exited 0, while ``--launch`` next to them exited 1. In ``--json`` the
    payload is ``{"error": ...}`` on stdout -- an agent parsing stdout must not
    have to also read stderr to find out the run failed.

    Args:
        args: Parsed CLI arguments; only ``--json`` is consulted.
        message: What failed, already carrying its remedy.
    """
    if args.json:
        print(json_lib.dumps({"error": message}, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _report_action(args: argparse.Namespace, action: str, success: bool, message: str) -> None:
    """Print the outcome of one lifecycle action and exit.

    The success shape is unchanged (``success`` / ``message`` / ``action``).
    A failure goes through :func:`_report_failure`, so ``--json`` answers
    ``{"error": ...}`` on every mode of this script rather than six modes
    answering ``{"success": false, ...}`` while two answered ``{"error": ...}``
    -- an agent should not have to know which flag it passed to know where the
    failure is written.

    Args:
        args: Parsed CLI arguments; only ``--json`` is consulted.
        action: The action name reported in the JSON payload.
        success: Whether the action succeeded.
        message: The outcome text; on failure it carries the remedy.
    """
    if not success:
        _report_failure(args, message)

    if args.json:
        print(json_lib.dumps({"success": True, "message": message, "action": action}, indent=2))
    else:
        print(message)
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="Android app lifecycle management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Launch app
  python app_launcher.py --launch com.example.app

  # Launch specific activity
  python app_launcher.py --launch com.example.app --activity .MainActivity

  # Launch with intent string extras (repeatable)
  python app_launcher.py --launch com.example.app --args mode=test --args env=ci

  # Restart app (terminate then launch)
  python app_launcher.py --restart com.example.app

  # Terminate app
  python app_launcher.py --terminate com.example.app

  # Install APK
  python app_launcher.py --install /path/to/app.apk

  # Uninstall app
  python app_launcher.py --uninstall com.example.app

  # Open deep link
  python app_launcher.py --open-url "myapp://main"

  # List installed packages
  python app_launcher.py --list

  # Get app state
  python app_launcher.py --state com.example.app
        """,
    )

    parser.add_argument("--serial", "-s", help="Device serial number (auto-detects if omitted)")
    parser.add_argument("--launch", help="Launch app by package name")
    parser.add_argument("--restart", help="Restart app by package name (terminate then launch)")
    parser.add_argument("--activity", help="Activity name for launch/restart (optional)")
    parser.add_argument(
        "--args",
        action="append",
        metavar="KEY=VALUE",
        help="Intent string extra (repeatable); passed to 'am start' as '--es KEY VALUE'",
    )
    parser.add_argument("--terminate", help="Terminate app by package name")
    parser.add_argument("--install", help="Install APK from path")
    parser.add_argument("--uninstall", help="Uninstall app by package name")
    parser.add_argument("--open-url", help="Open URL or deep link")
    parser.add_argument("--list", action="store_true", help="List installed packages")
    parser.add_argument("--filter", help="Filter packages by text (use with --list)")
    parser.add_argument("--state", help="Get app state by package name")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    args = parser.parse_args()

    # Parse --args KEY=VALUE intent extras, failing fast on malformed input.
    try:
        extras = parse_extras(args.args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve device
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    launcher = AppLauncher(serial)

    # Execute operation.
    #
    # CLI boundary: a device-level adb failure (ambiguous target, wrong
    # serial, offline, unauthorized) means the command never ran. Those
    # errors already carry a remedy, so print it rather than letting a
    # traceback reach the user.
    try:
        if args.launch:
            success, message = launcher.launch(args.launch, args.activity, extras or None)
            _report_action(args, "launch", success, message)

        elif args.restart:
            success, message = launcher.restart(args.restart, args.activity, extras or None)
            _report_action(args, "restart", success, message)

        elif args.terminate:
            success, message = launcher.terminate(args.terminate)
            _report_action(args, "terminate", success, message)

        elif args.install:
            success, message = launcher.install(args.install)
            _report_action(args, "install", success, message)

        elif args.uninstall:
            success, message = launcher.uninstall(args.uninstall)
            _report_action(args, "uninstall", success, message)

        elif args.open_url:
            success, message = launcher.open_url(args.open_url)
            _report_action(args, "open_url", success, message)

        elif args.list:
            success, packages = launcher.list_packages(args.filter)
            if not success:
                _report_failure(
                    args,
                    "could not list installed packages; run `adb devices` to check "
                    "the device is attached and unlocked",
                )
            if args.json:
                print(json_lib.dumps({"packages": packages, "count": len(packages)}, indent=2))
            else:
                print(f"Installed packages ({len(packages)}):")
                for pkg in packages:
                    print(f"  - {pkg}")
            sys.exit(0 if success else 1)

        elif args.state:
            success, state = launcher.get_state(args.state)
            if not success:
                # Structurally unreachable today -- get_state raises instead --
                # and kept so that a future failure return cannot walk into the
                # `state['package']` below, which is exactly how C6 turned a
                # failed lookup into a KeyError traceback.
                _report_failure(
                    args,
                    state.get(
                        "error",
                        "could not read app state; run `adb devices` to check the "
                        "device is attached and unlocked",
                    ),
                )
            elif args.json:
                print(json_lib.dumps(state, indent=2))
            elif state.get("installed"):
                print(f"Package: {state['package']}")
                print("Installed: Yes")
                print(f"Running: {'Yes' if state.get('running') else 'No'}")
                print(f"Foreground: {'Yes' if state.get('foreground') else 'No'}")
                if state.get("current_activity"):
                    print(f"Current Activity: {state['current_activity']}")
            else:
                print(f"Package: {state['package']}")
                print("Installed: No")
            sys.exit(0 if success else 1)

        else:
            parser.print_help()
            sys.exit(1)

    except RuntimeError as error:
        # Every adb failure subclasses RuntimeError and arrives carrying its
        # own remedy; `list_installed_packages` re-raises a non-zero `pm list
        # packages` as a plain RuntimeError. Catching only AdbError let the
        # second one reach the user as a traceback once list_packages stopped
        # swallowing it.
        _report_failure(args, str(error))


if __name__ == "__main__":
    main()
