"""Backend-independent interfaces for Android devices."""

from __future__ import annotations

from typing import Protocol

from vphone.device.models import (
    DeviceCapabilities,
    DeviceDescriptor,
    DeviceHealth,
    KeyCode,
    Point,
    PrimitiveResult,
    ScreenFrame,
)


class DeviceSession(Protocol):
    descriptor: DeviceDescriptor
    capabilities: DeviceCapabilities

    def health_check(self, *, timeout: float = 5.0) -> DeviceHealth:
        """Return current readiness within the requested timeout."""
        ...

    def capture_screen(self, *, timeout: float = 10.0) -> ScreenFrame:
        """Capture one verified current screen frame."""
        ...

    def tap(self, point: Point, *, timeout: float = 5.0) -> PrimitiveResult:
        """Tap a concrete screen pixel and report command completion."""
        ...

    def long_press(self, point: Point, *, timeout: float = 5.0) -> PrimitiveResult:
        """Long-press a concrete screen pixel and report command completion."""
        ...

    def swipe(
        self,
        start: Point,
        end: Point,
        *,
        duration_ms: int = 300,
        timeout: float = 5.0,
    ) -> PrimitiveResult:
        """Swipe between concrete pixels for the requested duration."""
        ...

    def key_event(self, key: KeyCode | int, *, timeout: float = 5.0) -> PrimitiveResult:
        """Send one Android key event."""
        ...

    def start_text_input(self, *, timeout: float = 10.0) -> None:
        """Select the headless input helper for the current task."""
        ...

    def stop_text_input(self, *, timeout: float = 10.0) -> None:
        """Hide the helper and restore the input method used before the task."""
        ...

    def input_text(self, text: str, *, timeout: float = 10.0) -> PrimitiveResult:
        """Type printable text into the currently focused field."""
        ...

    def replace_text(self, text: str, *, timeout: float = 10.0) -> PrimitiveResult:
        """Replace all text in the currently focused field."""
        ...

    def close(self) -> None:
        """Close this session and prevent further use."""
        ...


class DeviceBackend(Protocol):
    def list_devices(self, *, timeout: float = 5.0) -> list[DeviceDescriptor]:
        """List ADB-visible devices and their states."""
        ...

    def open(
        self,
        device_id: str,
        *,
        timeout: float = 5.0,
        input_setup_timeout: float = 120.0,
    ) -> DeviceSession:
        """Open a session for a ready device with the given serial."""
        ...
