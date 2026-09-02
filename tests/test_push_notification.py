"""What push_notification may claim, and how it is allowed to prove it.

The old script advertised "simulate push notifications for testing notification
handling" and invented every piece it needed to look like it worked:

  S4   It broadcast to ``{package}/.NotificationReceiver`` -- a class this skill
       made up -- and reported success when stdout contained ``result=``.
       ``am broadcast`` always prints "Broadcast completed: result=0" and exits 0
       even for a receiver class that does not exist, so the failure branch was
       unreachable. The old tests mocked stdout as exactly that string, which is
       how the dead branch survived: code and tests were wrong in the same
       direction.

  S14  ``--list-channels`` ran ``cmd notification list channels <pkg>``. No such
       subcommand exists; the extra arguments are ignored, bare ``list`` runs and
       exits 0, so the answer was permanently "no channels found".

So these tests do two things the old ones did not. Every piece of tool output
comes from ``tests/fixtures/recorded/`` via the ``recorded`` / ``any_profile``
fixtures -- never an inline literal -- and the central case feeds the *recorded
failure output* into the success path to prove a substring can no longer stand
in for a result.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import push_notification
import pytest

SOURCE = Path(push_notification.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


@pytest.fixture(autouse=True)
def _fast_verify_poll(monkeypatch):
    """Collapse the post read-back deadline for tests.

    `post()` polls `cmd notification list` because the notification is not
    registered by the time `cmd notification post` returns (measured on API 35:
    absent immediately, present within ~2s). The production default of 10s is
    right for a slow device but would make every negative test here wait it out.
    """
    monkeypatch.setattr(push_notification, "VERIFY_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(push_notification, "VERIFY_POLL_SECONDS", 0)


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


def _contains(cmd: list[str], tokens: tuple[str, ...]) -> bool:
    """Whether ``tokens`` appears as a contiguous run inside ``cmd``."""
    span = len(tokens)
    return any(cmd[i : i + span] == list(tokens) for i in range(len(cmd) - span + 1))


class FakeAdb:
    """Routes adb invocations to canned results and records every call.

    Responses are keyed on the device-side command so a single test can script
    the post and its read-back independently, which is the whole point: the two
    disagreeing is the failure the old code could not detect.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._routes: list[tuple[tuple[str, ...], SimpleNamespace]] = []
        self.default = SimpleNamespace(returncode=0, stdout="", stderr="")

    def when(self, *tokens: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Answer any command containing ``tokens`` with this result."""
        self._routes.append(
            (tokens, SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))
        )

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        for tokens, response in self._routes:
            if _contains(list(cmd), tokens):
                return response
        return self.default

    def commands_matching(self, *tokens: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if _contains(cmd, tokens)]


@pytest.fixture
def fake_adb(monkeypatch) -> FakeAdb:
    """Patch the module's subprocess.run with a scriptable fake."""
    fake = FakeAdb()
    monkeypatch.setattr(push_notification.subprocess, "run", fake.run)
    return fake


def _run_main(monkeypatch, argv: list[str]) -> int:
    """Run the CLI in-process and return its exit code.

    An agent branches on the exit code, so the exit code is what gets asserted.
    """
    monkeypatch.setattr(push_notification.sys, "argv", ["push_notification.py", *argv])
    monkeypatch.setattr(push_notification, "resolve_device_identifier", lambda _serial: None)
    with pytest.raises(SystemExit) as exit_info:
        push_notification.main()
    return exit_info.value.code


def _key_line_in_recorded_shape(recorded, package: str, tag: str, notification_id: int = 1) -> str:
    """A ``cmd notification list`` line built from the RECORDED field layout.

    Nothing had posted a com.android.shell notification when the fixture was
    captured, so the success path needs a line the fixture does not contain. The
    *shape* -- field count, order, separator, user id and uid -- is lifted from
    ground truth rather than imagined, and the round-trip assertion below proves
    the reconstruction is something the parser genuinely accepts.
    """
    template = push_notification.parse_notification_keys(recorded.text("cmd_notification_list"))[0]
    line = "|".join([str(template.user_id), package, str(notification_id), tag, str(template.uid)])
    parsed = push_notification.parse_notification_keys(line)
    assert len(parsed) == 1, f"reconstructed line is not in the recorded shape: {line!r}"
    assert (parsed[0].package, parsed[0].tag) == (package, tag)
    return line


# ---------------------------------------------------------------------------
# S4 / S14 — the invented surface must be gone.
# ---------------------------------------------------------------------------


def _adb_call_constants(func_names: tuple[str, ...]) -> list[list[str]]:
    """Constant string args of every call to one of ``func_names``.

    Parsed, not grepped: the module docstring now *explains* the invented
    receiver and the bogus subcommand, and a substring search cannot tell an
    explanation apart from a call.
    """
    calls = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in func_names:
            calls.append([a.value for a in node.args if isinstance(a, ast.Constant)])
    return calls


def _device_commands() -> list[list[str]]:
    """Every device-side command the module issues, as constant tokens."""
    return _adb_call_constants(("_shell", "build_adb_command"))


@pytest.mark.parametrize("invented", [".NotificationReceiver", ".MainActivity"])
def test_no_invented_component_is_targeted(invented):
    """S4: no real app implements either class; both were hardcoded."""
    interpolations = [
        ast.unparse(node)
        for node in ast.walk(TREE)
        if isinstance(node, ast.JoinedStr) and invented in ast.unparse(node)
    ]
    literals = [
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(invented)
    ]
    assert not interpolations, f"still targets {invented}: {interpolations}"
    assert not literals, f"still targets {invented}: {literals}"


def test_no_am_broadcast_is_issued():
    """S4: the delivery mechanism itself was fiction, not just the class name."""
    for tokens in _device_commands():
        assert not (
            "am" in tokens and "broadcast" in tokens
        ), f"still issues an am broadcast: {tokens}"


def test_success_is_never_a_membership_test_on_command_output():
    """S4 root cause: ``if "result=" in result.stdout``.

    Any ``<literal> in <expr>`` check is how the original bug was spelled, so the
    marker strings it used may not reappear in one.
    """
    forbidden = {"result=", "broadcast", "channelid"}
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            continue
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            assert (
                node.left.value.lower() not in forbidden
            ), f"success still inferred from a substring: {ast.unparse(node)}"


def test_every_cmd_notification_subcommand_exists(any_profile):
    """S14: cross-check the module against the platform's own help, per API level.

    ``cmd notification`` does not reject an unknown trailing argument, so an
    invented subcommand exits 0 and looks fine. The recorded help is the only
    authority, and it must agree on every device we have evidence for.
    """
    if not any_profile.has("cmd_notification_help"):
        pytest.skip(f"{any_profile.name} did not record cmd_notification_help")

    help_text = any_profile.text("cmd_notification_help")
    documented = {line.split()[0] for line in help_text.splitlines() if line.startswith("  ")}
    assert {"post", "list"} <= documented, "fixture does not look like the expected help output"

    issued = [
        tokens[2]
        for tokens in _device_commands()
        if len(tokens) >= 3 and tokens[:2] == ["cmd", "notification"]
    ]
    assert issued, "module no longer issues any `cmd notification` command"
    for subcommand in issued:
        assert subcommand in documented, (
            f"`cmd notification {subcommand}` is not in the subcommand list on "
            f"{any_profile.name}; it would silently exit 0"
        )


def test_no_literal_word_is_appended_to_a_notification_subcommand():
    """S14 exactly: `list channels` parsed as bare `list` plus ignored noise.

    ``cmd notification`` accepts trailing junk without complaint, so an invented
    sub-subcommand is indistinguishable from the real thing at runtime. A literal
    token after the subcommand is therefore only ever legitimate as a flag --
    real argument values are computed, not hardcoded.
    """
    for tokens in _device_commands():
        if len(tokens) < 3 or tokens[:2] != ["cmd", "notification"]:
            continue
        extras = [token for token in tokens[3:] if not token.startswith("-")]
        assert not extras, (
            f"`cmd notification {tokens[2]}` carries hardcoded extra words {extras}; "
            "that is how `list channels` silently became bare `list`"
        )


def test_list_channels_flag_is_gone():
    """S14: the CLI must not advertise a capability adb cannot provide."""
    flags = [
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "--list-channels" not in flags


# ---------------------------------------------------------------------------
# The parser, against real `cmd notification list` output.
# ---------------------------------------------------------------------------


def test_parses_recorded_notification_keys(recorded):
    """Keys are ``userId|package|id|tag|uid``, with a literal ``null`` for no tag."""
    parsed = push_notification.parse_notification_keys(recorded.text("cmd_notification_list"))

    assert parsed, "parser matched nothing against real `cmd notification list` output"
    assert len(parsed) == len(
        [line for line in recorded.lines("cmd_notification_list") if line.strip()]
    ), "some recorded keys were dropped"

    assert {item.package for item in parsed} == {"android"}
    assert any(item.tag is None for item in parsed), "'null' tag was not normalised to None"
    assert any(item.user_id == -1 for item in parsed), "the all-users (-1) key was not parsed"
    assert all(isinstance(item.notification_id, int) for item in parsed)
    assert all(item.key in recorded.text("cmd_notification_list") for item in parsed)


def test_a_tag_containing_a_pipe_keeps_the_fixed_fields(recorded):
    """Tags are app-chosen free text; only the outer fields are positional."""
    line = _key_line_in_recorded_shape(recorded, "com.example.app", "a|b")
    parsed = push_notification.parse_notification_keys(line)
    assert parsed[0].tag == "a|b"
    assert parsed[0].package == "com.example.app"


def test_non_key_lines_are_skipped_not_guessed_at(recorded):
    """Anything whose fixed fields are not integers is not a key."""
    noise = recorded.text("cmd_notification_help")
    assert push_notification.parse_notification_keys(noise) == []


# ---------------------------------------------------------------------------
# The central case: an always-present substring is not a result.
# ---------------------------------------------------------------------------


def test_recorded_success_substring_does_not_make_a_post_succeed(recorded, fake_adb):
    """The exact output the old check trusted, with nothing actually posted.

    ``am_broadcast_missing_receiver`` is a recording of a command that did
    nothing: it targeted a class that does not exist, printed "Broadcast
    completed: result=0" and exited 0. Feed that as the post's stdout, and a
    read-back in which no such notification exists, and the answer must be
    failure. The old implementation returned success on this input.
    """
    always_present = recorded.text("am_broadcast_missing_receiver")
    assert "result=" in always_present.lower(), "fixture no longer carries the trusted substring"

    fake_adb.when("cmd", "notification", "post", returncode=0, stdout=always_present)
    fake_adb.when(
        "cmd", "notification", "list", returncode=0, stdout=recorded.text("cmd_notification_list")
    )

    success, result = push_notification.NotificationTester().post("order-42", "shipped")

    assert success is False, "success was inferred from output that proves nothing"
    assert result["verified"] is False
    assert "order-42" in result["error"]


def test_cli_exits_non_zero_when_the_post_cannot_be_verified(recorded, fake_adb, monkeypatch):
    """The same case through the CLI, because agents branch on the exit code."""
    fake_adb.when(
        "cmd",
        "notification",
        "post",
        returncode=0,
        stdout=recorded.text("am_broadcast_missing_receiver"),
    )
    fake_adb.when(
        "cmd", "notification", "list", returncode=0, stdout=recorded.text("cmd_notification_list")
    )

    code = _run_main(monkeypatch, ["--post", "--tag", "t", "--text", "hi"])
    assert code == 1


def test_non_zero_exit_is_a_failure_whatever_stdout_says(recorded, fake_adb):
    """Exit status outranks output.

    No invented stderr: the double is given a non-zero status and nothing else,
    so what is asserted is the code's own fallback message. The stderr literal
    this replaced -- "Exception occurred while executing 'post'" -- described a
    case that does not exist. `cmd notification post` was driven into every
    failure it has (no arguments, unknown option, missing argument, bad icon,
    bad style, bad picture spec) and exited 0 every single time; see
    cmd_notification_post_rejected.
    """
    fake_adb.when(
        "cmd",
        "notification",
        "post",
        returncode=1,
        stdout=recorded.text("am_broadcast_missing_receiver"),
    )
    success, result = push_notification.NotificationTester().post("t", "hi")

    assert success is False, "a reassuring stdout outranked a non-zero exit"
    assert result["exit_code"] == 1
    assert "result=0" in result["error"], "the failure must carry what adb printed"


def test_a_refusal_at_exit_zero_is_a_failure(recorded, fake_adb):
    """The failure mode this command actually has.

    `cmd notification post -i nonsense://x tag hello` answers
    "error: invalid icon: nonsense://x" and exits 0. With --no-verify the exit
    status is the only other signal, and it says success. Checking for the
    usage block alone -- which this rejection does not print -- let it through.
    """
    rejection = recorded.text("cmd_notification_post_rejected")
    assert "exit=0" in rejection, "the fixture no longer records a zero exit"

    fake_adb.when("cmd", "notification", "post", returncode=0, stdout=rejection)
    success, result = push_notification.NotificationTester().post("t", "hi", verify=False)

    assert success is False
    assert "invalid icon" in result["error"]


def test_the_usage_block_at_exit_zero_is_also_a_failure(recorded, fake_adb):
    """The other recorded rejection wording: bare `post` prints its usage."""
    fake_adb.when(
        "cmd",
        "notification",
        "post",
        returncode=0,
        stdout=recorded.text("cmd_notification_post_usage"),
    )
    success, result = push_notification.NotificationTester().post("t", "hi", verify=False)

    assert success is False
    assert "nothing was posted" in result["error"]


def test_post_succeeds_only_once_the_notification_is_actually_there(recorded, fake_adb):
    """The positive case, proven the same way: by reading the shade back."""
    listing = "\n".join(
        [
            recorded.text("cmd_notification_list").rstrip("\n"),
            _key_line_in_recorded_shape(recorded, push_notification.SHELL_PACKAGE, "order-42"),
        ]
    )
    fake_adb.when("cmd", "notification", "post", returncode=0)
    fake_adb.when("cmd", "notification", "list", returncode=0, stdout=listing)

    success, result = push_notification.NotificationTester().post("order-42", "shipped")

    assert success is True
    assert result["verified"] is True
    assert result["posted_as"] == push_notification.SHELL_PACKAGE
    assert result["channel"] == push_notification.SHELL_CHANNEL
    assert "order-42" in result["key"]


def test_a_matching_tag_from_another_package_does_not_count(recorded, fake_adb):
    """Only com.android.shell's own notification proves the post landed."""
    fake_adb.when("cmd", "notification", "post", returncode=0)
    fake_adb.when(
        "cmd",
        "notification",
        "list",
        returncode=0,
        stdout=_key_line_in_recorded_shape(recorded, "com.example.app", "order-42"),
    )
    success, _result = push_notification.NotificationTester().post("order-42", "shipped")
    assert success is False


def test_a_failed_read_back_is_not_a_pass(fake_adb):
    """If the shade cannot be read, the post is unconfirmed, not confirmed."""
    fake_adb.when("cmd", "notification", "post", returncode=0)
    fake_adb.when("cmd", "notification", "list", returncode=255, stderr="device offline")
    success, result = push_notification.NotificationTester().post("t", "hi")
    assert success is False
    assert "device offline" in result["error"]


def test_no_verify_reports_that_it_did_not_verify(fake_adb):
    """--no-verify trades the proof away, and must say so rather than claim it."""
    fake_adb.when("cmd", "notification", "post", returncode=0)
    success, result = push_notification.NotificationTester().post("t", "hi", verify=False)

    assert success is True
    assert result["verified"] is False
    assert "not verified" in result["note"].lower()
    assert not fake_adb.commands_matching("cmd", "notification", "list")


def test_no_verify_still_rejects_a_usage_dump(any_profile, fake_adb):
    """Without a read-back, the shell echoing its own help is the only tell."""
    if not any_profile.has("cmd_notification_help"):
        pytest.skip(f"{any_profile.name} did not record cmd_notification_help")
    fake_adb.when(
        "cmd",
        "notification",
        "post",
        returncode=0,
        stdout=any_profile.text("cmd_notification_help"),
    )
    success, result = push_notification.NotificationTester().post("t", "hi", verify=False)
    assert success is False
    assert "usage" in result["error"].lower()


# ---------------------------------------------------------------------------
# Verifying what the app posted — the capability that replaces the fiction.
# ---------------------------------------------------------------------------


def test_expect_package_exits_zero_for_a_package_present_in_the_recording(
    recorded, fake_adb, monkeypatch
):
    """`android` really is posting in the recorded output."""
    fake_adb.when(
        "cmd", "notification", "list", returncode=0, stdout=recorded.text("cmd_notification_list")
    )
    assert _run_main(monkeypatch, ["--list", "--expect-package", "android"]) == 0


def test_expect_package_exits_non_zero_for_a_package_that_posted_nothing(
    recorded, fake_adb, monkeypatch
):
    """The branch an agent needs: absence must be reportable."""
    fake_adb.when(
        "cmd", "notification", "list", returncode=0, stdout=recorded.text("cmd_notification_list")
    )
    assert _run_main(monkeypatch, ["--list", "--expect-package", "com.example.app"]) == 1


def test_expect_package_names_what_is_posting_when_it_fails(recorded, fake_adb):
    """A bare 'not found' leaves an agent nowhere to go."""
    fake_adb.when(
        "cmd", "notification", "list", returncode=0, stdout=recorded.text("cmd_notification_list")
    )
    found, result = push_notification.NotificationTester().expect_package("com.example.app")
    assert found is False
    assert result["packages_present"] == ["android"]
    assert "android" in result["error"]


def test_expect_package_requires_list(monkeypatch, fake_adb):
    """Silently ignoring a misplaced flag is the S14 failure mode."""
    argv = ["--post", "--tag", "t", "--text", "h", "--expect-package", "x"]
    assert _run_main(monkeypatch, argv) == 2


# ---------------------------------------------------------------------------
# POST_NOTIFICATIONS — a real, grantable permission.
# ---------------------------------------------------------------------------


def test_grant_uses_pm_grant_with_the_real_permission(fake_adb, recorded):
    fake_adb.when("dumpsys", "package", stdout=recorded.text("dumpsys_package_permissions"))

    success, result = push_notification.NotificationTester().set_post_permission(
        "com.google.android.deskclock", granted=True
    )
    assert success is True
    assert result["permission"] == "android.permission.POST_NOTIFICATIONS"
    assert result["verified"] is True
    issued = fake_adb.commands_matching("pm", "grant")
    assert issued, f"no pm grant issued: {fake_adb.calls}"
    assert "android.permission.POST_NOTIFICATIONS" in issued[0]


def test_revoke_uses_pm_revoke(fake_adb):
    push_notification.NotificationTester().set_post_permission("com.example.app", granted=False)
    assert fake_adb.commands_matching("pm", "revoke"), f"no pm revoke issued: {fake_adb.calls}"


def test_a_refused_grant_is_a_failure(recorded, fake_adb):
    """`pm grant` refusing, in the wording and at the exit status it really uses.

    Recorded: granting an install-time permission answers "Exception occurred
    while executing 'grant':" plus a java.lang.SecurityException and a full
    stack trace, on stderr, and exits 255. The literal this replaced said
    "Operation not allowed: java.lang.SecurityException: not requested" at exit
    0 -- neither the wording nor the status Android uses.
    """
    refusal = recorded.text("pm_grant_not_changeable")
    fake_adb.when("pm", "grant", returncode=255, stderr=refusal)

    success, result = push_notification.NotificationTester().set_post_permission(
        "com.example.app", granted=True
    )
    assert success is False
    assert "SecurityException" in result["error"]


def test_a_grant_that_did_nothing_is_not_a_success(recorded, fake_adb):
    """`pm grant` of a permission the app never requested is a silent no-op.

    Recorded as pm_grant_not_requested: no output, exit 0, and the permission
    still not held. "Exit 0 and nothing printed" was the success test, and it
    is exactly what the no-op looks like, so a grant that never happened was
    reported as done. The proof has to come from the state afterwards --
    dumpsys_package_after_silent_grant, captured immediately after that same
    command, does not mention POST_NOTIFICATIONS at all.
    """
    silent = recorded.text("pm_grant_not_requested")
    assert silent.strip() == "exit=0", "the fixture no longer records a silent success"
    after = recorded.text("dumpsys_package_after_silent_grant")
    assert "android.permission.POST_NOTIFICATIONS" not in after

    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=after)

    success, result = push_notification.NotificationTester().set_post_permission(
        "com.example.composefixture", granted=True
    )
    assert success is False
    assert result["verified"] is False
    assert "does not request" in result["error"]


def test_a_grant_is_read_back_from_the_device(fake_adb, recorded):
    """The read-back is a real dumpsys call, not an assumption."""
    fake_adb.when("dumpsys", "package", stdout=recorded.text("dumpsys_package_permissions"))

    push_notification.NotificationTester().set_post_permission(
        "com.google.android.deskclock", granted=True
    )

    assert fake_adb.commands_matching(
        "dumpsys", "package"
    ), f"grant never read the state back: {fake_adb.calls}"


def test_a_read_back_that_could_not_run_is_not_a_success(fake_adb):
    """No evidence is not evidence of success."""
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", returncode=1, stderr="device offline")

    success, result = push_notification.NotificationTester().set_post_permission(
        "com.example.app", granted=True
    )
    assert success is False
    assert "could not be read back" in result["error"]


def test_permission_cli_exit_codes(fake_adb, monkeypatch, recorded):
    fake_adb.when("dumpsys", "package", stdout=recorded.text("dumpsys_package_permissions"))
    argv = ["--grant-permission", "--package", "com.google.android.deskclock"]
    assert _run_main(monkeypatch, argv) == 0
    fake_adb.when("pm", "revoke", returncode=255, stderr="Unknown permission")
    assert _run_main(monkeypatch, ["--revoke-permission", "--package", "com.example.app"]) == 1


def test_permission_actions_require_a_package(monkeypatch, fake_adb):
    assert _run_main(monkeypatch, ["--grant-permission"]) == 2


# ---------------------------------------------------------------------------
# Subprocess hygiene: no host shell, bounded, explicit.
# ---------------------------------------------------------------------------


def test_every_subprocess_run_passes_check_and_timeout_explicitly():
    """An unbounded adb call wedges the connection for whatever runs next."""
    runs = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "run"
    ]
    assert runs, "module no longer calls subprocess.run"
    for node in runs:
        keywords = {kw.arg for kw in node.keywords}
        assert "check" in keywords, f"subprocess.run without explicit check=: {ast.unparse(node)}"
        assert "timeout" in keywords, f"subprocess.run without timeout=: {ast.unparse(node)}"
        assert "shell" not in keywords, f"subprocess.run with shell=: {ast.unparse(node)}"


