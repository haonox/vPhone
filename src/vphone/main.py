"""Command-line entry point for visual Android tasks."""

from __future__ import annotations

import argparse
import os

from dotenv import find_dotenv, load_dotenv

from vphone.device import AdbDeviceBackend
from vphone.planner import ModelConfig, OpenAICompatibleDecisionModel, PlannerSession
from vphone.planner.models import StepRecord


def _report_step(step: StepRecord) -> None:
    """Print one structured action record without logging typed text or screenshots.

    Args:
        step: The completed L2 action and its outcome.
    """
    fields = [
        ("screen_summary", step.trace.screen_summary),
        ("decision_reason", step.trace.decision_reason),
        ("action", step.describe_action(include_coordinates=True)),
        ("result", step.result_description),
        ("duration", f"{step.result.duration_seconds:.3f}s"),
    ]
    if step.usage is not None:
        usage_parts = [
            f"prompt={step.usage.prompt_tokens}",
            f"completion={step.usage.completion_tokens}",
            f"total={step.usage.total_tokens}",
        ]
        if step.usage.cache_hit_tokens is not None:
            usage_parts.append(f"cache_hit={step.usage.cache_hit_tokens}")
        if step.usage.cache_miss_tokens is not None:
            usage_parts.append(f"cache_miss={step.usage.cache_miss_tokens}")
        fields.append(("usage", " ".join(usage_parts)))

    lines = ["Step completed"]
    for index, (label, value) in enumerate(fields):
        is_last = index == len(fields) - 1
        lines.append(f"{'└──' if is_last else '├──'} {label}: {value}")

    print("\n".join(lines), flush=True)


def main(argv: list[str] | None = None) -> None:
    """Run a user-supplied visual task on one authorized phone.

    Args:
        argv: Optional arguments for tests; defaults to process arguments.
    """
    parser = argparse.ArgumentParser(description="Run one visual task on an Android phone.")
    parser.add_argument("task", help="Natural-language task to perform on the phone")
    args = parser.parse_args(argv)
    task = args.task.strip()
    if not task:
        parser.error("task must not be empty")

    env_path = find_dotenv(usecwd=True)
    load_dotenv(env_path)
    try:
        config = ModelConfig.from_env()
        max_actions = int(os.getenv("VPHONE_MAX_ACTIONS", "20"))
        max_seconds = float(os.getenv("VPHONE_MAX_SECONDS", "600"))
        settle_seconds = float(os.getenv("VPHONE_SETTLE_SECONDS", "0.5"))
    except ValueError as exc:
        raise SystemExit(f"Invalid model or runtime configuration: {exc}") from exc

    backend = AdbDeviceBackend()
    device_id = os.getenv("VPHONE_DEVICE_ID")
    if not device_id:
        ready = [item for item in backend.list_devices() if item.state.value == "device"]
        if len(ready) != 1:
            raise SystemExit("Set VPHONE_DEVICE_ID when there is not exactly one ready device")
        device_id = ready[0].device_id

    model = OpenAICompatibleDecisionModel(config)
    with backend.open(device_id) as device:
        try:
            session = PlannerSession(
                model,
                task,
                device,
                max_actions=max_actions,
                max_seconds=max_seconds,
                settle_seconds=settle_seconds,
                on_step=_report_step,
            )
        except ValueError as exc:
            raise SystemExit(f"Invalid run limits: {exc}") from exc
        with session:
            while True:
                result = session.step()
                if result.terminal:
                    break
    if result.terminal_trace is not None:
        print(f"screen_summary={result.terminal_trace.screen_summary}")
        print(f"decision_reason={result.terminal_trace.decision_reason}")
    if result.terminal_action is not None:
        print(f"action={result.terminal_action}")
    print(result.message)
    if not result.completed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
