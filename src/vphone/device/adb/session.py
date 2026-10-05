"""Synchronous session for one ready ADB device."""

from __future__ import annotations

import threading
import time
from types import TracebackType
from typing import Self

from vphone.device.adb import input as adb_input
from vphone.device.adb.runner import AdbRunner
from vphone.device.adb.screenshot import capture_screen
from vphone.device.errors import DeviceClosedError
from vphone.device.models import (
    DeviceCapabilities,
    DeviceDescriptor,
    DeviceHealth,
    DeviceState,
    KeyCode,
    Point,
    PrimitiveResult,
    ScreenFrame,
)

_DEVICE_LOCKS_GUARD = threading.Lock()
_DEVICE_LOCKS: dict[str, threading.RLock] = {}


def _lock_for_device(device_id: str) -> threading.RLock:
    """Return the shared in-process lock for an ADB serial."""
    with _DEVICE_LOCKS_GUARD:
        lock = _DEVICE_LOCKS.get(device_id)
        if lock is None:
            lock = threading.RLock()
            _DEVICE_LOCKS[device_id] = lock
        return lock


class AdbDeviceSession:
    def __init__(self, runner: AdbRunner, descriptor: DeviceDescriptor):
        """Bind a ready device descriptor to a shared ADB runner.

        Args:
            runner: Runner used for all commands in this session.
            descriptor: Descriptor of the device selected by the backend.
        """
        self._runner = runner
        self.descriptor = descriptor
        self.capabilities = DeviceCapabilities()
        self._lock = _lock_for_device(descriptor.device_id)
        self._closed = False
        self._task_input_active = False
        self._previous_input_method: str | None = None

    def health_check(self, *, timeout: float = 5.0) -> DeviceHealth:
        """Query whether the selected device is still ready.

        Args:
            timeout: Maximum ADB command duration in seconds.

        Returns:
            Current readiness and observed device state.
        """
        with self._lock:
            self._ensure_open()
            result = self._runner.run(
                ("get-state",), serial=self.descriptor.device_id, timeout=timeout, check=False
            )
            state_text = result.stdout.decode("utf-8", errors="replace").strip()
            try:
                state = DeviceState(state_text)
            except ValueError:
                combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                folded = combined.casefold()
                state = (
                    DeviceState.UNAUTHORIZED
                    if "unauthorized" in folded
                    else DeviceState.OFFLINE
                    if "offline" in folded
                    else DeviceState.UNKNOWN
                )
            return DeviceHealth(
                ready=result.returncode == 0 and state is DeviceState.READY,
                state=state,
                checked_at=time.time(),
                message=result.stderr.decode("utf-8", errors="replace").strip(),
            )

    def capture_screen(self, *, timeout: float = 10.0) -> ScreenFrame:
        """Capture a verified PNG screenshot from this device.

        Args:
            timeout: Maximum ADB command duration in seconds.

        Returns:
            The current screen frame.
        """
        with self._lock:
            self._ensure_open()
            return capture_screen(self._runner, self.descriptor.device_id, timeout=timeout)

    def tap(self, point: Point, *, timeout: float = 5.0) -> PrimitiveResult:
        """Tap one screen pixel through the shared runner.

        Args:
            point: Pixel to tap.
            timeout: Maximum ADB command duration in seconds.

        Returns:
            Result of the input primitive.
        """
        with self._lock:
            self._ensure_open()
            return adb_input.tap(self._runner, self.descriptor.device_id, point, timeout=timeout)

    def long_press(self, point: Point, *, timeout: float = 5.0) -> PrimitiveResult:
        """Long-press one screen pixel through the shared runner.

        Args:
            point: Pixel to hold for the fixed long-press duration.
            timeout: Maximum ADB command duration in seconds.

        Returns:
            Result of the long-press primitive.
        """
        with self._lock:
            self._ensure_open()
            return adb_input.long_press(
                self._runner, self.descriptor.device_id, point, timeout=timeout
            )

    def swipe(
        self,
        start: Point,
        end: Point,
        *,
        duration_ms: int = 300,
        timeout: float = 5.0,
    ) -> PrimitiveResult:
        """Swipe between two screen pixels through the shared runner.

        Args:
            start: Initial pixel.
            end: Final pixel.
            duration_ms: Swipe duration in milliseconds.
            timeout: Maximum ADB command duration in seconds.

        Returns:
            Result of the input primitive.
        """
        with self._lock:
            self._ensure_open()
            return adb_input.swipe(
                self._runner,
                self.descriptor.device_id,
                start,
                end,
                duration_ms=duration_ms,
                timeout=timeout,
            )

    def key_event(self, key: KeyCode | int, *, timeout: float = 5.0) -> PrimitiveResult:
        """Send an Android key event to this device.

        Args:
            key: Named key or numeric Android keycode.
            timeout: Maximum ADB command duration in seconds.

        Returns:
            Result of the input primitive.
        """
        with self._lock:
            self._ensure_open()
            return adb_input.key_event(
                self._runner, self.descriptor.device_id, key, timeout=timeout
            )

    def _prepare_text_input(self, *, timeout: float = 120.0) -> None:
        """Install or update the input helper before text actions begin."""
        with self._lock:
            self._ensure_open()
            adb_input.prepare_text_input(self._runner, self.descriptor.device_id, timeout=timeout)

    def start_text_input(self, *, timeout: float = 10.0) -> None:
        """Keep the headless helper selected until the current task ends."""
        with self._lock:
            self._ensure_open()
            if self._task_input_active:
                return
            previous = adb_input.start_text_input(
                self._runner,
                self.descriptor.device_id,
                timeout=timeout,
            )
            self._previous_input_method = previous
            self._task_input_active = True

    def stop_text_input(self, *, timeout: float = 10.0) -> None:
        """Hide the helper and restore the input method saved for this task."""
        with self._lock:
            self._ensure_open()
            if not self._task_input_active:
                return
            previous = self._previous_input_method
            if previous is None:
                raise RuntimeError("active text input task has no saved input method")
            adb_input.stop_text_input(
                self._runner,
                self.descriptor.device_id,
                previous,
                timeout=timeout,
            )
            self._task_input_active = False
            self._previous_input_method = None

    def input_text(self, text: str, *, timeout: float = 10.0) -> PrimitiveResult:
        """Enter text into the currently focused device field.

        Args:
            text: Printable text to enter.
            timeout: Overall input timeout budget in seconds.

        Returns:
            Result of the input primitive.
        """
        with self._lock:
            self._ensure_open()
            return adb_input.input_text(
                self._runner, self.descriptor.device_id, text, timeout=timeout
            )

    def replace_text(self, text: str, *, timeout: float = 10.0) -> PrimitiveResult:
        """Replace all text in the currently focused device field.

        Args:
            text: Printable replacement text.
            timeout: Overall replacement timeout budget in seconds.

        Returns:
            Result of the replacement primitive.
        """
        with self._lock:
            self._ensure_open()
            return adb_input.replace_text(
                self._runner, self.descriptor.device_id, text, timeout=timeout
            )

    def close(self) -> None:
        """Restore task input state, then reject later device operations."""
        with self._lock:
            if self._closed:
                return
            if self._task_input_active:
                self.stop_text_input()
            self._closed = True

    def __enter__(self) -> Self:
        """Validate and return this session for context-manager use."""
        with self._lock:
            self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session when leaving a context manager.

        Args:
            exc_type: Exception class raised in the context, if any.
            exc: Exception instance raised in the context, if any.
            traceback: Traceback of the exception, if any.
        """
        try:
            self.close()
        except Exception as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"failed to close device session: {cleanup_error}")

    def _ensure_open(self) -> None:
        """Reject operations after the session has been closed."""
        if self._closed:
            raise DeviceClosedError(f"device session is closed: {self.descriptor.device_id}")
