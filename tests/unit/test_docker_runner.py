"""The Docker sandbox runner's isolation flags and path policy (docker calls are recorded, not run)."""

from __future__ import annotations

import base64
import random
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from design_agent.runtime.sandbox import _with_skills
from design_agent.runtime.sandbox.docker import (
    ARTIFACTS_ROOT,
    DockerSandboxBackend,
    DockerSandboxRunner,
    SandboxUnavailableError,
)


class RecordingRunner(DockerSandboxRunner):
    def __init__(self, host_cpus: int = 16, host_mem_gib: int = 64, **kwargs: Any) -> None:
        super().__init__(image="design-agent-sandbox:test", **kwargs)
        self.calls: list[list[str]] = []
        self.running: set[str] = set()
        self.host = f"{host_cpus} {host_mem_gib * 1024**3}\n"

    def _run(
        self,
        args: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float | None = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        if args[0] == "container":
            name = args[-1]
            return subprocess.CompletedProcess(
                args,
                0 if name in self.running else 1,
                b"true" if name in self.running else b"",
                b"",
            )
        if args[0] == "info":
            return subprocess.CompletedProcess(args, 0, self.host.encode(), b"")
        if args[0] == "run":
            self.running.add(args[args.index("--name") + 1])
        if args[0] == "exec" and args[-2:-1] == ["cat"]:
            return subprocess.CompletedProcess(args, 0, b"content", b"")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")


def test_container_runs_isolated() -> None:
    runner = RecordingRunner()
    runner.container_for("thread-a")
    run = next(c for c in runner.calls if c[0] == "run")
    flag = lambda name: run[run.index(name) + 1]  # noqa: E731
    assert flag("--network") == "none"
    assert flag("--cap-drop") == "ALL"
    assert flag("--security-opt") == "no-new-privileges"
    assert "/var/run/docker.sock" not in " ".join(run)
    assert flag("--mount").startswith("type=volume,") and flag("--mount").endswith(
        "target=/workspace"
    )


@pytest.mark.parametrize(
    ("host_cpus", "host_mem_gib", "cpus", "mem_gib"), [(16, 64, "8", 16), (4, 8, "4", 8)]
)
def test_limits_never_exceed_the_docker_host(
    host_cpus: int, host_mem_gib: int, cpus: str, mem_gib: int
) -> None:
    runner = RecordingRunner(host_cpus=host_cpus, host_mem_gib=host_mem_gib)
    runner.container_for("t")
    run = next(c for c in runner.calls if c[0] == "run")
    assert run[run.index("--cpus") + 1] == cpus
    assert run[run.index("--memory") + 1] == str(mem_gib * 1024**3)


def test_a_failed_docker_call_reports_the_daemon_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = (
        b"docker: Error response from daemon: range of CPUs is from 0.01 to 4.00.\n"
        b"Run 'docker run --help' for more information\n"
    )
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        "subprocess.run", lambda args, **_kw: subprocess.CompletedProcess(args, 125, b"", stderr)
    )
    with pytest.raises(SandboxUnavailableError, match="range of CPUs"):
        DockerSandboxRunner(image="x")._run(["run"])


def test_threads_get_their_own_container_and_volume() -> None:
    runner = RecordingRunner()
    assert runner.container_for("a") != runner.container_for("b")
    assert runner.names_for("a")[1] != runner.names_for("b")[1]
    assert sum(1 for c in runner.calls if c[0] == "run") == 2
    runner.container_for("a")
    assert sum(1 for c in runner.calls if c[0] == "run") == 2, "a running container is reused"


def test_agent_commands_run_unprivileged_and_staging_runs_as_root() -> None:
    runner = RecordingRunner()
    runner.exec("t", "echo hi")
    agent = [c for c in runner.calls if c[0] == "exec"][-1]
    assert agent[agent.index("--user") + 1] == "1000:1000"
    runner.exec("t", "fc-cache", root=True)
    staged = [c for c in runner.calls if c[0] == "exec"][-1]
    assert staged[staged.index("--user") + 1] == "0:0"


def test_commands_are_killed_inside_the_container_on_timeout() -> None:
    runner = RecordingRunner()
    runner.exec("t", "sleep 100", timeout=7)
    script = [c for c in runner.calls if c[0] == "exec"][-1][-1]
    assert "timeout --kill-after=10 7 bash -c" in script


