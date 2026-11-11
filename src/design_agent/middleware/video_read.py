"""A bounded, whole-film overview for read_file(video) without a requested window."""

from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import av
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.messages.content import ContentBlock
from langchain_core.tools import BaseTool
from langgraph.types import Command

from design_agent.tools.files import BackendFor, WorkspaceError, require_backend, workspace_path

VIDEO_SUFFIXES = {".mp4", ".mov", ".mpeg", ".mpg", ".avi", ".flv", ".webm", ".wmv", ".3gpp", ".mkv"}
MAX_BYTES = 100 * 1024**2
MAX_OUTPUT_BYTES = 4 * 1024**2


def overview_times(duration: float, frame_interval: float) -> list[float]:
    """Two frames one second apart per quarter, shifting the final pair before EOF."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration is unavailable; request an offset/limit window instead.")
    end = max(0.0, duration - frame_interval)
    times: set[float] = set()
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        first = min(duration * fraction, max(0.0, end - 1))
        times.update((round(first, 6), round(min(first + 1, end), 6)))
    return sorted(times)


def overview_blocks(raw: bytes, path: str) -> list[dict[str, Any]]:
    started = time.monotonic()
    blocks: list[dict[str, Any]] = []
    encoded_bytes = 0
    seen = set()
    # Uploaded media must not make the host follow external file or network references.
    with av.open(io.BytesIO(raw), mode="r", options={"protocol_whitelist": "pipe"}) as container:
        if not container.streams.video:
            raise ValueError("No video stream was found.")
        stream = container.streams.video[0]
        if stream.time_base is None:
            raise ValueError("Video timing is unavailable.")
        start = float((stream.start_time or 0) * stream.time_base)
        duration = (
            float(stream.duration * stream.time_base)
            if stream.duration is not None
            else float(container.duration / av.time_base)
            if container.duration
            else 0.0
        )
        interval = 1 / float(stream.average_rate) if stream.average_rate else 0.04
        for target in overview_times(duration, interval):
            if time.monotonic() - started > 10:
                raise ValueError("Video overview decoding timed out; request a smaller window.")
            container.seek(int((start + target) * av.time_base), backward=True)
            for frame in container.decode(stream):
                if time.monotonic() - started > 10:
                    raise ValueError("Video overview decoding timed out; request a smaller window.")
                if frame.time is None:
                    continue
                stamp = float(frame.time) - start
                if stamp + interval / 2 < target:
                    continue
                if round(stamp, 6) in seen:
                    break
                if max(frame.width, frame.height) > 4096:
                    raise ValueError(
                        "Video frame exceeds the 4096-pixel side limit; resize it first."
                    )
                image = cast(Any, frame).to_image()  # PyAV does not type this Pillow conversion.
                image.thumbnail((1280, 1280))
                out = io.BytesIO()
                image.convert("RGB").save(out, format="JPEG", quality=75)
                data = base64.b64encode(out.getvalue()).decode()
                encoded_bytes += len(data)
                if encoded_bytes > MAX_OUTPUT_BYTES:
                    raise ValueError(
                        "Video overview exceeds the 4 MiB frame budget; use a smaller window."
                    )
                blocks.extend(
                    [
                        {"type": "text", "text": f"Frame at t={stamp:.3f}s (target {target:.3f}s)"},
                        {"type": "image", "mime_type": "image/jpeg", "base64": data},
                    ]
                )
                seen.add(round(stamp, 6))
                break
            else:
                blocks.append({"type": "text", "text": f"No frame decoded near t={target:.3f}s."})
    if not seen:
        raise ValueError("No frames could be decoded.")
    return [
        {
            "type": "text",
            "text": (
                f"Uniform video overview of {path}; duration {duration:.3f}s; {len(seen)} frames. "
                "Samples near 0%, 25%, 50%, 75%, 100%, paired about one second apart. "
                "This is sparse visual sampling, not full-film inspection or audio transcription. "
                "Use explicit offset/limit seconds to inspect an action more closely."
            ),
        },
        *blocks,
    ]


class VideoOverviewMiddleware(AgentMiddleware):
    def __init__(self, backend_for: BackendFor) -> None:
        super().__init__()
        self.backend_for = backend_for

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        tools = []
        for tool in request.tools:
            if isinstance(tool, BaseTool) and tool.name == "read_file":
                description = "\n".join(
                    line
                    for line in tool.description.splitlines()
                    if not line.startswith("- For videos,")
                )
                description += (
                    "\nFor a video overview, pass only file_path: at most 10 timestamped frames "
                    "spread across the entire video. To inspect a local action, explicitly pass "
                    "offset and limit in seconds; that samples the requested window at 0.5 fps. "
                    "Neither mode transcribes audio or proves every action was inspected."
                )
                tool = tool.model_copy(update={"description": description})
            tools.append(tool)
        return await handler(request.override(tools=tools))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        from pathlib import PurePosixPath

        args = request.tool_call.get("args", {})
        path = args.get("file_path")
        if (
            request.tool_call.get("name") != "read_file"
            or not isinstance(path, str)
            or PurePosixPath(path).suffix.lower() not in VIDEO_SUFFIXES
            or set(args) != {"file_path"}
        ):
            return await handler(request)
        call_id = str(request.tool_call["id"])
        try:
            path = workspace_path(path)
            backend = require_backend(self.backend_for, request.runtime)
            result = await backend.aread(path)
            if result.error or result.file_data is None:
                raise WorkspaceError(result.error or "Video file unavailable.")
            content = result.file_data["content"]
            if len(content) > ((MAX_BYTES + 2) // 3) * 4:
                raise WorkspaceError("Video exceeds the 100 MiB overview limit.")
            raw = base64.b64decode(content, validate=True)
            if len(raw) > MAX_BYTES:
                raise WorkspaceError("Video exceeds the 100 MiB overview limit.")
            blocks = await asyncio.to_thread(overview_blocks, raw, path)
        except WorkspaceError as error:
            return ToolMessage(
                content=str(error), name="read_file", tool_call_id=call_id, status="error"
            )
        except Exception as error:  # noqa: BLE001 - decoder messages may contain untrusted metadata
            return ToolMessage(
                content=f"Video overview failed ({type(error).__name__}); use a smaller offset/limit window or extract frames in the sandbox.",
                name="read_file",
                tool_call_id=call_id,
                status="error",
            )
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Video overview sampled; timestamped frames follow.",
                        name="read_file",
                        tool_call_id=call_id,
                    ),
                    HumanMessage(
                        content_blocks=cast(list[ContentBlock], blocks),
                        additional_kwargs={"read_file_media_result": True, "read_file_path": path},
                    ),
                ]
            }
        )
