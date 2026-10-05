"""Unit tests for ADB device session lifecycle and serialization."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from vphone.device.adb import input as adb_input
from vphone.device.adb.session import AdbDeviceSession
from vphone.device.errors import DeviceClosedError
from vphone.device.models import (
    CommandResult,
    ConnectionType,
    DeviceDescriptor,
    DeviceState,
)


class FakeRunner:
    def run(self, args, **kwargs):
        """Return a ready-device state for any simulated ADB command."""
        return CommandResult(tuple(args), 0, b"device\n", b"", 0.1)


def descriptor() -> DeviceDescriptor:
    """Build the ready USB descriptor shared by session tests."""
    return DeviceDescriptor("serial", DeviceState.READY, ConnectionType.USB)


def test_health_check_reports_ready_device() -> None:
    """Verify health check reports ready device."""
    session = AdbDeviceSession(FakeRunner(), descriptor())

    health = session.health_check()

    assert health.ready is True
    assert health.state is DeviceState.READY


def test_closed_session_rejects_operations() -> None:
    """Verify closed session rejects operations."""
    session = AdbDeviceSession(FakeRunner(), descriptor())
    session.close()

    with pytest.raises(DeviceClosedError):
        session.health_check()


def test_task_text_input_lifecycle_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select and restore the task IME once even when lifecycle calls repeat."""
    calls = []
    monkeypatch.setattr(
        adb_input,
        "start_text_input",
        lambda runner, serial, timeout: (
            calls.append(("start", serial, timeout)) or "com.example.ime/.Keyboard"
        ),
    )
    monkeypatch.setattr(
        adb_input,
        "stop_text_input",
        lambda runner, serial, previous, timeout: calls.append(("stop", serial, previous, timeout)),
    )
    session = AdbDeviceSession(FakeRunner(), descriptor())

    session.start_text_input(timeout=3)
    session.start_text_input(timeout=3)
    session.stop_text_input(timeout=4)
    session.stop_text_input(timeout=4)

    assert calls == [
        ("start", "serial", 3),
        ("stop", "serial", "com.example.ime/.Keyboard", 4),
    ]


def test_close_restores_active_task_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use device-session cleanup as a final safety net for abandoned tasks."""
    restored = []
    monkeypatch.setattr(
        adb_input,
        "start_text_input",
        lambda runner, serial, timeout: "com.example.ime/.Keyboard",
    )
    monkeypatch.setattr(
        adb_input,
        "stop_text_input",
        lambda runner, serial, previous, timeout: restored.append(previous),
    )
    session = AdbDeviceSession(FakeRunner(), descriptor())

    session.start_text_input()
    session.close()

    assert restored == ["com.example.ime/.Keyboard"]


def test_sessions_for_same_device_serialize_commands() -> None:
    """Verify sessions for same device serialize commands."""

    class ConcurrentRunner:
        def __init__(self):
            """Initialize counters for simultaneous command execution."""
            self.guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def run(self, args, **kwargs):
            """Measure overlapping calls while simulating a slow command."""
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self.guard:
                self.active -= 1
            return CommandResult(tuple(args), 0, b"device\n", b"", 0.02)

    runner = ConcurrentRunner()
    first = AdbDeviceSession(runner, descriptor())
    second = AdbDeviceSession(runner, descriptor())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(session.health_check) for session in (first, second)]
        assert all(future.result().ready for future in futures)

    assert runner.max_active == 1