def test_uploads_outside_writable_folders_are_refused() -> None:
    runner = RecordingRunner()
    backend = DockerSandboxBackend(runner, thread_id=lambda: "t")
    results = backend.upload_files(
        [("/workspace/brand/tokens.json", b"{}"), ("/workspace/projects/x/a.txt", b"a")]
    )
    assert [r.error for r in results] == ["permission_denied", None]


def test_summarization_offloads_land_where_the_agent_can_write(tmp_path: Path) -> None:
    """Deep Agents writes conversation history, large results and media under the graph backend's
    artifacts root; the default `/` failed with permission_denied (issue 41)."""
    graph = _with_skills(DockerSandboxBackend(RecordingRunner(), thread_id=lambda: "t"), tmp_path)
    assert graph.artifacts_root == ARTIFACTS_ROOT
    media = f"{ARTIFACTS_ROOT}/conversation_history/media/910208ce0b6e3c89.png"
    backend = DockerSandboxBackend(RecordingRunner(), thread_id=lambda: "t")
    assert backend.upload_files([(media, b"png")])[0].error is None


@pytest.mark.parametrize("path", ["/etc/passwd", "/workspace/../etc/shadow", "/root/.ssh/id_rsa"])
def test_downloads_outside_readable_folders_are_refused(path: str) -> None:
    backend = DockerSandboxBackend(RecordingRunner(), thread_id=lambda: "t")
    assert backend.download_files([path])[0].error == "permission_denied"


def test_idle_containers_are_stopped_but_volumes_kept() -> None:
    now = [0.0]
    runner = RecordingRunner(idle_ttl_seconds=60, clock=lambda: now[0])
    runner.container_for("t")
    now[0] = 61
    assert runner.reap_idle() == [runner.names_for("t")[0]]
    assert not any(c[0] in {"rm", "volume"} for c in runner.calls)


