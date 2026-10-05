"""Low-level, non-retrying Android input primitives."""

from __future__ import annotations

import base64
import importlib.resources
import re
import time

from vphone.device.adb.runner import AdbRunner
from vphone.device.errors import DeviceCommandTimeoutError, DeviceError, InputError
from vphone.device.models import KeyCode, Point, PrimitiveResult

_IME_HELPER = "resources/vphone-ime-input.apk"
_IME_PACKAGE = "dev.vphone.input"
_IME_SERVICE = "dev.vphone.input/.VPhoneInputMethodService"
_IME_COMMIT_ACTION = "dev.vphone.input.COMMIT_TEXT"
_IME_REPLACE_ACTION = "dev.vphone.input.REPLACE_TEXT"
_IME_HIDE_ACTION = "dev.vphone.input.HIDE_INPUT"
_IME_VERSION_CODE = 5
_LONG_PRESS_DURATION_MS = 1000
_BROADCAST_RESULT = re.compile(rb"Broadcast completed: result=(-?\d+)(?:, data=\"([^\"]*)\")?")
_IME_COMPONENT = re.compile(r"^[A-Za-z0-9._]+/[A-Za-z0-9._$]+$")


def tap(
    runner: AdbRunner,
    serial: str,
    point: Point,
    *,
    timeout: float = 5.0,
) -> PrimitiveResult:
    """Send one coordinate tap through ADB.

    Args:
        runner: ADB command runner.
        serial: Target device serial.
        point: Screen pixel to tap.
        timeout: Maximum command duration in seconds.

    Returns:
        The completed input primitive and its duration.
    """
    result = runner.run(
        ("shell", "input", "tap", str(point.x), str(point.y)),
        serial=serial,
        timeout=timeout,
    )
    return PrimitiveResult("tap", result.duration_seconds)


def long_press(
    runner: AdbRunner,
    serial: str,
    point: Point,
    *,
    timeout: float = 5.0,
) -> PrimitiveResult:
    """Hold one screen coordinate long enough to trigger a long press.

    Args:
        runner: ADB command runner.
        serial: Target device serial.
        point: Screen pixel to hold.
        timeout: Maximum command duration in seconds.

    Returns:
        The completed long-press primitive and its duration.
    """
    result = runner.run(
        (
            "shell",
            "input",
            "swipe",
            str(point.x),
            str(point.y),
            str(point.x),
            str(point.y),
            str(_LONG_PRESS_DURATION_MS),
        ),
        serial=serial,
        timeout=timeout,
    )
    return PrimitiveResult("long_press", result.duration_seconds)


def swipe(
    runner: AdbRunner,
    serial: str,
    start: Point,
    end: Point,
    *,
    duration_ms: int = 300,
    timeout: float = 5.0,
) -> PrimitiveResult:
    """Send one timed swipe between screen pixels.

    Args:
        runner: ADB command runner.
        serial: Target device serial.
        start: Initial screen pixel.
        end: Final screen pixel.
        duration_ms: Swipe duration in milliseconds.
        timeout: Maximum command duration in seconds.

    Returns:
        The completed input primitive and its duration.

    Raises:
        InputError: If the swipe duration is invalid.
    """
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise InputError("duration_ms must be an integer")
    if not 1 <= duration_ms <= 10_000:
        raise InputError("duration_ms must be between 1 and 10000")
    result = runner.run(
        (
            "shell",
            "input",
            "swipe",
            str(start.x),
            str(start.y),
            str(end.x),
            str(end.y),
            str(duration_ms),
        ),
        serial=serial,
        timeout=timeout,
    )
    return PrimitiveResult("swipe", result.duration_seconds)


def key_event(
    runner: AdbRunner,
    serial: str,
    key: KeyCode | int,
    *,
    timeout: float = 5.0,
) -> PrimitiveResult:
    """Send one Android key event.

    Args:
        runner: ADB command runner.
        serial: Target device serial.
        key: Named Android key or non-negative numeric keycode.
        timeout: Maximum command duration in seconds.

    Returns:
        The completed input primitive and its duration.

    Raises:
        InputError: If the key identifier is invalid.
    """
    if isinstance(key, KeyCode):
        value = key.value
    elif isinstance(key, int) and not isinstance(key, bool) and key >= 0:
        value = str(key)
    else:
        raise InputError("key must be a KeyCode or non-negative integer")
    result = runner.run(("shell", "input", "keyevent", value), serial=serial, timeout=timeout)
    return PrimitiveResult("key_event", result.duration_seconds)


