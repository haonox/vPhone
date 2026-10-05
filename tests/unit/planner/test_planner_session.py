"""Tests for the stateful L4 planning session."""

from __future__ import annotations

from vphone.action import (
    ActionKind,
    ActionResult,
    LongPressAction,
    ReplaceTextAction,
    SwipeAction,
    TapAction,
    TextAction,
    WaitAction,
)
from vphone.device import InputError, Point, PrimitiveResult, ScreenFrame
from vphone.planner.errors import InvalidDecisionError
from vphone.planner.models import (
    ActionDecision,
    DecisionTrace,
    FinishDecision,
    SessionStatus,
    StepRecord,
    StopDecision,
    TokenUsage,
)
from vphone.planner.session import PlannerSession

TRACE = DecisionTrace("A task-relevant screen", "This decision advances the task")


class FakeDevice:
    def __init__(self) -> None:
        """Initialize screenshot and tap counters for planner tests."""
        self.screen_calls = 0
        self.taps: list[Point] = []
        self.long_presses: list[Point] = []
        self.text_input_starts = 0
        self.text_input_stops = 0

    def start_text_input(self, *, timeout: float = 10.0) -> None:
        """Record selection of the task-scoped headless input method."""
        self.text_input_starts += 1

    def stop_text_input(self, *, timeout: float = 10.0) -> None:
        """Record restoration of the input method used before the task."""
        self.text_input_stops += 1

    def capture_screen(self, *, timeout: float = 10.0) -> ScreenFrame:
        """Return a distinct synthetic frame for each observation."""
        self.screen_calls += 1
        return ScreenFrame(
            b"png" + bytes([self.screen_calls]),
            100,
            200,
            "image/png",
            str(self.screen_calls) * 64,
            1.0,
            0.0,
        )

    def tap(self, point: Point, *, timeout: float = 5.0) -> PrimitiveResult:
        """Record a tap without touching a real device."""
        self.taps.append(point)
        return PrimitiveResult("tap", 0.0)

    def long_press(self, point: Point, *, timeout: float = 5.0) -> PrimitiveResult:
        """Record a long press without touching a real device."""
        self.long_presses.append(point)
        return PrimitiveResult("long_press", 0.0)


class FakeModel:
    def __init__(self, decisions: list) -> None:
        """Queue decisions to return in successive planner turns."""
        self.decisions = decisions
        self.seen: list[tuple[str, int]] = []

    def decide(self, task, observation, history):
        """Record the observation and return the next queued decision."""
        self.seen.append((observation.observation_id, len(history)))
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_each_call_executes_at_most_one_action_then_observes_again() -> None:
    """Keep history across calls while executing no more than one action per call."""
    device = FakeDevice()
    model = FakeModel(
        [ActionDecision(TapAction(Point(30, 40)), TRACE), FinishDecision("Battery 80%", TRACE)]
    )
    session = PlannerSession(model, "Find battery", device, settle_seconds=0)

    first = session.step()

    assert first.status is SessionStatus.RUNNING
    assert first.terminal is False
    assert device.taps == [Point(30, 40)]
    assert device.screen_calls == 1
    assert first.steps[0].screen_sha256 == "1" * 64
    assert first.steps[0].trace == TRACE

    second = session.step()

    assert second.status is SessionStatus.FINISHED
    assert second.completed is True
    assert second.message == "Battery 80%"
    assert second.terminal_trace == TRACE
    assert second.terminal_action == "finish"
    assert device.taps == [Point(30, 40)]
    assert device.screen_calls == 2
    assert [item[1] for item in model.seen] == [0, 1]
    assert model.seen[0][0] != model.seen[1][0]
    assert second.latest_observation.screen.sha256 == "2" * 64
    assert device.text_input_starts == 1
    assert device.text_input_stops == 1


