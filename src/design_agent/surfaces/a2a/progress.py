"""Safe tool lifecycle updates for the active A2A task on a runtime thread."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from design_agent.runtime.profiles import thread_id_from_config

PROGRESS_KEY = "urn:design-agent:a2a:progress:v1"
ProgressSink = Callable[[dict[str, Any]], Awaitable[None]]
_LOGGER = logging.getLogger(__name__)


class A2AProgressMiddleware(AgentMiddleware):
    def __init__(self, sinks: dict[str, ProgressSink]) -> None:
        super().__init__()
        self.sinks = sinks

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        sink = self.sinks.get(thread_id_from_config(request.runtime))
        if sink is None:
            return await handler(request)
        step = uuid4().hex
        name = str(request.tool_call.get("name") or "tool")[:128]
        started = time.monotonic()

        async def publish(state: str) -> None:
            # Progress transport must not prevent tool execution or expose args/results/errors.
            try:
                async with asyncio.timeout(1):
                    await sink(
                        {
                            "stepId": step,
                            "tool": name,
                            "state": state,
                            "elapsedSeconds": round(time.monotonic() - started, 3),
                        }
                    )
            except Exception:  # noqa: BLE001 -- presentation is independent of execution
                _LOGGER.warning("A2A progress delivery failed")

        await publish("running")
        try:
            result = await handler(request)
        except BaseException:
            await publish("failed")
            raise
        await publish(
            "failed"
            if isinstance(result, ToolMessage) and result.status == "error"
            else "completed"
        )
        return result
