"""Retention uses Docker FinishedAt, with no host usage files or extra volume metadata."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from design_agent.harness.protocols import WakeTarget
from design_agent.runtime.local import LocalRuntime
from design_agent.runtime.sandbox.docker import DockerSandboxRunner, SandboxUnavailableError
from design_agent.runtime.waker.local import InProcessWaker


class RetentionRunner(DockerSandboxRunner):
    def __init__(self, now: list[float], daemon: dict[str, Any] | None = None) -> None:
        super().__init__(
            image="test-image",
            retention_seconds=10,
            wall_clock=lambda: now[0],
            clock=lambda: now[0],
            idle_ttl_seconds=2,
        )
        self.now = now
        self.daemon = daemon if daemon is not None else {"volumes": {}, "containers": {}}
        self.calls: list[list[str]] = []
        self.fail: str | None = None

    def _run(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        volumes, containers = self.daemon["volumes"], self.daemon["containers"]
        value: Any = b"ok"
        if args[:2] == ["container", "ls"]:
            condition = args[args.index("--filter") + 1]
            if condition.startswith("volume="):
                names = [
                    name
                    for name, c in containers.items()
                    if c["Mounts"][0]["Name"] == condition.removeprefix("volume=")
                ]
            else:
                assert condition == "label=design-agent.thread=1"
                names = list(containers)
            value = "\n".join(names).encode()
        elif args[:2] == ["container", "inspect"]:
            if args[-1] not in containers:
                return subprocess.CompletedProcess(args, 1, b"", b"not found")
            data = containers[args[-1]]
            value = (
                str(data["State"]["Running"]).lower().encode()
                if args[3] == "{{.State.Running}}"
                else json.dumps(data).encode()
            )
        elif args[0] == "info":
            value = b"8 17179869184"
        elif args[0] == "run":
            name = args[args.index("--name") + 1]
            volume = args[args.index("--mount") + 1].split("source=")[1].split(",")[0]
            volumes.setdefault(volume, {"Labels": None})
            containers[name] = {
                "Name": "/" + name,
                "Config": {"Labels": {"design-agent.thread": "1"}},
                "State": {"Status": "running", "Running": True},
                "Mounts": [{"Type": "volume", "Name": volume, "Destination": "/workspace"}],
            }
        elif args[0] == "stop":
            if containers[args[-1]]["State"]["Running"]:
                containers[args[-1]]["State"] = {
                    "Status": "exited",
                    "Running": False,
                    "FinishedAt": datetime.fromtimestamp(self.now[0], UTC).isoformat(),
                }
        elif args[0] == "start":
            containers[args[-1]]["State"]["Status"] = "running"
            containers[args[-1]]["State"]["Running"] = True
        elif args[:2] == ["container", "rm"]:
            if self.fail == "container" or containers[args[-1]]["State"]["Running"]:
                raise SandboxUnavailableError("container still in use")
            del containers[args[-1]]
        elif args[:2] == ["volume", "rm"]:
            if self.fail == "volume":
                raise SandboxUnavailableError("volume still in use")
            del volumes[args[-1]]
        return subprocess.CompletedProcess(args, 0, value, b"")


@pytest.fixture
def runner() -> RetentionRunner:
    return RetentionRunner([1_700_000_000.0])


def create_stopped(runner: RetentionRunner, thread: str = "a") -> str:
    runner.container_for(thread)
    runner.stop_all()
    return runner.names_for(thread)[1]


def test_retention_boundary_and_removal_order(runner: RetentionRunner) -> None:
    volume = create_stopped(runner)
    runner.now[0] += 9
    assert runner.prune_unused() == []
    runner.now[0] += 1
    assert runner.prune_unused() == [volume]
    removals = [c for c in runner.calls if "rm" in c]
    assert [c[:2] for c in removals] == [["container", "rm"], ["volume", "rm"]]
    assert not any("--force" in c or "-f" in c for c in removals)
    runner.container_for("a")
    assert volume in runner.daemon["volumes"]
    assert not any(c[:2] == ["volume", "create"] for c in runner.calls)


def test_finished_at_survives_host_restart_without_usage_records(runner: RetentionRunner) -> None:
    volume = create_stopped(runner)
    restarted = RetentionRunner(runner.now, runner.daemon)
    assert not restarted._containers
    runner.now[0] += 10
    assert restarted.prune_unused() == [volume]


def test_restart_and_stop_reset_retention(runner: RetentionRunner) -> None:
    volume = create_stopped(runner)
    runner.now[0] += 9
    runner.container_for("a")
    runner.stop_all()
    runner.now[0] += 9
    assert runner.prune_unused() == []
    runner.now[0] += 1
    assert runner.prune_unused() == [volume]


def test_repeated_stop_does_not_extend_retention(runner: RetentionRunner) -> None:
    volume = create_stopped(runner)
    runner.now[0] += 9
    runner.reap_idle()
    runner.now[0] += 1
    assert runner.prune_unused() == [volume]


def test_running_container_and_recent_stop_are_protected(runner: RetentionRunner) -> None:
    runner.container_for("a")
    runner.now[0] += 100
    assert runner.prune_unused() == []
    runner.stop_all()
    assert runner.prune_unused() == []
    runner.now[0] += 10
    assert runner.prune_unused() == [runner.names_for("a")[1]]


def test_pending_work_protects_stopped_workspace(runner: RetentionRunner) -> None:
    volume = create_stopped(runner)
    runner.now[0] += 100
    restarted = RetentionRunner(runner.now, runner.daemon)
    assert restarted.prune_unused(protected=lambda name: name == runner.names_for("a")[0]) == []
    assert volume in runner.daemon["volumes"]


async def test_restored_check_in_protects_workspace_without_container_cache(
    runner: RetentionRunner,
) -> None:
    store = InMemoryStore()

    async def turn(*_: Any) -> None:
        raise AssertionError("future check-in must not fire")

    first = InProcessWaker(turn, store=store)
    key = await first.schedule(WakeTarget("a"), datetime.now(UTC) + timedelta(days=4), "check")
    for task in first._timers.values():
        task.cancel()
    second = InProcessWaker(turn, store=store)
    try:
        assert await second.restore() == 1
        assert second.pending_threads() == {"a"}
        volume = create_stopped(runner)
        runner.now[0] += 100
        restarted = RetentionRunner(runner.now, runner.daemon)
        runtime = SimpleNamespace(
            binding=SimpleNamespace(runner=restarted),
            _locks={},
            waker=second,
            thread_busy=lambda thread: second.pending(thread) > 0,
        )
        assert (
            restarted.prune_unused(protected=lambda name: LocalRuntime.sandbox_busy(runtime, name))
            == []
        )
        assert volume in runner.daemon["volumes"]
        await second.cancel(key)
        assert second.pending_threads() == set()
        assert restarted.prune_unused(
            protected=lambda name: LocalRuntime.sandbox_busy(runtime, name)
        ) == [volume]
    finally:
        await second.cancel(key)


def test_reaper_keeps_active_turn_alive(runner: RetentionRunner) -> None:
    runner.container_for("a")
    runner.now[0] += 100
    assert runner.reap_idle(protected=lambda _: True) == []
    runner.now[0] += 1
    assert runner.reap_idle() == []
    runner.now[0] += 1
    assert runner.reap_idle() == [runner.names_for("a")[0]]


@pytest.mark.parametrize(
    "finished",
    [None, "", "bad-date", "0001-01-01T00:00:00Z", "2023-01-01T00:00:00", "2099-01-01T00:00:00Z"],
)
def test_missing_invalid_or_future_finished_at_is_skipped(
    runner: RetentionRunner, finished: Any
) -> None:
    volume = create_stopped(runner)
    runner.daemon["containers"][runner.names_for("a")[0]]["State"]["FinishedAt"] = finished
    runner.now[0] += 100
    assert runner.prune_unused() == []
    assert volume in runner.daemon["volumes"]


@pytest.mark.parametrize(
    "condition", ["foreign", "paused", "created", "wrong_mount", "extra_holder", "rm_failure"]
)
def test_unsafe_container_never_leads_to_volume_removal(
    runner: RetentionRunner, condition: str
) -> None:
    volume = create_stopped(runner)
    container = runner.daemon["containers"][runner.names_for("a")[0]]
    if condition == "foreign":
        container["Config"]["Labels"] = {}
    elif condition in {"paused", "created"}:
        container["State"]["Status"] = condition
    elif condition == "wrong_mount":
        container["Mounts"][0]["Destination"] = "/other"
    elif condition == "extra_holder":
        runner.daemon["containers"]["foreign"] = container
    else:
        runner.fail = "container"
    runner.now[0] += 100
    assert runner.prune_unused() == []
    assert volume in runner.daemon["volumes"]
    assert not any(c[:2] == ["volume", "rm"] for c in runner.calls)


def test_volume_removal_failure_leaves_orphan_for_operator(runner: RetentionRunner) -> None:
    volume = create_stopped(runner)
    runner.now[0] += 10
    runner.fail = "volume"
    assert runner.prune_unused() == []
    assert not runner.daemon["containers"]
    runner.fail = None
    assert runner.prune_unused() == []
    assert volume in runner.daemon["volumes"]


def test_orphan_volumes_are_not_deleted_or_adopted(runner: RetentionRunner) -> None:
    runner.daemon["volumes"]["design-agent-ws-" + "a" * 16] = {"Labels": None}
    assert runner.prune_unused() == []
    assert runner.calls == [
        ["container", "ls", "--all", "--filter", "label=design-agent.thread=1", "--quiet"]
    ]


def test_disabled_retention_never_scans_docker(runner: RetentionRunner) -> None:
    runner.retention_seconds = 0
    assert runner.prune_unused() == []
    assert runner.calls == []
