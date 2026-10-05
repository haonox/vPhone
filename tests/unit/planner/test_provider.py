"""Unit tests for model request construction and decision parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vphone.action import ActionKind, ActionResult, TapAction
from vphone.device import Point, ScreenFrame
from vphone.perception import PageObservation
from vphone.planner.config import ModelConfig
from vphone.planner.errors import InvalidDecisionError
from vphone.planner.models import ActionDecision, DecisionTrace, StepRecord, TokenUsage
from vphone.planner.provider import OpenAICompatibleDecisionModel


def _args(**values: object) -> str:
    """Build a valid task-aware tool call for provider tests."""
    return json.dumps(
        {
            "screen_summary": "Settings screen with a Battery entry",
            "decision_reason": "Open Battery to answer the task",
            **values,
        }
    )


class FakeCompletions:
    def __init__(self, calls: list[tuple[str, str]], usage: object | None = None) -> None:
        """Store tool calls for a fixed synthetic model response."""
        self.calls = calls
        self.kwargs = None
        self.usage = usage

    def create(self, **kwargs):
        """Capture request arguments and return the configured tool calls."""
        self.kwargs = kwargs
        tool_calls = [
            SimpleNamespace(type="function", function=SimpleNamespace(name=name, arguments=args))
            for name, args in self.calls
        ]
        return SimpleNamespace(
            usage=self.usage,
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(tool_calls=tool_calls),
                )
            ],
        )


class SequenceCompletions:
    def __init__(
        self,
        responses: list[list[tuple[str, str]]],
        usages: list[object | None] | None = None,
    ) -> None:
        """Queue different tool-call sets for retry-path testing."""
        self.responses = responses
        self.usages = usages or [None] * len(responses)
        self.prompts: list[str] = []

    def create(self, **kwargs):
        """Record the prompt and return the next queued response."""
        self.prompts.append(kwargs["messages"][1]["content"][0]["text"])
        calls = self.responses.pop(0)
        tool_calls = [
            SimpleNamespace(type="function", function=SimpleNamespace(name=name, arguments=args))
            for name, args in calls
        ]
        return SimpleNamespace(
            usage=self.usages.pop(0),
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(tool_calls=tool_calls),
                )
            ],
        )


def _model(calls):
    """Build a provider with an injected fixed-response client."""
    completions = FakeCompletions(calls)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    config = ModelConfig("test-key", "https://example.test", "vision-test", image_detail="original")
    return OpenAICompatibleDecisionModel(config, client=client), completions


def _observation():
    """Build a synthetic PNG observation for provider tests."""
    screen = ScreenFrame(b"png", 100, 200, "image/png", "a" * 64, 1.0, 0.0)
    return PageObservation("obs", screen)


def test_provider_sends_png_as_original_and_returns_validated_action(capsys) -> None:
    """Verify provider sends png as original and returns validated action."""
    model, completions = _model([("tap", _args(x=99, y=199))])

    decision = model.decide("Tap icon", _observation(), ())

    assert isinstance(decision, ActionDecision)
    assert decision.action.point == Point(99, 199)
    assert completions.kwargs["model"] == "vision-test"
    assert completions.kwargs["max_tokens"] == 4096
    assert completions.kwargs["tool_choice"] == "required"
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in completions.kwargs
    tap_tool = next(
        item for item in completions.kwargs["tools"] if item["function"]["name"] == "tap"
    )
    assert tap_tool["function"]["parameters"]["properties"]["y"]["maximum"] == 199
    assert tap_tool["function"]["parameters"]["required"] == [
        "screen_summary",
        "decision_reason",
        "x",
        "y",
    ]
    image = completions.kwargs["messages"][1]["content"][1]["image_url"]
    assert image["detail"] == "original"
    assert image["url"].startswith("data:image/png;base64,")
    instructions = completions.kwargs["messages"][0]["content"]
    assert "do not repeat the same action" in instructions
    assert "look for task-relevant shortcuts" in instructions
    assert "search, filters, tabs" in instructions
    assert "prefer them over repeated scrolling or manual browsing" in instructions
    assert "Before calling finish:" in instructions
    assert "A successful device command" in instructions
    assert "In decision_reason for finish, briefly name the evidence" in instructions
    assert "This device uses Google Pixel Launcher" in instructions
    assert "swipe up to open the app drawer" in instructions
    assert "between home pages to search for apps" in instructions
    assert "use replace_text" in instructions
    assert any(item["function"]["name"] == "replace_text" for item in completions.kwargs["tools"])
    long_press_tool = next(
        item for item in completions.kwargs["tools"] if item["function"]["name"] == "long_press"
    )
    assert long_press_tool["function"]["parameters"]["properties"]["x"]["maximum"] == 99
    assert "visible target requires a hold gesture" in instructions
    assert "Do not pause for user confirmation" in instructions
    assert "sending or deleting when requested" in instructions
    assert all(
        item["function"]["name"] != "request_confirmation" for item in completions.kwargs["tools"]
    )
    assert capsys.readouterr().out == ""


def test_provider_attaches_usage_to_decision() -> None:
    """Normalize provider token counts for the session step record."""
    usage = SimpleNamespace(
        prompt_tokens=3593,
        completion_tokens=129,
        total_tokens=3722,
        prompt_cache_hit_tokens=2304,
        prompt_cache_miss_tokens=1289,
    )
    completions = FakeCompletions([("tap", _args(x=1, y=2))], usage)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleDecisionModel(
        ModelConfig("test-key", "https://example.test", "vision-test"),
        client=client,
    )

    decision = model.decide("Tap icon", _observation(), ())

    assert decision.usage == TokenUsage(3593, 129, 3722, 2304, 1289)


def test_provider_rejects_multiple_tool_calls() -> None:
    """Verify provider rejects multiple tool calls."""
    model, _ = _model([("tap", _args(x=1, y=2)), ("tap", _args(x=3, y=4))])

    with pytest.raises(InvalidDecisionError, match="exactly one"):
        model.decide("Tap icon", _observation(), ())


def test_provider_sends_configured_optional_reasoning_effort() -> None:
    """Include the optional reasoning parameter only when configured."""
    completions = FakeCompletions([("tap", _args(x=1, y=2))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    config = ModelConfig("test-key", "https://example.test", "another-model", 20, 512, "medium")
    model = OpenAICompatibleDecisionModel(config, client=client)

    model.decide("Tap icon", _observation(), ())

    assert completions.kwargs["model"] == "another-model"
    assert completions.kwargs["max_tokens"] == 512
    assert completions.kwargs["reasoning_effort"] == "medium"
    assert completions.kwargs["tool_choice"] == "required"
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "detail" not in completions.kwargs["messages"][1]["content"][1]["image_url"]


def test_provider_constructs_client_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the configured endpoint and timeout to the SDK client."""
    captured = {}

    def fake_client(**kwargs):
        """Capture SDK constructor options without contacting a service."""
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("vphone.planner.provider.OpenAI", fake_client)
    config = ModelConfig("test-key", "https://example.test/v1", "vision-test", 25)

    OpenAICompatibleDecisionModel(config)

    assert captured == {
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "timeout": 25,
        "max_retries": 0,
    }


