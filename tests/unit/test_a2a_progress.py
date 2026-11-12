"""Tool progress is scoped, contains no args/results, and cannot break a tool."""

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from design_agent.surfaces.a2a.progress import A2AProgressMiddleware


@pytest.mark.parametrize("outcome", ["success", "error_result", "exception"])
async def test_progress_lifecycle_and_sensitive_output(outcome: str) -> None:
    events: list[dict[str, Any]] = []

    async def sink(event: dict[str, Any]) -> None:
        events.append(event)

    request = SimpleNamespace(
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "a"}}),
        tool_call={"name": "render", "args": {"password": "private-input"}},
    )
    middleware = A2AProgressMiddleware({"a": sink})

    async def handler(_: Any) -> ToolMessage:
        if outcome == "exception":
            raise RuntimeError("private-error")
        return ToolMessage(
            content="private-output",
            tool_call_id="1",
            status="error" if outcome == "error_result" else "success",
        )

    if outcome == "exception":
        with pytest.raises(RuntimeError, match="private-error"):
            await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
    else:
        await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
    assert [e["state"] for e in events] == [
        "running",
        "completed" if outcome == "success" else "failed",
    ]
    assert events[0]["stepId"] == events[1]["stepId"]
    assert "private-" not in str(events)

    events.clear()
    request.runtime.config["configurable"]["thread_id"] = "other"
    if outcome == "success":
        await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]
        assert events == []


async def test_progress_failure_does_not_prevent_tool_result() -> None:
    async def broken(_: dict[str, Any]) -> None:
        raise RuntimeError("private-sink-error")

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id="1")

    request = SimpleNamespace(
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "a"}}),
        tool_call={"name": "render"},
    )
    result = await A2AProgressMiddleware({"a": broken}).awrap_tool_call(request, handler)  # type: ignore[arg-type]
    assert result.content == "ok"