class LocalTransportRunner(DockerSandboxRunner):
    """Execute the actual file scripts locally, replacing only the Docker transport."""

    def __init__(self) -> None:
        super().__init__(image="unused")

    def container_for(self, thread_id: str) -> str:
        return "fixture"

    def _run(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == "exec"
        return subprocess.run(args[args.index("fixture") + 1 :], capture_output=True, timeout=10)


@pytest.mark.parametrize("size", [(450, 340), (1200, 900)])
@pytest.mark.asyncio
async def test_image_reads_preserve_bytes_above_command_and_preview_caps(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    path = tmp_path / "reference.png"
    pixels = random.Random(17).randbytes(size[0] * size[1] * 3)  # noqa: S311 - repeatable pixels
    Image.frombytes("RGB", size, pixels).save(path)
    raw = path.read_bytes()
    assert len(base64.b64encode(raw)) > 512_000
    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    for result in (backend.read(str(path)), await backend.aread(str(path))):
        assert result.error is None
        assert result.file_data is not None
        assert result.file_data["encoding"] == "base64"
        assert base64.b64decode(result.file_data["content"]) == raw


def test_image_read_limit_and_file_errors_are_explicit(tmp_path: Path) -> None:
    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    path = tmp_path / "oversized.png"
    with path.open("wb") as output:
        output.truncate(20 * 1024**2 + 1)
    assert "20 MiB" in (backend.read(str(path)).error or "")
    assert backend.read(str(tmp_path / "missing.png")).error
    (tmp_path / "directory.png").mkdir()
    assert backend.read(str(tmp_path / "directory.png")).error
    assert "permission_denied" in (backend.read("/etc/private.png").error or "")


def test_text_pagination_and_command_output_cap_remain(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("first\nsecond\nthird\n")
    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    result = backend.read(str(path), offset=1, limit=1)
    assert result.error is None
    assert result.file_data is not None and result.file_data["content"] == "second"
    command = backend.execute("python3 -c 'print(\"x\" * 600000)'")
    assert command.truncated
    assert len(command.output.encode()) == 512_000


@pytest.mark.asyncio
async def test_large_image_reaches_the_model_as_a_complete_image_block(tmp_path: Path) -> None:
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents import create_agent
    from langchain_core.messages import ToolMessage

    from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call

    path = tmp_path / "large.png"
    Image.frombytes("RGB", (1200, 900), random.Random(19).randbytes(1200 * 900 * 3)).save(path)  # noqa: S311
    raw = path.read_bytes()
    seen = []

    def respond(messages: Any) -> Any:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            assert last.status == "success"
            blocks = [b for b in last.content_blocks if b.get("type") == "image"]
            assert len(blocks) == 1 and blocks[0]["mime_type"] == "image/png"
            assert base64.b64decode(blocks[0]["base64"]) == raw
            seen.append(True)
            return reply("Image received.")
        return tool_call("read_file", {"file_path": str(path)})

    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    graph = create_agent(
        RespondingChatModel(respond=respond), middleware=[FilesystemMiddleware(backend=backend)]
    )
    await graph.ainvoke({"messages": [{"role": "user", "content": "Read the image."}]})
    assert seen


def make_video(path: Path, *, seconds: int = 4, small: bool = False) -> bytes:
    import av

    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=12)
        width, height = (64, 36) if small else (640, 360)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        pixels = random.Random(31)  # noqa: S311 - fixture pixels
        for _ in range(seconds * 12):
            image = Image.frombytes("RGB", (width, height), pixels.randbytes(width * height * 3))
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path.read_bytes()


@pytest.mark.asyncio
async def test_video_read_decodes_a_window_into_model_image_blocks(tmp_path: Path) -> None:
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage, ToolMessage

    from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call

    path = tmp_path / "video.mp4"
    raw = make_video(path)
    assert len(raw) > 512_000
    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    result = await backend.aread(str(path))
    assert result.error is None and result.file_data is not None
    assert base64.b64decode(result.file_data["content"]) == raw
    seen = []

    def respond(messages: Any) -> Any:
        last = messages[-1]
        if isinstance(last, HumanMessage) and last.additional_kwargs.get("read_file_media_result"):
            images = [b for b in last.content_blocks if b.get("type") == "image"]
            texts = " ".join(b["text"] for b in last.content_blocks if b.get("type") == "text")
            assert 1 <= len(images) <= 64
            assert "[2.000s, 3.000s)" in texts and "00:00:02." in texts
            for image in images:
                assert image["mime_type"] == "image/jpeg"
                assert base64.b64decode(image["base64"]).startswith(b"\xff\xd8")
            assert any(isinstance(m, ToolMessage) and m.status == "success" for m in messages)
            seen.append(True)
            return reply("Video window received.")
        return tool_call("read_file", {"file_path": str(path), "offset": 2, "limit": 1})

    graph = create_agent(
        RespondingChatModel(respond=respond), middleware=[FilesystemMiddleware(backend=backend)]
    )
    await graph.ainvoke({"messages": [{"role": "user", "content": "Inspect seconds 2–3."}]})
    assert seen


def test_oversized_video_is_rejected_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "huge.mp4"
    with path.open("wb") as output:
        output.truncate(100 * 1024**2 + 1)
    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    assert "100 MiB" in (backend.read(str(path)).error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_video_sampling_caps_and_decode_errors_reach_the_model(
    tmp_path: Path, corrupt: bool
) -> None:
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage, ToolMessage

    from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call

    path = tmp_path / "long.mp4"
    if corrupt:
        path.write_bytes(b"not a video")
    else:
        make_video(path, seconds=150, small=True)
    seen = []

    def respond(messages: Any) -> Any:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            assert corrupt and last.status == "error"
            seen.append(True)
            return reply("Video decode failed.")
        if isinstance(last, HumanMessage) and last.additional_kwargs.get("read_file_media_result"):
            assert not corrupt
            blocks = last.content_blocks
            assert sum(b.get("type") == "image" for b in blocks) == 64
            assert "Coverage truncated" in str([b for b in blocks if b.get("type") == "text"])
            assert sum(len(b.get("base64", "")) for b in blocks) <= 4 * 1024**2
            seen.append(True)
            return reply("Only the sampled window was inspected.")
        return tool_call("read_file", {"file_path": str(path), "limit": 150})

    backend = DockerSandboxBackend(LocalTransportRunner(), thread_id=lambda: "t")
    graph = create_agent(
        RespondingChatModel(respond=respond), middleware=[FilesystemMiddleware(backend=backend)]
    )
    await graph.ainvoke({"messages": [{"role": "user", "content": "Inspect the video."}]})
    assert seen
