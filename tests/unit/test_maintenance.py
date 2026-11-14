"""The service loop runs retention on its existing housekeeping cadence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from design_agent.runtime import maintenance


@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_cleanup_runs_every_fifteen_minutes_without_blocking_housekeeping(
    monkeypatch: pytest.MonkeyPatch, fail_cleanup: bool
) -> None:
    calls: list[str] = []
    sleeps = 0

    async def sleep(seconds: float) -> None:
        nonlocal sleeps
        assert seconds == 60
        sleeps += 1
        if sleeps > 30:
            raise asyncio.CancelledError

    async def to_thread(func: Any, **kwargs: Any) -> Any:
        return func(**kwargs)

    def reap_idle(**kwargs: Any) -> None:
        assert kwargs["protected"]("busy")
        calls.append("reap")

    def prune_unused(**kwargs: Any) -> None:
        assert kwargs["protected"]("busy")
        calls.append("prune")
        if fail_cleanup:
            raise RuntimeError("test failure")

    async def sweep(store: Any) -> None:
        calls.append("sweep")

    runtime = SimpleNamespace(
        binding=SimpleNamespace(
            runner=SimpleNamespace(reap_idle=reap_idle, prune_unused=prune_unused)
        ),
        thread_busy=lambda thread: thread == "busy",
        sandbox_busy=lambda container: container == "busy",
        services=SimpleNamespace(sweep=sweep),
    )
    monkeypatch.setattr(maintenance.asyncio, "sleep", sleep)
    monkeypatch.setattr(maintenance.asyncio, "to_thread", to_thread)
    with pytest.raises(asyncio.CancelledError):
        await maintenance.run(runtime, object())
    assert calls == [*(["reap"] * 15), "prune", "sweep", *(["reap"] * 15), "prune", "sweep"]
