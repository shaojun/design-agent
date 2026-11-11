from pathlib import Path
from typing import Any

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from design_agent.middleware.video_read import (
    VideoOverviewMiddleware,
    overview_blocks,
    overview_times,
)
from design_agent.runtime.sandbox.docker import DockerSandboxBackend
from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call
from tests.unit.test_docker_runner import LocalTransportRunner, make_video


def test_overview_times_cover_long_video_and_deduplicate_short_video() -> None:
    times = overview_times(1000, 0.04)
    assert len(times) == 10
    assert times[:4] == [0, 1, 250, 251]
    assert 999 < times[-1] < 1000
    short = overview_times(0.5, 0.04)
    assert short == sorted(set(short)) and len(short) <= 10
    with pytest.raises(ValueError):
        overview_times(float("nan"), 0.04)


def test_overview_decodes_the_end_of_long_video_in_ten_frames(tmp_path: Path) -> None:
    blocks = overview_blocks(make_video(tmp_path / "long.mp4", seconds=150, small=True), "long.mp4")
    assert sum(b["type"] == "image" for b in blocks) == 10
    text = " ".join(b["text"] for b in blocks if b["type"] == "text")
    assert "t=149." in text and "t=75." in text and "sparse" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [False, True])
async def test_video_tool_overview_and_explicit_window_reach_model(
    tmp_path: Path, window: bool
) -> None:
    path = tmp_path / "workspace" / "attachments" / "long.mp4"
    path.parent.mkdir(parents=True)
    make_video(path, seconds=150, small=True)

    class Runner(LocalTransportRunner):
        def get_file(
            self, thread_id: str, path: str, *, max_bytes: int | None = None
        ) -> bytes | None:
            return super().get_file(
                thread_id, str(tmp_path / path.lstrip("/")), max_bytes=max_bytes
            )

    backend = DockerSandboxBackend(Runner(), thread_id=lambda: "test")
    seen = []

    def respond(messages: Any) -> Any:
        last = messages[-1]
        if isinstance(last, HumanMessage) and last.additional_kwargs.get("read_file_media_result"):
            images = [b for b in last.content_blocks if b.get("type") == "image"]
            text = " ".join(b["text"] for b in last.content_blocks if b.get("type") == "text")
            if window:
                assert "[135.000s, 138.000s)" in text
            else:
                assert len(images) == 10 and "Uniform video overview" in text
            seen.append(True)
            return reply("Sample inspected.")
        args = {"file_path": "/workspace/attachments/long.mp4"}
        if window:
            args.update(offset=135, limit=3)
        return tool_call("read_file", args)

    graph = create_agent(
        RespondingChatModel(respond=respond),
        middleware=[
            FilesystemMiddleware(backend=backend),
            VideoOverviewMiddleware(lambda _: backend),
        ],
    )
    await graph.ainvoke({"messages": [{"role": "user", "content": "Inspect the video."}]})
    assert seen