def test_provider_repairs_missing_field_once_without_action() -> None:
    """Verify provider repairs missing field once without action."""
    completions = SequenceCompletions(
        [
            [("tap", '{"x":50}')],
            [("tap", _args(x=50, y=70))],
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    config = ModelConfig("test-key", "https://example.test", "vision-test")
    model = OpenAICompatibleDecisionModel(config, client=client)

    decision = model.decide("Tap icon", _observation(), ())

    assert isinstance(decision, ActionDecision)
    assert decision.action.point == Point(50, 70)
    assert len(completions.prompts) == 2
    assert "previous proposal was rejected" in completions.prompts[1]


def test_provider_aggregates_usage_across_format_retry() -> None:
    """Charge both model requests to a step when formatting requires a retry."""
    usages = [
        SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            prompt_cache_hit_tokens=20,
            prompt_cache_miss_tokens=80,
        ),
        SimpleNamespace(
            prompt_tokens=200,
            completion_tokens=20,
            total_tokens=220,
            prompt_cache_hit_tokens=50,
            prompt_cache_miss_tokens=150,
        ),
    ]
    completions = SequenceCompletions(
        [[("tap", '{"x":50}')], [("tap", _args(x=50, y=70))]],
        usages,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleDecisionModel(
        ModelConfig("test-key", "https://example.test", "vision-test"),
        client=client,
    )

    decision = model.decide("Tap icon", _observation(), ())

    assert decision.usage == TokenUsage(300, 30, 330, 70, 230)


def test_provider_sends_task_aware_trajectory_without_old_coordinates() -> None:
    """Use prior screen meaning and decision intent instead of raw tap coordinates."""
    model, completions = _model([("finish", _args(answer="Battery 80%"))])
    trace = DecisionTrace(
        "The Android home screen is visible",
        "Open Settings as the first step toward checking the battery",
    )
    step = StepRecord(
        "old-observation",
        "b" * 64,
        TapAction(Point(987, 654)),
        ActionResult(ActionKind.TAP, 0.1),
        trace,
    )

    model.decide("Check battery", _observation(), (step,))

    prompt = completions.kwargs["messages"][1]["content"][0]["text"]
    assert "Task-aware trajectory" in prompt
    assert trace.screen_summary in prompt
    assert trace.decision_reason in prompt
    assert "Action: tap" in prompt
    assert "987" not in prompt
    assert "654" not in prompt
