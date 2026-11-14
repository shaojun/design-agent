"""Real Docker lifecycle, isolated names and a short virtual retention clock; no model calls."""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

import pytest

from design_agent.runtime.sandbox.docker import DockerSandboxRunner


@pytest.mark.render
def test_real_docker_retention_and_workspace_recreation() -> None:
    prefix = f"da-retention-test-{uuid.uuid4().hex[:12]}"
    now = [time.time()]
    kwargs = {
        "image": os.environ.get("DESIGN_AGENT_SANDBOX_IMAGE", "design-agent-sandbox:local"),
        "prefix": prefix,
        "retention_seconds": 2,
        "wall_clock": lambda: now[0],
        "clock": lambda: now[0],
        "idle_ttl_seconds": 1,
    }
    runner = DockerSandboxRunner(**kwargs)
    threads = ("expired", "running")
    try:
        assert (
            runner.exec("expired", "printf retained > /workspace/projects/source.txt").exit_code
            == 0
        )
        assert runner.get_file("expired", "/workspace/projects/source.txt") == b"retained"
        now[0] += 1
        assert runner.reap_idle() == [runner.names_for("expired")[0]]
        # Verify idle stop preserves files before the retention threshold.
        assert runner.get_file("expired", "/workspace/projects/source.txt") == b"retained"
        runner.stop_all()
        state = runner._inspect("container", runner.names_for("expired")[0])["State"]
        runner.stop_all()
        assert (
            runner._inspect("container", runner.names_for("expired")[0])["State"]["FinishedAt"]
            == state["FinishedAt"]
        )
        finished = datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00")).timestamp()
        now[0] = finished + 1
        assert runner.prune_unused() == []
        runner.exec("running", "true")
        now[0] = finished + 3
        restarted = DockerSandboxRunner(**kwargs)
        assert restarted.prune_unused() == [runner.names_for("expired")[1]]
        assert runner.names_for("running")[1] not in restarted.prune_unused()
        assert runner.get_file("running", "/workspace/projects/missing.txt") is None
        # Expired threads get a fresh sandbox with no stale project files.
        assert restarted.exec("expired", "test ! -e /workspace/projects/source.txt").exit_code == 0
        assert restarted.exec("expired", "printf new > /workspace/projects/new.txt").exit_code == 0
        assert restarted.get_file("expired", "/workspace/projects/new.txt") == b"new"
    finally:
        # Only this test's deterministic resources, even if an assertion fails midway.
        for thread in threads:
            container, volume = runner.names_for(thread)
            runner._run(["stop", "--time", "1", container], check=False)
            runner._run(["container", "rm", container], check=False)
            runner._run(["volume", "rm", volume], check=False)
