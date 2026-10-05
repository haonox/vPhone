"""Unit tests for ADB backend discovery and session opening."""

from __future__ import annotations

import pytest

from vphone.device.adb.backend import AdbDeviceBackend
from vphone.device.errors import DeviceNotFoundError, DeviceUnauthorizedError
from vphone.device.models import CommandResult


class FakeRunner:
    def __init__(self, output: bytes, *, helper_installed: bool = True):
        """Set the ADB device-listing bytes returned by this fake."""
        self.output = output
        self.helper_installed = helper_installed
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, **kwargs):
        """Return device discovery and helper setup responses."""
        command = tuple(args)
        self.calls.append(command)
        if command == ("devices", "-l"):
            stdout = self.output
        elif command[0] == "shell" and "cmd package list" in command[1]:
            stdout = b"package:dev.vphone.input versionCode:6\n" if self.helper_installed else b""
        else:
            stdout = b"Success\n"
            if command[0] == "install":
                self.helper_installed = True
        return CommandResult(command, 0, stdout, b"", 0.1)


def test_backend_opens_ready_device() -> None:
    """Verify backend opens ready device."""
    backend = AdbDeviceBackend(
        runner=FakeRunner(b"List of devices attached\nserial-1\tdevice model:Pixel\n")
    )

    with backend.open("serial-1") as session:
        assert session.descriptor.device_id == "serial-1"


def test_backend_normalizes_device_id_before_lookup() -> None:
    """Verify backend normalizes device id before lookup."""
    backend = AdbDeviceBackend(
        runner=FakeRunner(b"List of devices attached\nserial-1\tdevice model:Pixel\n")
    )

    with backend.open("  serial-1\t") as session:
        assert session.descriptor.device_id == "serial-1"


def test_backend_installs_input_helper_before_returning_session() -> None:
    """Verify one-time deployment happens before callers can focus an editor."""
    runner = FakeRunner(
        b"List of devices attached\nserial-1\tdevice model:Pixel\n",
        helper_installed=False,
    )
    backend = AdbDeviceBackend(runner=runner)

    with backend.open("serial-1"):
        pass

    assert any(command[:3] == ("install", "--no-streaming", "-r") for command in runner.calls)


def test_backend_rejects_non_string_device_id() -> None:
    """Verify backend rejects non string device id."""
    backend = AdbDeviceBackend(runner=FakeRunner(b"List of devices attached\n"))

    with pytest.raises(TypeError, match="must be a string"):
        backend.open(123)  # type: ignore[arg-type]


def test_backend_rejects_unauthorized_device() -> None:
    """Verify backend rejects unauthorized device."""
    backend = AdbDeviceBackend(
        runner=FakeRunner(b"List of devices attached\nserial-1\tunauthorized\n")
    )

    with pytest.raises(DeviceUnauthorizedError):
        backend.open("serial-1")


def test_backend_rejects_missing_device() -> None:
    """Verify backend rejects missing device."""
    backend = AdbDeviceBackend(runner=FakeRunner(b"List of devices attached\n"))

    with pytest.raises(DeviceNotFoundError):
        backend.open("missing")
