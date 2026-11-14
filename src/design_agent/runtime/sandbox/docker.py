"""The Docker sandbox runner: one container per thread, built from `sandbox/Dockerfile`.

Only this module touches the Docker socket; containers never see it. Each thread gets a container
and a named volume mounted at `/workspace`, so files survive an idle stop while processes do not,
as on Managed Deep Agents. Containers run with no network (default-deny egress), all capabilities
dropped and a pids limit. The agent's commands run as the image's unprivileged `node` user
(uid 1000); staging runs as root, so the staged brand pack, gates and skill copies are read-only
to the agent.

`DockerSandboxBackend` is the one backend instance a compiled graph holds: Deep Agents takes a
backend instance, not a factory, so each call resolves the current thread's container from the
run config.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import posixpath
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    ReadResult,
)
from deepagents.backends.sandbox import MAX_OUTPUT_BYTES, BaseSandbox
from langgraph.config import get_config

_LOGGER = logging.getLogger(__name__)

AGENT_USER = "1000:1000"
AGENT_HOME = "/home/node"
#: Where Deep Agents offloads conversation history, large tool results and media when it
#: summarizes. Its default is `/`, which the agent cannot write.
ARTIFACTS_ROOT = "/workspace/.agent"
#: Binary image reads use file transport, independently of the command stdout budget.
MAX_IMAGE_READ_BYTES = 20 * 1024 * 1024
MAX_VIDEO_READ_BYTES = 100 * 1024 * 1024
#: Paths the agent may write. Everything else in the container is image or staged content.
AGENT_WRITABLE = (
    "/workspace/projects",
    "/workspace/attachments",
    "/workspace/.renders",
    ARTIFACTS_ROOT,
    "/tmp",
)
#: Paths the agent may read back through the file tools.
AGENT_READABLE = ("/workspace", "/tmp", "/opt/design-agent/probe")
#: Directories created at container start. The first set belongs to the agent; the second is
#: staged by the host and stays root-owned.
AGENT_DIRS = (
    "/workspace/projects",
    "/workspace/attachments",
    "/workspace/references",
    "/workspace/.renders",
    ARTIFACTS_ROOT,
)
STAGED_DIRS = ("/workspace/skills", "/workspace/tools", "/workspace/brand", "/workspace/fonts")
DEFAULT_TIMEOUT_SECONDS = 30 * 60


class SandboxUnavailableError(RuntimeError):
    """Docker or the sandbox image is missing. The message says what to run."""


def _under(path: str, roots: Iterable[str]) -> bool:
    normalized = posixpath.normpath(path)
    return any(normalized == root or normalized.startswith(f"{root}/") for root in roots)


def current_thread_id(default: str = "local") -> str:
    """The thread of the run in progress, from the LangGraph run config."""
    try:
        configurable = get_config().get("configurable") or {}
    except RuntimeError:
        return default
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else default


@dataclass(frozen=True)
class DockerLimits:
    vcpus: int = 8
    mem_gib: int = 16
    pids: int = 4096
    shm_gib: int = 2


@dataclass
class _Container:
    name: str
    volume: str
    last_used: float = field(default_factory=time.monotonic)


class DockerSandboxRunner:
    """Creates, reuses and reaps per-thread containers. Thread-safe; one per process."""

    def __init__(
        self,
        *,
        image: str,
        limits: DockerLimits | None = None,
        idle_ttl_seconds: int = 600,
        docker: str = "docker",
        prefix: str = "design-agent",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        retention_seconds: int = 3 * 24 * 3600,
    ) -> None:
        self.image = image
        self.limits = limits or DockerLimits()
        self.idle_ttl_seconds = idle_ttl_seconds
        self._docker = docker
        self._prefix = prefix
        self._clock = clock
        self._wall_clock = wall_clock
        self.retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._containers: dict[str, _Container] = {}

    # -- docker plumbing ------------------------------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float | None = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        if shutil.which(self._docker) is None:
            raise SandboxUnavailableError(
                "Docker is not installed or not on PATH; install Docker or choose another sandbox"
            )
        result = subprocess.run(  # noqa: S603 - fixed docker argv, never a shell
            [self._docker, *args],
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            # The CLI ends with "Run 'docker run --help' …"; the daemon's reason is above it.
            lines = [
                line
                for line in result.stderr.decode(errors="replace").strip().splitlines()
                if line.strip() and not line.startswith("Run '")
            ]
            detail = lines[-1] if lines else ""
            raise SandboxUnavailableError(f"docker {args[0]} failed: {detail[:300]}")
        return result

    def _host_limits(self) -> tuple[str, str]:
        """`--cpus` and `--memory`: the configured limits, capped at the Docker host's. The daemon
        refuses more CPUs than it has, and a memory limit above the VM's only hides the OOM."""
        info = self._run(["info", "--format", "{{.NCPU}} {{.MemTotal}}"], timeout=30, check=False)
        try:
            cpus, memory = (int(part) for part in info.stdout.split())
        except ValueError:
            cpus, memory = 0, 0
        wanted = self.limits.mem_gib * 1024**3
        return (
            str(min(self.limits.vcpus, cpus) if cpus > 0 else self.limits.vcpus),
            str(min(wanted, memory) if memory > 0 else wanted),
        )

    def available(self) -> tuple[bool, str]:
        """Whether Docker answers and the image exists, with a reason when not."""
        try:
            self._run(["version", "--format", "{{.Server.Version}}"], timeout=15)
        except (SandboxUnavailableError, subprocess.TimeoutExpired) as error:
            return False, str(error) or "docker did not answer"
        found = self._run(["image", "inspect", self.image], timeout=15, check=False)
        if found.returncode != 0:
            return False, (
                f"sandbox image {self.image} is not built; run "
                f"`docker build -t {self.image} sandbox/` (or `design-agent sandbox build`)"
            )
        return True, "ok"

    def names_for(self, thread_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(thread_id.encode()).hexdigest()[:16]
        return f"{self._prefix}-{digest}", f"{self._prefix}-ws-{digest}"

    def container_for(self, thread_id: str) -> str:
        """The running container for `thread_id`, created or restarted as needed."""
        with self._lock:
            known = self._containers.get(thread_id)
            if known is None:
                name, volume = self.names_for(thread_id)
                known = _Container(name=name, volume=volume)
                self._containers[thread_id] = known
            self._ensure_running(known)
            known.last_used = self._clock()
            return known.name

    def _ensure_running(self, container: _Container) -> None:
        state = self._run(
            ["container", "inspect", "--format", "{{.State.Running}}", container.name],
            check=False,
            timeout=30,
        )
        if state.returncode == 0 and state.stdout.strip() == b"true":
            return
        if state.returncode == 0:
            self._run(["start", container.name], timeout=60)
        else:
            cpus, memory = self._host_limits()
            self._run(
                [
                    "run",
                    "--detach",
                    "--name",
                    container.name,
                    "--label",
                    f"{self._prefix}.thread=1",
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    # Root needs these three to stage read-only files; the agent's uid has none.
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "FOWNER",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    str(self.limits.pids),
                    "--cpus",
                    cpus,
                    "--memory",
                    memory,
                    "--shm-size",
                    f"{self.limits.shm_gib}g",
                    "--mount",
                    f"type=volume,source={container.volume},target=/workspace",
                    self.image,
                    "sleep",
                    "infinity",
                ],
                timeout=120,
            )
        dirs = " ".join(shlex.quote(d) for d in (*AGENT_DIRS, *STAGED_DIRS))
        agent_dirs = " ".join(shlex.quote(d) for d in AGENT_DIRS)
        self._run(
            [
                "exec",
                container.name,
                "sh",
                "-c",
                f"mkdir -p {dirs} && chown {AGENT_USER} {agent_dirs}",
            ],
            timeout=60,
        )

    def reap_idle(self, *, protected: Callable[[str], bool] = lambda _: False) -> list[str]:
        """Stop containers idle past the TTL. Their volumes (the thread's files) stay."""
        stopped: list[str] = []
        now = self._clock()
        with self._lock:
            for thread, container in self._containers.items():
                if protected(thread):
                    container.last_used = self._clock()
                    continue
                if now - container.last_used >= self.idle_ttl_seconds:
                    self._run(["stop", "--time", "5", container.name], check=False, timeout=60)
                    stopped.append(container.name)
        return stopped

    def _inspect(self, kind: str, name: str) -> dict[str, object]:
        result = self._run([kind, "inspect", "--format", "{{json .}}", name])
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("unexpected Docker inspect response")
        return data

    def _expired_workspace(self, data: dict[str, object]) -> tuple[str, str] | None:
        """Return this service's container and volume names only for a valid, expired exit."""
        state, config = data.get("State"), data.get("Config")
        if not isinstance(state, dict) or not isinstance(config, dict):
            return None
        name = str(data.get("Name") or "").removeprefix("/")
        match = re.fullmatch(rf"{re.escape(self._prefix)}-([0-9a-f]{{16}})", name)
        labels = config.get("Labels") or {}
        if (
            match is None
            or not isinstance(labels, dict)
            or labels.get(f"{self._prefix}.thread") != "1"
            or state.get("Status") != "exited"
            or any(state.get(key) for key in ("Running", "Paused", "Restarting"))
        ):
            return None
        finished = state.get("FinishedAt")
        if not isinstance(finished, str):
            return None
        timestamp = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.year <= 1:
            return None
        age = self._wall_clock() - timestamp.timestamp()
        if age < self.retention_seconds:
            return None
        volume = f"{self._prefix}-ws-{match[1]}"
        mounts = data.get("Mounts")
        if not isinstance(mounts, list) or not any(
            m.get("Type") == "volume"
            and m.get("Name") == volume
            and m.get("Destination") == "/workspace"
            for m in mounts
            if isinstance(m, dict)
        ):
            return None
        return name, volume

    def prune_unused(self, *, protected: Callable[[str], bool] = lambda _: False) -> list[str]:
        """Remove workspaces stopped past retention using Docker's own FinishedAt timestamp.

        `protected` receives a container name, so restored pending work can be protected without
        this process having accessed its sandbox yet. Orphan volumes have no exit time and stay.
        """
        if self.retention_seconds <= 0:
            return []
        listed = self._run(
            ["container", "ls", "--all", "--filter", f"label={self._prefix}.thread=1", "--quiet"]
        )
        removed: list[str] = []
        for container_id in listed.stdout.decode().split():
            try:
                with self._lock:
                    expired = self._expired_workspace(self._inspect("container", container_id))
                    if expired is None:
                        continue
                    container, volume = expired
                    if protected(container):
                        continue
                    holders = self._run(
                        ["container", "ls", "--all", "--filter", f"volume={volume}", "--quiet"]
                    )
                    if holders.stdout.decode().split() != [container_id]:
                        continue
                    self._run(["container", "rm", container_id])
                    # Docker refuses if another container acquired a reference in the meantime.
                    self._run(["volume", "rm", volume])
                    threads = [t for t, c in self._containers.items() if c.name == container]
                    for thread in threads:
                        self._containers.pop(thread, None)
                    removed.append(volume)
                    _LOGGER.info("[sandbox] removed unused workspace %s", volume)
            except (
                SandboxUnavailableError,
                subprocess.TimeoutExpired,
                OSError,
                ValueError,
            ) as error:
                _LOGGER.warning(
                    "[sandbox] workspace cleanup failed for %s: %s",
                    container_id,
                    type(error).__name__,
                )
        return removed

    def stop_all(self) -> None:
        with self._lock:
            for container in self._containers.values():
                self._run(["stop", "--time", "5", container.name], check=False, timeout=60)

    # -- execution and file transfer ------------------------------------------------------------

    def exec(
        self,
        thread_id: str,
        command: str,
        *,
        timeout: int | None = None,
        root: bool = False,
    ) -> ExecuteResponse:
        name = self.container_for(thread_id)
        seconds = DEFAULT_TIMEOUT_SECONDS if timeout is None or timeout <= 0 else int(timeout)
        # The in-container `timeout` kills the process tree; killing the docker client would not.
        wrapped = f"exec 2>&1; timeout --kill-after=10 {seconds} bash -c {shlex.quote(command)}"
        user = ["--user", "0:0"] if root else ["--user", AGENT_USER, "--env", f"HOME={AGENT_HOME}"]
        result = self._run(
            ["exec", *user, "--workdir", "/workspace", name, "bash", "-c", wrapped],
            timeout=seconds + 30,
            check=False,
        )
        output = result.stdout
        truncated = len(output) > MAX_OUTPUT_BYTES
        if truncated:
            output = output[:MAX_OUTPUT_BYTES]
        exit_code = result.returncode
        text = output.decode(errors="replace")
        if exit_code == 124:
            text += f"\n[command timed out after {seconds} s]"
        return ExecuteResponse(output=text, exit_code=exit_code, truncated=truncated)

    def put_archive(self, thread_id: str, archive: bytes, *, root: bool) -> None:
        """Extract a tar archive at `/` in the thread's container."""
        name = self.container_for(thread_id)
        user = ["--user", "0:0"] if root else ["--user", AGENT_USER]
        self._run(
            ["exec", "-i", *user, name, "tar", "-x", "-C", "/", "--no-same-owner", "-f", "-"],
            input_bytes=archive,
            timeout=300,
        )

    def get_file(self, thread_id: str, path: str, *, max_bytes: int | None = None) -> bytes | None:
        name = self.container_for(thread_id)
        command = ["cat", "--", path]
        if max_bytes is not None:
            # One extra byte distinguishes an oversized file from a complete read.
            # Keep this outside exec(): its stdout cap corrupts binary payloads.
            script = (
                "import os,stat,sys; p=sys.argv[1]; "
                "sys.exit(1) if not stat.S_ISREG(os.stat(p).st_mode) else None; "
                "f=open(p,'rb'); sys.stdout.buffer.write(f.read(int(sys.argv[2])+1))"
            )
            command = ["python3", "-c", script, path, str(max_bytes)]
        result = self._run(["exec", "--user", AGENT_USER, name, *command], check=False, timeout=300)
        return result.stdout if result.returncode == 0 else None


def tar_of(files: Iterable[tuple[str, bytes]], *, mode: int = 0o644) -> bytes:
    """A tar archive holding `files` at their absolute paths (leading slash stripped)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path, content in files:
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size = len(content)
            info.mode = mode
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class DockerSandboxBackend(BaseSandbox):
    """The agent-facing backend: commands and file tools in the current thread's container."""

    def __init__(
        self, runner: DockerSandboxRunner, *, thread_id: Callable[[], str] = current_thread_id
    ) -> None:
        self._runner = runner
        self._thread_id = thread_id

    @property
    def id(self) -> str:
        return self._runner.names_for(self._thread_id())[0]

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._runner.exec(self._thread_id(), command, timeout=timeout)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        media_type = mimetypes.guess_type(file_path)[0] or ""
        image = media_type.startswith("image/")
        video = media_type.startswith("video/")
        if not image and not video:
            return super().read(file_path, offset, limit)
        if not _under(file_path, AGENT_READABLE):
            return ReadResult(error="permission_denied")
        cap = MAX_IMAGE_READ_BYTES if image else MAX_VIDEO_READ_BYTES
        try:
            raw = self._runner.get_file(self._thread_id(), file_path, max_bytes=cap)
        except SandboxUnavailableError:
            return ReadResult(error=f"Could not read '{file_path}': sandbox unavailable.")
        if raw is None:
            return ReadResult(
                error=f"Could not read '{file_path}': file unavailable or unreadable."
            )
        if len(raw) > cap:
            kind = "Image" if image else "Video"
            return ReadResult(
                error=f"{kind} exceeds the {cap // 1024**2} MiB read limit; create a smaller "
                "preview, crop or clip in the sandbox and read that instead. "
                "The original file is preserved."
            )
        return ReadResult(
            file_data={"content": base64.b64encode(raw).decode(), "encoding": "base64"}
        )

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        allowed = [(p, c) for p, c in files if _under(p, AGENT_WRITABLE)]
        responses = [
            FileUploadResponse(
                path=p, error=None if _under(p, AGENT_WRITABLE) else "permission_denied"
            )
            for p, _ in files
        ]
        if allowed:
            try:
                # Create parents as the agent so new folders stay writable to it.
                parents = sorted({posixpath.dirname(posixpath.normpath(p)) for p, _ in allowed})
                quoted = " ".join(shlex.quote(p) for p in parents)
                self._runner.exec(self._thread_id(), f"mkdir -p {quoted}")
                self._runner.put_archive(self._thread_id(), tar_of(allowed), root=False)
            except SandboxUnavailableError as error:
                _LOGGER.warning("[sandbox] upload failed: %s", error)
                return [FileUploadResponse(path=p, error="upload failed") for p, _ in files]
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not _under(path, AGENT_READABLE):
                responses.append(FileDownloadResponse(path=path, error="permission_denied"))
                continue
            content = self._runner.get_file(self._thread_id(), path)
            responses.append(
                FileDownloadResponse(
                    path=path,
                    content=content,
                    error=None if content is not None else "file_not_found",
                )
            )
        return responses


class DockerStagingTarget:
    """Privileged staging into the current thread's container: files land root-owned and
    read-only to the agent (`middleware.workspace.StagingTarget`)."""

    def __init__(
        self, runner: DockerSandboxRunner, *, thread_id: Callable[[], str] = current_thread_id
    ) -> None:
        self._runner = runner
        self._thread_id = thread_id

    def upload(self, files: list[tuple[str, bytes]]) -> None:
        self._runner.put_archive(self._thread_id(), tar_of(files), root=True)

    def execute(self, command: str, *, timeout: int) -> ExecuteResponse:
        return self._runner.exec(self._thread_id(), command, timeout=timeout, root=True)

    def read(self, path: str) -> bytes | None:
        return self._runner.get_file(self._thread_id(), path)
