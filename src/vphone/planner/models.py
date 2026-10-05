"""Backend-neutral planner decisions and run records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from vphone.action.models import (
    Action,
    ActionResult,
    KeyAction,
    LongPressAction,
    ReplaceTextAction,
    SwipeAction,
    TapAction,
    TextAction,
    WaitAction,
)
from vphone.perception.models import PageObservation


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported token counts associated with one model decision."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        """Require every available token count to be a non-negative integer."""
        for field_name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer")

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Aggregate multiple provider requests made for one planner decision."""

        def add_optional(left: int | None, right: int | None) -> int | None:
            if left is None and right is None:
                return None
            return (left or 0) + (right or 0)

        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
            add_optional(self.cache_hit_tokens, other.cache_hit_tokens),
            add_optional(self.cache_miss_tokens, other.cache_miss_tokens),
        )


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    """Task-relevant interpretation behind one model decision."""

    screen_summary: str = field(repr=False)
    decision_reason: str = field(repr=False)

    def __post_init__(self) -> None:
        """Keep trajectory text concise, printable, and safe for one-line prompts."""
        for field_name in ("screen_summary", "decision_reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be text")
            normalized = value.strip()
            if not normalized or len(value) > 1000 or not normalized.isprintable():
                raise ValueError(
                    f"{field_name} must be non-empty printable text up to 1000 characters"
                )
            object.__setattr__(self, field_name, normalized)


@dataclass(frozen=True, slots=True)
class ActionDecision:
    action: Action
    trace: DecisionTrace
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class FinishDecision:
    answer: str
    trace: DecisionTrace
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class StopDecision:
    reason: str
    trace: DecisionTrace
    usage: TokenUsage | None = None


Decision = ActionDecision | FinishDecision | StopDecision


@dataclass(frozen=True, slots=True)
class StepRecord:
    observation_id: str
    screen_sha256: str
    action: Action
    result: ActionResult
    trace: DecisionTrace
    usage: TokenUsage | None = None

    def describe_action(self, *, include_coordinates: bool = False) -> str:
        """Return a printable action description without exposing typed text.

        Args:
            include_coordinates: Include current-step tap and swipe pixels for
                local diagnostics. Model history keeps this disabled so stale
                coordinates never enter a later prompt.
        """
        action = self.action
        if isinstance(action, TapAction):
            if include_coordinates:
                return f"tap(x={action.point.x}, y={action.point.y})"
            return "tap"
        if isinstance(action, LongPressAction):
            if include_coordinates:
                return f"long_press(x={action.point.x}, y={action.point.y})"
            return "long_press"
        if isinstance(action, SwipeAction):
            if include_coordinates:
                return (
                    f"swipe(start=({action.start.x}, {action.start.y}), "
                    f"end=({action.end.x}, {action.end.y}), "
                    f"duration_ms={action.duration_ms})"
                )
            return "swipe"
        if isinstance(action, KeyAction):
            key = action.key.name if hasattr(action.key, "name") else action.key
            return f"press_key({key})"
        if isinstance(action, TextAction):
            return f"input_text({len(action.text)} characters)"
        if isinstance(action, ReplaceTextAction):
            return f"replace_text({len(action.text)} characters)"
        if isinstance(action, WaitAction):
            return f"wait({action.seconds:g} seconds)"
        return "unknown_action"

    @property
    def result_description(self) -> str:
        """Describe command completion without claiming the UI effect succeeded."""
        if isinstance(self.action, WaitAction):
            return "wait completed" if self.result.completed else "wait failed"
        return "device command completed" if self.result.completed else "device command failed"


class SessionStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    STOPPED = "stopped"
    ACTION_LIMIT = "action_limit"
    TIME_LIMIT = "time_limit"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SessionResult:
    status: SessionStatus
    message: str
    steps: tuple[StepRecord, ...]
    latest_observation: PageObservation | None
    terminal_trace: DecisionTrace | None = None

    @property
    def terminal(self) -> bool:
        """Whether the session must not dispatch any more device actions."""
        return self.status is not SessionStatus.RUNNING

    @property
    def completed(self) -> bool:
        """Model reported task completion; not an independent UI proof."""
        return self.status is SessionStatus.FINISHED

    @property
    def terminal_action(self) -> str | None:
        """Return the model terminal tool name when one produced this result."""
        if self.terminal_trace is None:
            return None
        if self.status is SessionStatus.FINISHED:
            return "finish"
        if self.status is SessionStatus.STOPPED:
            return "stop"
        return None