def test_step_callback_reports_executed_action() -> None:
    """Report an action once while keeping it in the session history."""
    reported = []
    usage = TokenUsage(3593, 129, 3722, 2304, 1289)
    model = FakeModel([ActionDecision(TapAction(Point(30, 40)), TRACE, usage)])
    session = PlannerSession(
        model,
        "Find page",
        FakeDevice(),
        settle_seconds=0,
        on_step=reported.append,
    )

    result = session.step()

    assert result.status is SessionStatus.RUNNING
    assert reported == list(result.steps)
    assert result.steps[0].usage == usage


def test_context_exit_restores_input_for_an_unfinished_task() -> None:
    """Restore the original IME when a caller stops between planner steps."""
    device = FakeDevice()
    session = PlannerSession(
        FakeModel([ActionDecision(TapAction(Point(30, 40)), TRACE)]),
        "Start but do not finish",
        device,
        settle_seconds=0,
    )

    with session:
        result = session.step()
        assert result.status is SessionStatus.RUNNING
        assert device.text_input_stops == 0

    assert device.text_input_starts == 1
    assert device.text_input_stops == 1


def test_input_start_failure_stops_before_observation() -> None:
    """Report helper-selection failure without touching the task UI."""

    class FailingStartDevice(FakeDevice):
        def start_text_input(self, *, timeout: float = 10.0) -> None:
            raise InputError("cannot select helper")

    device = FailingStartDevice()
    session = PlannerSession(FakeModel([]), "Cannot start", device, settle_seconds=0)

    result = session.step()

    assert result.status is SessionStatus.ERROR
    assert "failed to start headless text input" in result.message
    assert device.screen_calls == 0
    assert device.text_input_stops == 0


def test_input_restore_failure_turns_terminal_result_into_error() -> None:
    """Do not report task success when the user's previous IME was not restored."""

    class FailingStopDevice(FakeDevice):
        def stop_text_input(self, *, timeout: float = 10.0) -> None:
            raise InputError("cannot restore keyboard")

    device = FailingStopDevice()
    session = PlannerSession(
        FakeModel([FinishDecision("done", TRACE)]),
        "Finish with cleanup failure",
        device,
        settle_seconds=0,
    )

    result = session.step()

    assert result.status is SessionStatus.ERROR
    assert "failed to restore input method" in result.message


def test_session_dispatches_long_press_as_one_action() -> None:
    """Route a validated long press through the session and record it once."""
    device = FakeDevice()
    session = PlannerSession(
        FakeModel([ActionDecision(LongPressAction(Point(30, 40)), TRACE)]),
        "Open the context menu",
        device,
        settle_seconds=0,
    )

    result = session.step()

    assert result.status is SessionStatus.RUNNING
    assert device.long_presses == [Point(30, 40)]
    assert result.steps[0].result.kind is ActionKind.LONG_PRESS


def test_action_limit_stops_before_extra_execution() -> None:
    """Allow a final observation and decision without dispatching an extra action."""
    device = FakeDevice()
    proposal = ActionDecision(TapAction(Point(30, 40)), TRACE)
    session = PlannerSession(
        FakeModel([proposal, proposal]),
        "Find battery",
        device,
        max_actions=1,
        settle_seconds=0,
    )

    first = session.step()
    second = session.step()

    assert first.status is SessionStatus.RUNNING
    assert second.status is SessionStatus.ACTION_LIMIT
    assert device.taps == [Point(30, 40)]
    assert device.screen_calls == 2


def test_disallowed_action_does_not_reach_device() -> None:
    """Verify disallowed action does not reach device."""
    device = FakeDevice()
    model = FakeModel([ActionDecision(TapAction(Point(30, 40)), TRACE)])
    session = PlannerSession(
        model,
        "Find battery",
        device,
        allowed_kinds=frozenset({ActionKind.KEY}),
        settle_seconds=0,
    )

    result = session.step()

    assert result.status is SessionStatus.STOPPED
    assert device.taps == []