def test_runtime_calls_are_bounded_and_not_shelled(fake_adb):
    tester = push_notification.NotificationTester()
    tester.post("t", "hi")
    tester.set_post_permission("com.example.app", granted=True)

    assert fake_adb.kwargs, "nothing was executed"
    for kwargs in fake_adb.kwargs:
        assert kwargs.get("shell") in (None, False)
        assert kwargs.get("check") is False
        assert isinstance(kwargs.get("timeout"), int | float)
    for cmd in fake_adb.calls:
        assert cmd[0] == "adb"


def test_a_hung_adb_is_reported_as_a_failure(monkeypatch):
    def _timeout(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, push_notification.ADB_TIMEOUT)

    monkeypatch.setattr(push_notification.subprocess, "run", _timeout)
    success, result = push_notification.NotificationTester().post("t", "hi")
    assert success is False
    assert "timed out" in result["error"]


def test_a_missing_adb_is_reported_as_a_failure(monkeypatch):
    def _missing(_cmd, **_kwargs):
        raise FileNotFoundError("adb")

    monkeypatch.setattr(push_notification.subprocess, "run", _missing)
    success, result = push_notification.NotificationTester().list_posted()
    assert success is False
    assert "adb" in result["error"]


# ---------------------------------------------------------------------------
# Device-shell quoting: every argument crosses `sh -c` on the device.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["tag", "text"])
def test_post_arguments_are_quoted_for_the_device_shell(fake_adb, field):
    """`adb shell` rejoins argv and the device re-parses it."""
    kwargs = {"tag": "t", "text": "hi", field: "x;id"}
    push_notification.NotificationTester().post(kwargs["tag"], kwargs["text"], verify=False)

    posted = fake_adb.commands_matching("cmd", "notification", "post")[0]
    payloads = [token for token in posted if "id" in token and "x" in token]
    assert payloads, f"injected value never reached the command: {posted}"
    for payload in payloads:
        assert not payload.startswith("x;"), f"unquoted payload reached the device: {payload!r}"


