"""Unit tests for low-level ADB input command construction."""

from __future__ import annotations

import base64

import pytest

from vphone.device.adb import input as adb_input
from vphone.device.errors import InputError
from vphone.device.models import CommandResult, KeyCode, Point


class FakeRunner:
    def __init__(self, *, installed: bool = True, broadcast: bytes | None = None):
        """Start an empty log of simulated ADB commands."""
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.installed = installed
        self.broadcast = broadcast or b'Broadcast completed: result=1, data="committed"\n'
        self.current_ime = "com.example.ime/.Keyboard"

    def run(self, args, **kwargs):
        """Record a command and simulate successful ADB output."""
        self.calls.append((tuple(args), kwargs))
        if args[0] == "shell" and "settings get" in args[1]:
            stdout = f"{self.current_ime}\n".encode()
            if self.installed:
                stdout += b"package:dev.vphone.input versionCode:6\n"
        elif args[0] == "shell" and "cmd package list" in args[1]:
            stdout = b"package:dev.vphone.input versionCode:6\n" if self.installed else b""
        elif args[:3] == ("shell", "am", "broadcast"):
            stdout = self.broadcast
        else:
            stdout = b"Success\n" if args[0] == "install" else b""
        if args[0] == "shell" and "ime set dev.vphone.input/.VPhoneInputMethodService" in args[1]:
            self.current_ime = "dev.vphone.input/.VPhoneInputMethodService"
        elif args[0] == "shell" and "ime set com.example.ime/.Keyboard" in args[1]:
            self.current_ime = "com.example.ime/.Keyboard"
        return CommandResult(tuple(args), 0, stdout, b"", 0.1)


def test_tap_builds_input_command() -> None:
    """Verify tap builds input command."""
    runner = FakeRunner()

    result = adb_input.tap(runner, "serial", Point(12, 34))

    assert runner.calls[0][0] == ("shell", "input", "tap", "12", "34")
    assert result.operation == "tap"


def test_long_press_builds_fixed_duration_hold_command() -> None:
    """Represent a long press as a one-second swipe that does not move."""
    runner = FakeRunner()

    result = adb_input.long_press(runner, "serial", Point(12, 34))

    assert runner.calls[0][0] == (
        "shell",
        "input",
        "swipe",
        "12",
        "34",
        "12",
        "34",
        "1000",
    )
    assert result.operation == "long_press"


def test_swipe_validates_duration() -> None:
    """Verify swipe validates duration."""
    with pytest.raises(InputError, match="between"):
        adb_input.swipe(FakeRunner(), "serial", Point(0, 0), Point(1, 1), duration_ms=0)


def test_key_event_accepts_named_key() -> None:
    """Verify key event accepts named key."""
    runner = FakeRunner()

    adb_input.key_event(runner, "serial", KeyCode.BACK)

    assert runner.calls[0][0] == ("shell", "input", "keyevent", "4")


@pytest.mark.parametrize("text", ["hello phone", "你好", "it's & safe", "vphone%s42"])
def test_input_text_commits_all_text_through_the_ime(text: str) -> None:
    """Verify every supported character set uses the same editor protocol."""
    runner = FakeRunner()

    result = adb_input.input_text(runner, "serial", text)

    assert result.operation == "input_text"
    broadcast = next(
        call[0] for call in runner.calls if call[0][:3] == ("shell", "am", "broadcast")
    )
    encoded = broadcast[broadcast.index("text_base64") + 1]
    assert base64.b64decode(encoded).decode("utf-8") == text
    assert not any("uiautomator" in call[0] for call in runner.calls)
    assert not any(call[0][:3] == ("shell", "input", "text") for call in runner.calls)


def test_replace_text_requests_atomic_editor_replacement() -> None:
    """Send replacement text through the helper's dedicated action."""
    runner = FakeRunner()

    result = adb_input.replace_text(runner, "serial", "new value")

    assert result.operation == "replace_text"
    broadcast = next(
        call[0] for call in runner.calls if call[0][:3] == ("shell", "am", "broadcast")
    )
    assert "dev.vphone.input.REPLACE_TEXT" in broadcast
    encoded = broadcast[broadcast.index("text_base64") + 1]
    assert base64.b64decode(encoded).decode("utf-8") == "new value"