def input_text(
    runner: AdbRunner,
    serial: str,
    text: str,
    *,
    timeout: float = 10.0,
) -> PrimitiveResult:
    """Commit printable text through the focused editor's input connection.

    Args:
        runner: ADB command runner.
        serial: Target device serial.
        text: Printable text to enter.
        timeout: Overall timeout budget in seconds.

    Returns:
        The completed input primitive and its duration.

    Raises:
        InputError: If text is invalid, no editor is focused, or the editor rejects it.
        DeviceCommandTimeoutError: If the overall budget is exhausted.
    """
    return _edit_text(
        runner,
        serial,
        text,
        timeout=timeout,
        action=_IME_COMMIT_ACTION,
        operation="input_text",
    )


def replace_text(
    runner: AdbRunner,
    serial: str,
    text: str,
    *,
    timeout: float = 10.0,
) -> PrimitiveResult:
    """Replace all text through the focused editor's input connection.

    Args:
        runner: ADB command runner.
        serial: Target device serial.
        text: Printable replacement text.
        timeout: Overall timeout budget in seconds.

    Returns:
        The completed replacement primitive and its duration.

    Raises:
        InputError: If text is invalid, no editor is focused, or replacement is rejected.
        DeviceCommandTimeoutError: If the overall budget is exhausted.
    """
    return _edit_text(
        runner,
        serial,
        text,
        timeout=timeout,
        action=_IME_REPLACE_ACTION,
        operation="replace_text",
    )


