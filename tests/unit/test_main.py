"""Tests for the task-based package and console entry points."""

from __future__ import annotations

import tomllib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import vphone.main as cli
from vphone.action import (
    ActionKind,
    ActionResult,
    LongPressAction,
    ReplaceTextAction,
    TapAction,
    TextAction,
)
from vphone.device import Point
from vphone.planner.models import (
    DecisionTrace,
    SessionResult,
    SessionStatus,
    StepRecord,
    TokenUsage,
)


def test_console_entry_point_targets_main() -> None:
    """Keep the project command mapped to the package-level main function."""
    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["vphone"] == "vphone.main:main"


def test_main_wires_task_config_device_and_planner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Exercise the complete entry-point wiring without a phone or network."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "find_dotenv", lambda **kwargs: "")
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setenv("VPHONE_API_KEY", "test-key")
    monkeypatch.setenv("VPHONE_MODEL_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("VPHONE_MODEL_ID", "vision-test")
    monkeypatch.setenv("VPHONE_MAX_ACTIONS", "3")
    monkeypatch.setenv("VPHONE_MAX_SECONDS", "30")
    monkeypatch.setenv("VPHONE_SETTLE_SECONDS", "0")
    monkeypatch.delenv("VPHONE_DEVICE_ID", raising=False)
    observed: dict = {}
    phone = object()

    class FakeBackend:
        """Return one authorized synthetic device."""

        def list_devices(self):
            """Expose one ready device for automatic selection."""
            return [SimpleNamespace(device_id="test-device", state=SimpleNamespace(value="device"))]

        def open(self, device_id):
            """Open the selected synthetic device as a context manager."""
            observed["device_id"] = device_id
            return nullcontext(phone)

    def fake_model(config):
        """Capture the configured model without creating an SDK client."""
        observed["config"] = config
        return object()

    def fake_session(model, task, device, **options):
        """Capture limits and return a successful single-step session."""
        observed["session_model"] = model
        observed["task"] = task
        observed["device"] = device
        observed["options"] = options

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return None

            def step(self):
                """Finish without touching the synthetic device."""
                return SessionResult(
                    SessionStatus.FINISHED,
                    "Battery 80%",
                    (),
                    None,
                    DecisionTrace(
                        "Battery page shows 80%",
                        "The requested battery value is visible",
                    ),
                )

        return FakeSession()

    monkeypatch.setattr(cli, "AdbDeviceBackend", FakeBackend)
    monkeypatch.setattr(cli, "OpenAICompatibleDecisionModel", fake_model)
    monkeypatch.setattr(cli, "PlannerSession", fake_session)

    cli.main(["查看当前电量"])

    assert observed["config"].model_id == "vision-test"
    assert observed["device_id"] == "test-device"
    assert observed["device"] is phone
    assert observed["task"] == "查看当前电量"
    assert observed["options"]["max_actions"] == 3
    assert observed["options"]["max_seconds"] == 30
    assert observed["options"]["settle_seconds"] == 0
    assert "allowed_kinds" not in observed["options"]
    output = capsys.readouterr().out
    assert "status=" not in output
    assert "screen_summary=Battery page shows 80%" in output
    assert "decision_reason=The requested battery value is visible" in output
    assert "action=finish" in output
    assert "trajectory=" not in output
    assert not (tmp_path / "traces").exists()
    assert "on_observation" not in observed["options"]
    assert "on_decision" not in observed["options"]
    assert observed["options"]["on_step"] is cli._report_step


def test_main_requires_task(capsys: pytest.CaptureFixture[str]) -> None:
    """Reject a missing task before loading credentials or opening a device."""
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "task" in capsys.readouterr().err


def test_configuration_failure_does_not_create_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configuration error exits without creating task artifacts."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "find_dotenv", lambda **kwargs: "")
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setenv("VPHONE_API_KEY", "")
    monkeypatch.setenv("VPHONE_MODEL_ID", "vision-test")

    with pytest.raises(SystemExit, match="Invalid model or runtime configuration"):
        cli.main(["查看电量"])

    assert not (tmp_path / "traces").exists()


def test_progress_output_hides_text_input(capsys: pytest.CaptureFixture[str]) -> None:
    """Print structured step context without exposing typed text."""
    step = StepRecord(
        "obs",
        "a" * 64,
        TextAction("private value"),
        ActionResult(ActionKind.TEXT, 0.1),
        DecisionTrace("A text field is focused", "Enter the requested value"),
    )
    cli._report_step(step)
    output = capsys.readouterr().out
    assert output.startswith("Step completed\n")
    assert "├── screen_summary: A text field is focused" in output
    assert "├── decision_reason: Enter the requested value" in output
    assert "├── action: input_text(13 characters)" in output
    assert "├── result: device command completed" in output
    assert output.endswith("└── duration: 0.100s\n")
    assert "private value" not in output


def test_progress_output_hides_replacement_text(capsys: pytest.CaptureFixture[str]) -> None:
    """Describe replacement length without exposing replacement text."""
    step = StepRecord(
        "obs",
        "a" * 64,
        ReplaceTextAction("replacement value"),
        ActionResult(ActionKind.REPLACE_TEXT, 0.1),
        DecisionTrace("A populated field is focused", "Replace its existing value"),
    )

    cli._report_step(step)

    output = capsys.readouterr().out
    assert "replace_text(17 characters)" in output
    assert "replacement value" not in output


def test_progress_output_includes_current_action_coordinates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose current-step coordinates locally without adding them to model history."""
    step = StepRecord(
        "obs",
        "a" * 64,
        TapAction(Point(12, 34)),
        ActionResult(ActionKind.TAP, 0.25),
        DecisionTrace("A settings page", "Open the visible battery row"),
        TokenUsage(3593, 129, 3722, 2304, 1289),
    )

    cli._report_step(step)

    output = capsys.readouterr().out
    assert "├── action: tap(x=12, y=34)" in output
    assert "├── duration: 0.250s" in output
    assert output.endswith(
        "└── usage: prompt=3593 completion=129 total=3722 "
        "cache_hit=2304 cache_miss=1289\n"
    )


def test_progress_output_includes_long_press_coordinates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose the current long-press point in local progress output."""
    step = StepRecord(
        "obs",
        "a" * 64,
        LongPressAction(Point(12, 34)),
        ActionResult(ActionKind.LONG_PRESS, 1.0),
        DecisionTrace("A context target is visible", "Open its context menu"),
    )

    cli._report_step(step)

    assert "├── action: long_press(x=12, y=34)" in capsys.readouterr().out
