"""Stateful single-step visual planning session."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from types import TracebackType
from typing import Self

from vphone.action import ActionExecutor
from vphone.action.models import (
    ActionKind,
    KeyAction,
    LongPressAction,
    ReplaceTextAction,
    SwipeAction,
    TapAction,
    TextAction,
    WaitAction,
)
from vphone.device.errors import DeviceError
from vphone.device.protocol import DeviceSession
from vphone.perception import PerceptionEngine
from vphone.perception.errors import PerceptionError
from vphone.planner.errors import PlannerError
from vphone.planner.models import (
    ActionDecision,
    DecisionTrace,
    FinishDecision,
    SessionResult,
    SessionStatus,
    StepRecord,
    StopDecision,
)
from vphone.planner.protocol import DecisionModel


class PlannerSession:
    """Advance one task by at most one device action per ``step`` call."""

    def __init__(
        self,
        model: DecisionModel,
        task: str,
        device: DeviceSession,
        *,
        max_actions: int = 12,
        max_seconds: float = 600.0,
        settle_seconds: float = 0.5,
        allowed_kinds: frozenset[ActionKind] | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
    ):
        """Create a stateful visual planning session for one task.

        Args:
            model: Provider that proposes one decision per observation.
            task: Nonempty natural-language task kept for the session lifetime.
            device: Session shared by perception and action execution.
            max_actions: Maximum number of actions to dispatch.
            max_seconds: Overall session budget in seconds.
            settle_seconds: Wait after a completed action before returning.
            allowed_kinds: Permitted L2 action kinds, or all kinds by default.
            on_step: Optional callback after each dispatched action.

        Raises:
            TypeError: If the task is not text.
            ValueError: If the task or runtime limits are invalid.
        """
        if not isinstance(task, str):
            raise TypeError("task must be text")
        task = task.strip()
        if not task:
            raise ValueError("task must be non-empty text")
        if type(max_actions) is not int or max_actions < 1:
            raise ValueError("max_actions must be a positive integer")
        if (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds)
            or max_seconds <= 0
            or isinstance(settle_seconds, bool)
            or not isinstance(settle_seconds, (int, float))
            or not math.isfinite(settle_seconds)
            or settle_seconds < 0
        ):
            raise ValueError("time limits must be valid")

        self._model = model
        self._task = task
        self._device = device
        self._max_actions = max_actions
        self._max_seconds = float(max_seconds)
        self._settle_seconds = float(settle_seconds)
        self._allowed_kinds = frozenset(ActionKind) if allowed_kinds is None else allowed_kinds
        self._on_step = on_step
        self._started = time.monotonic()
        self._steps: list[StepRecord] = []
        self._latest_observation = None
        self._terminal_result: SessionResult | None = None
        self._text_input_started = False
        self._executor = ActionExecutor(device)
        self._perception = PerceptionEngine()

    def __enter__(self) -> Self:
        """Return this task session; input starts immediately before its first observation."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Restore the device input method when a caller abandons or interrupts the task."""
        try:
            self.close()
        except DeviceError as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"failed to restore input method: {cleanup_error}")

    def close(self) -> None:
        """End the task-scoped input session, if it is still active."""
        if self._text_input_started:
            self._device.stop_text_input()
            self._text_input_started = False

    def step(self) -> SessionResult:
        """Observe, decide, and dispatch at most one action.

        Terminal results are cached. Calling ``step`` again after termination is
        idempotent and never observes or operates the device again.
        """
        if self._terminal_result is not None:
            return self._terminal_result
        if self._time_limit_reached():
            return self._terminate(SessionStatus.TIME_LIMIT, "time limit reached")
        try:
            if not self._text_input_started:
                self._device.start_text_input()
                self._text_input_started = True
        except DeviceError as exc:
            return self._terminate(
                SessionStatus.ERROR,
                f"failed to start text input helper: {exc}",
            )

        try:
            self._latest_observation = self._perception.observe(self._device)
            decision = self._model.decide(
                self._task,
                self._latest_observation,
                tuple(self._steps),
            )
        except (PerceptionError, PlannerError) as exc:
            return self._terminate(SessionStatus.ERROR, str(exc))

        if self._time_limit_reached():
            return self._terminate(SessionStatus.TIME_LIMIT, "time limit reached")
        if isinstance(decision, FinishDecision):
            return self._terminate(SessionStatus.FINISHED, decision.answer, decision.trace)
        if isinstance(decision, StopDecision):
            return self._terminate(SessionStatus.STOPPED, decision.reason, decision.trace)
        if not isinstance(decision, ActionDecision):
            return self._terminate(SessionStatus.ERROR, "unsupported decision")
        if len(self._steps) >= self._max_actions:
            return self._terminate(SessionStatus.ACTION_LIMIT, "action limit reached")

        action = decision.action
        kind = _kind_of(action)
        if kind not in self._allowed_kinds:
            return self._terminate(SessionStatus.STOPPED, "action kind not allowed")

        outcome = self._executor.execute(action)
        record = StepRecord(
            self._latest_observation.observation_id,
            self._latest_observation.screen.sha256,
            action,
            outcome,
            decision.trace,
            decision.usage,
        )
        self._steps.append(record)
        if self._on_step is not None:
            self._on_step(record)
        if not outcome.completed:
            return self._terminate(SessionStatus.ERROR, "device action failed")
        if self._settle_seconds:
            time.sleep(self._settle_seconds)
        return self._result(SessionStatus.RUNNING, "action completed")

    def _time_limit_reached(self) -> bool:
        return time.monotonic() - self._started >= self._max_seconds

    def _result(
        self, status: SessionStatus, message: str, terminal_trace: DecisionTrace | None = None
    ) -> SessionResult:
        return SessionResult(
            status, message, tuple(self._steps), self._latest_observation, terminal_trace
        )

    def _terminate(
        self, status: SessionStatus, message: str, terminal_trace: DecisionTrace | None = None
    ) -> SessionResult:
        try:
            self.close()
        except DeviceError as exc:
            # The device session retains its own active flag and will make one
            # final restore attempt when it closes; avoid retrying here when the
            # planner context exits immediately after this terminal result.
            self._text_input_started = False
            status = SessionStatus.ERROR
            message = f"{message}; failed to restore input method: {exc}"
        result = self._result(status, message, terminal_trace)
        self._terminal_result = result
        return result


def _kind_of(action) -> ActionKind:
    if isinstance(action, TapAction):
        return ActionKind.TAP
    if isinstance(action, LongPressAction):
        return ActionKind.LONG_PRESS
    if isinstance(action, SwipeAction):
        return ActionKind.SWIPE
    if isinstance(action, KeyAction):
        return ActionKind.KEY
    if isinstance(action, TextAction):
        return ActionKind.TEXT
    if isinstance(action, ReplaceTextAction):
        return ActionKind.REPLACE_TEXT
    if isinstance(action, WaitAction):
        return ActionKind.WAIT
    raise TypeError("action must be a supported L2 action")