def test_package_is_quoted_before_reaching_pm(fake_adb):
    """A package name harvested from earlier tool output is untrusted text."""
    push_notification.NotificationTester().set_post_permission("x;id", granted=True)
    issued = fake_adb.commands_matching("pm", "grant")[0]
    assert "x;id" not in issued, f"unquoted package reached the device: {issued}"


# ---------------------------------------------------------------------------
# The promise itself: every surface says who actually posted.
# ---------------------------------------------------------------------------


def test_help_states_that_the_post_is_not_the_app(scripts_dir):
    result = subprocess.run(
        [sys.executable, str(scripts_dir / "push_notification.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "com.android.shell" in result.stdout
    assert "shell_cmd" in result.stdout
    assert "NOT" in result.stdout


def test_post_result_carries_the_caveat(fake_adb):
    _success, result = push_notification.NotificationTester().post("t", "hi", verify=False)
    assert "com.android.shell" in result["caveat"]
    assert "not exercised" in result["caveat"].lower()


def test_concise_output_names_the_posting_package(fake_adb, monkeypatch, capsys):
    fake_adb.when("cmd", "notification", "post", returncode=0)
    _run_main(monkeypatch, ["--post", "--tag", "t", "--text", "hi", "--no-verify"])
    assert "com.android.shell" in capsys.readouterr().out


def test_json_output_is_machine_readable(recorded, fake_adb, monkeypatch, capsys):
    fake_adb.when(
        "cmd", "notification", "list", returncode=0, stdout=recorded.text("cmd_notification_list")
    )
    _run_main(monkeypatch, ["--list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["action"] == "list"
    assert payload["packages"] == ["android"]


def test_serial_is_resolved_through_the_shared_helper(fake_adb, monkeypatch):
    """Serial resolution is centralised; a script must not re-implement it."""
    seen: list[str | None] = []

    def _resolve(identifier):
        seen.append(identifier)
        return "emulator-5554"

    monkeypatch.setattr(push_notification, "resolve_device_identifier", _resolve)
    monkeypatch.setattr(
        push_notification.sys, "argv", ["push_notification.py", "--list", "--serial", "emulator"]
    )
    with pytest.raises(SystemExit):
        push_notification.main()

    assert seen == ["emulator"]
    assert fake_adb.calls[0][:3] == ["adb", "-s", "emulator-5554"]