def test_input_text_selects_and_restores_the_previous_ime() -> None:
    """Verify the helper is enabled only around the acknowledged commit."""
    runner = FakeRunner()

    adb_input.input_text(runner, "serial", "hello")

    commands = [call[0] for call in runner.calls]
    assert "ime enable dev.vphone.input/.VPhoneInputMethodService" in commands[1][1]
    assert "ime set dev.vphone.input/.VPhoneInputMethodService" in commands[1][1]
    assert commands[2][:3] == ("shell", "am", "broadcast")
    assert "ime set com.example.ime/.Keyboard" in commands[3][1]
    assert "ime disable dev.vphone.input/.VPhoneInputMethodService" in commands[3][1]


def test_task_text_input_keeps_helper_selected_until_explicit_stop() -> None:
    """Select once before focus, reuse the helper, then hide and restore it once."""
    runner = FakeRunner()

    previous_ime = adb_input.start_text_input(runner, "serial")
    adb_input.input_text(runner, "serial", "hello")
    adb_input.replace_text(runner, "serial", "world")
    adb_input.stop_text_input(runner, "serial", previous_ime)

    commands = [call[0] for call in runner.calls]
    selects = [command for command in commands if "ime set dev.vphone.input" in command[-1]]
    restores = [command for command in commands if "ime set com.example.ime" in command[-1]]
    broadcasts = [command for command in commands if command[:3] == ("shell", "am", "broadcast")]
    assert previous_ime == "com.example.ime/.Keyboard"
    assert len(selects) == 1
    assert len(restores) == 1
    assert [command[command.index("-a") + 1] for command in broadcasts] == [
        "dev.vphone.input.COMMIT_TEXT",
        "dev.vphone.input.REPLACE_TEXT",
        "dev.vphone.input.HIDE_INPUT",
    ]


def test_task_text_input_restores_previous_ime_when_hide_is_rejected() -> None:
    """Prioritize restoring the user's keyboard even if hide acknowledgement fails."""
    runner = FakeRunner(broadcast=b'Broadcast completed: result=4, data="rejected"\n')
    previous_ime = adb_input.start_text_input(runner, "serial")

    with pytest.raises(InputError, match="did not acknowledge hide"):
        adb_input.stop_text_input(runner, "serial", previous_ime)

    assert runner.current_ime == "com.example.ime/.Keyboard"
    assert "ime disable dev.vphone.input/.VPhoneInputMethodService" in runner.calls[-1][0][1]


def test_input_text_requires_the_session_to_be_prepared() -> None:
    """Verify deployment cannot unexpectedly replace an already-focused screen."""
    runner = FakeRunner(installed=False)

    with pytest.raises(InputError, match="not prepared"):
        adb_input.input_text(runner, "serial", "hello")

    assert not any(call[0][0] == "install" for call in runner.calls)


def test_prepare_text_input_installs_the_packaged_ime_when_missing() -> None:
    """Verify session setup deploys the self-contained helper APK."""
    runner = FakeRunner(installed=False)

    adb_input.prepare_text_input(runner, "serial")

    install = runner.calls[1][0]
    assert install[:3] == ("install", "--no-streaming", "-r")
    assert install[3].endswith("vphone-ime-input.apk")


def test_prepare_text_input_skips_an_up_to_date_helper() -> None:
    """Verify normal session setup does not reinstall or disturb the foreground app."""
    runner = FakeRunner()

    adb_input.prepare_text_input(runner, "serial")

    assert len(runner.calls) == 1


def test_input_text_reports_editor_rejection_and_restores_the_ime() -> None:
    """Verify a rejected commit is visible and cannot strand the helper IME."""
    runner = FakeRunner(
        broadcast=b'Broadcast completed: result=4, data="input connection rejected text"\n'
    )

    with pytest.raises(InputError, match="rejected text"):
        adb_input.input_text(runner, "serial", "你好")

    assert "ime set com.example.ime/.Keyboard" in runner.calls[-1][0][1]


def test_input_text_reports_a_missing_focused_editor() -> None:
    """Verify a fallback connection is not reported as successful input."""
    runner = FakeRunner(broadcast=b'Broadcast completed: result=2, data="no input connection"\n')

    with pytest.raises(InputError, match="no input connection"):
        adb_input.input_text(runner, "serial", "你好", timeout=0.15)

    assert "ime set com.example.ime/.Keyboard" in runner.calls[-1][0][1]


def test_input_text_rejects_control_characters() -> None:
    """Verify input text rejects control characters."""
    with pytest.raises(InputError, match="printable"):
        adb_input.input_text(FakeRunner(), "serial", "first\nsecond")