def start_text_input(
    runner: AdbRunner,
    serial: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Select the headless helper for a task and return the previous IME."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    previous_ime, installed_version = _read_ime_state(runner, serial, deadline=deadline)
    if _IME_COMPONENT.fullmatch(previous_ime) is None:
        raise InputError("the device did not report a current input method")
    if installed_version != _IME_VERSION_CODE:
        raise InputError("the input method helper is not prepared for this device session")
    if previous_ime == _IME_SERVICE:
        return previous_ime

    try:
        _select_ime_helper(runner, serial, deadline=deadline)
    except DeviceError as operation_error:
        try:
            _restore_input_method(
                runner,
                serial,
                previous_ime,
                timeout=min(5.0, _remaining_timeout(deadline)),
            )
        except DeviceError as restore_error:
            operation_error.add_note(f"failed to restore input method: {restore_error}")
        raise
    return previous_ime


def stop_text_input(
    runner: AdbRunner,
    serial: str,
    previous_ime: str,
    *,
    timeout: float = 10.0,
) -> None:
    """Hide the helper and restore the IME selected before the task."""
    if _IME_COMPONENT.fullmatch(previous_ime) is None:
        raise InputError("the previous input method is invalid")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    hide_error: DeviceError | None = None
    restore_error: DeviceError | None = None
    try:
        _send_hide(runner, serial, deadline=deadline)
    except DeviceError as exc:
        hide_error = exc
    finally:
        if previous_ime != _IME_SERVICE:
            try:
                _restore_input_method(
                    runner,
                    serial,
                    previous_ime,
                    timeout=min(5.0, _remaining_timeout(deadline)),
                )
            except DeviceError as exc:
                restore_error = exc

    if hide_error is not None:
        if restore_error is not None:
            hide_error.add_note(f"failed to restore input method: {restore_error}")
        raise hide_error
    if restore_error is not None:
        raise InputError("the previous input method was not restored") from restore_error


def _edit_text(
    runner: AdbRunner,
    serial: str,
    text: str,
    *,
    timeout: float,
    action: str,
    operation: str,
) -> PrimitiveResult:
    """Run one validated editor operation while preserving the selected IME."""
    if not isinstance(text, str) or not text:
        raise InputError("text must be a non-empty string")
    if not text.isprintable():
        raise InputError("text must contain printable characters only")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    started = time.monotonic()
    deadline = started + timeout
    previous_ime, installed_version = _read_ime_state(runner, serial, deadline=deadline)
    if _IME_COMPONENT.fullmatch(previous_ime) is None:
        raise InputError("the device did not report a current input method")

    if installed_version != _IME_VERSION_CODE:
        raise InputError("the input method helper is not prepared for this device session")

    switched = previous_ime != _IME_SERVICE
    operation_error: DeviceError | None = None
    restore_error: DeviceError | None = None
    try:
        if switched:
            _select_ime_helper(runner, serial, deadline=deadline)
        _send_text(runner, serial, text, action=action, deadline=deadline)
    except DeviceError as exc:
        operation_error = exc
    finally:
        if switched:
            try:
                _restore_input_method(runner, serial, previous_ime)
            except DeviceError as exc:
                restore_error = exc

    if operation_error is not None:
        if restore_error is not None:
            operation_error.add_note(f"failed to restore input method: {restore_error}")
        raise operation_error
    if restore_error is not None:
        raise InputError(
            "text was committed but the previous input method was not restored"
        ) from restore_error
    return PrimitiveResult(operation, time.monotonic() - started)


def prepare_text_input(
    runner: AdbRunner,
    serial: str,
    *,
    timeout: float = 120.0,
) -> None:
    """Install or update the helper before any editor is focused for input."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    version_command = f"cmd package list packages --show-versioncode --user current {_IME_PACKAGE}"
    result = runner.run(
        ("shell", version_command),
        serial=serial,
        timeout=_remaining_timeout(deadline),
    )
    installed_version = _parse_installed_version(result.stdout)
    if installed_version != _IME_VERSION_CODE:
        _install_ime_helper(runner, serial, deadline=deadline)


def _read_ime_state(
    runner: AdbRunner,
    serial: str,
    *,
    deadline: float,
) -> tuple[str, int | None]:
    """Read the selected IME and installed helper version in one device round trip."""
    state_command = (
        "settings get --user current secure default_input_method; "
        "cmd package list packages --show-versioncode --user current "
        f"{_IME_PACKAGE}"
    )
    result = runner.run(
        ("shell", state_command),
        serial=serial,
        timeout=_remaining_timeout(deadline),
    )
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    previous_ime = lines[0].strip() if lines else ""
    return previous_ime, _parse_installed_version("\n".join(lines[1:]).encode())


def _parse_installed_version(output: bytes) -> int | None:
    """Extract the helper version from package-manager output."""
    text = output.decode("utf-8", errors="replace")
    match = re.search(rf"package:{re.escape(_IME_PACKAGE)}\s+versionCode:(\d+)", text)
    return int(match.group(1)) if match is not None else None


def _install_ime_helper(
    runner: AdbRunner,
    serial: str,
    *,
    deadline: float,
) -> None:
    """Install or update the packaged, non-visual input method."""
    helper = importlib.resources.files("vphone.device.adb").joinpath(_IME_HELPER)
    with importlib.resources.as_file(helper) as helper_path:
        if not helper_path.is_file():
            raise InputError("the packaged input method helper is missing")
        runner.run(
            ("install", "--no-streaming", "-r", str(helper_path)),
            serial=serial,
            timeout=_remaining_timeout(deadline),
        )


def _select_ime_helper(
    runner: AdbRunner,
    serial: str,
    *,
    deadline: float,
) -> None:
    """Enable and select the helper with one static remote shell command."""
    runner.run(
        (
            "shell",
            f"ime enable {_IME_SERVICE} >/dev/null && ime set {_IME_SERVICE}",
        ),
        serial=serial,
        timeout=_remaining_timeout(deadline),
    )


def _send_text(
    runner: AdbRunner,
    serial: str,
    text: str,
    *,
    action: str,
    deadline: float,
) -> None:
    """Send an editor operation to the helper and require acknowledgement."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    while True:
        result = runner.run(
            (
                "shell",
                "am",
                "broadcast",
                "--receiver-foreground",
                "-p",
                _IME_PACKAGE,
                "-a",
                action,
                "--es",
                "text_base64",
                encoded,
            ),
            serial=serial,
            timeout=_remaining_timeout(deadline),
        )
        match = _BROADCAST_RESULT.search(result.stdout + result.stderr)
        code = int(match.group(1)) if match is not None else 0
        detail = (
            match.group(2).decode("utf-8", errors="replace")
            if match is not None and match.group(2) is not None
            else ""
        )
        if code == 1:
            return
        if code in (3, 4):
            raise InputError(detail or "the focused editor rejected text")
        if deadline - time.monotonic() <= 0.2:
            raise InputError(detail or "no focused editor accepted text")
        time.sleep(0.1)


def _send_hide(runner: AdbRunner, serial: str, *, deadline: float) -> None:
    """Ask the helper to clear the system's soft-input visibility request."""
    result = runner.run(
        (
            "shell",
            "am",
            "broadcast",
            "--receiver-foreground",
            "-p",
            _IME_PACKAGE,
            "-a",
            _IME_HIDE_ACTION,
        ),
        serial=serial,
        timeout=_remaining_timeout(deadline),
    )
    match = _BROADCAST_RESULT.search(result.stdout + result.stderr)
    code = int(match.group(1)) if match is not None else 0
    if code != 1:
        raise InputError("the input method helper did not acknowledge hide")


def _restore_input_method(
    runner: AdbRunner,
    serial: str,
    previous_ime: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Restore the user's IME and disable the helper even after input failure."""
    runner.run(
        (
            "shell",
            f"ime set {previous_ime} >/dev/null && ime disable {_IME_SERVICE} >/dev/null",
        ),
        serial=serial,
        timeout=timeout,
    )


def _remaining_timeout(deadline: float) -> float:
    """Return remaining command time or fail after budget exhaustion.

    Args:
        deadline: Monotonic timestamp at which the operation expires.

    Returns:
        Positive seconds remaining in the operation budget.

    Raises:
        DeviceCommandTimeoutError: If the deadline has passed.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeviceCommandTimeoutError("text input exhausted its timeout budget")
    return remaining