def test_wait_action_is_recorded_before_next_observation(monkeypatch) -> None:
    """Treat waiting as one complete single-step action."""
    waits = []
    monkeypatch.setattr("vphone.action.executor.time.sleep", waits.append)
    device = FakeDevice()
    model = FakeModel([ActionDecision(WaitAction(2), TRACE), FinishDecision("loaded", TRACE)])
    session = PlannerSession(model, "Wait for loading", device, settle_seconds=0)

    first = session.step()

    assert first.status is SessionStatus.RUNNING
    assert waits == [2.0]
    assert first.steps[0].result.kind is ActionKind.WAIT
    assert device.screen_calls == 1
    assert device.taps == []

    second = session.step()
    assert second.status is SessionStatus.FINISHED
    assert device.screen_calls == 2


def test_invalid_model_decision_stops_without_action() -> None:
    """Verify invalid model decision stops without action."""
    device = FakeDevice()
    model = FakeModel([InvalidDecisionError("bad coordinates")])
    session = PlannerSession(model, "Find battery", device, settle_seconds=0)

    result = session.step()

    assert result.status is SessionStatus.ERROR
    assert result.terminal_trace is None
    assert "bad coordinates" in result.message
    assert device.taps == []


def test_terminal_step_is_idempotent() -> None:
    """Never observe or act again after the session reaches a terminal state."""
    device = FakeDevice()
    session = PlannerSession(
        FakeModel([FinishDecision("done", TRACE)]),
        "Finish now",
        device,
        settle_seconds=0,
    )

    first = session.step()
    second = session.step()

    assert second is first
    assert device.screen_calls == 1


def test_stop_preserves_terminal_trace_without_adding_an_action() -> None:
    """Keep the model's terminal explanation separate from executed steps."""
    session = PlannerSession(
        FakeModel([StopDecision("target not visible", TRACE)]),
        "Find target",
        FakeDevice(),
        settle_seconds=0,
    )

    result = session.step()

    assert result.status is SessionStatus.STOPPED
    assert result.terminal_trace == TRACE
    assert result.terminal_action == "stop"
    assert result.steps == ()


def test_time_limit_stops_before_observing(monkeypatch) -> None:
    """Do not capture or decide after the session budget has expired."""
    timestamps = iter([10.0, 11.1])
    monkeypatch.setattr("vphone.planner.session.time.monotonic", lambda: next(timestamps))
    device = FakeDevice()
    session = PlannerSession(
        FakeModel([]),
        "Timed task",
        device,
        max_seconds=1,
        settle_seconds=0,
    )

    result = session.step()

    assert result.status is SessionStatus.TIME_LIMIT
    assert result.latest_observation is None
    assert device.screen_calls == 0


def test_step_record_describes_actions_without_exposing_text() -> None:
    """Keep display formatting centralized and protect text input values."""
    text = StepRecord(
        "obs-text",
        "a" * 64,
        TextAction("private value"),
        ActionResult(ActionKind.TEXT, 0.1),
        TRACE,
    )
    replacement = StepRecord(
        "obs-replace",
        "c" * 64,
        ReplaceTextAction("new private value"),
        ActionResult(ActionKind.REPLACE_TEXT, 0.1),
        TRACE,
    )
    swipe = StepRecord(
        "obs-swipe",
        "b" * 64,
        SwipeAction(Point(10, 20), Point(30, 40), 350),
        ActionResult(ActionKind.SWIPE, 0.2),
        TRACE,
    )
    long_press = StepRecord(
        "obs-long-press",
        "d" * 64,
        LongPressAction(Point(15, 25)),
        ActionResult(ActionKind.LONG_PRESS, 1.0),
        TRACE,
    )

    assert text.describe_action() == "input_text(13 characters)"
    assert "private value" not in text.describe_action(include_coordinates=True)
    assert replacement.describe_action() == "replace_text(17 characters)"
    assert "new private value" not in replacement.describe_action()
    assert swipe.describe_action() == "swipe"
    assert swipe.describe_action(include_coordinates=True) == (
        "swipe(start=(10, 20), end=(30, 40), duration_ms=350)"
    )
    assert swipe.result_description == "device command completed"
    assert long_press.describe_action() == "long_press"
    assert long_press.describe_action(include_coordinates=True) == "long_press(x=15, y=25)"
