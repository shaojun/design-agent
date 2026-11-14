"""What a long-running self-hosted process does in the background: stop idle sandbox containers
every minute and remove expired Docker workspaces and sweep what a restart killed every 15
minutes. Started next to the API, Slack or Feishu adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from design_agent.runtime.local import LocalRuntime

_LOGGER = logging.getLogger(__name__)

SWEEP_SECONDS = 15 * 60
REAP_SECONDS = 60


async def start(runtime: LocalRuntime, store: Any) -> None:
    """The first sweep and the check-ins a previous process kept, before serving."""
    await runtime.services.sweep(store)
    await runtime.waker.restore()


async def run(runtime: LocalRuntime, store: Any) -> None:
    elapsed = 0
    while True:
        await asyncio.sleep(REAP_SECONDS)
        elapsed += REAP_SECONDS
        runner = runtime.binding.runner
        if runner is not None:
            try:
                await asyncio.to_thread(runner.reap_idle, protected=runtime.thread_busy)
            except Exception as error:  # noqa: BLE001 - the next pass tries again
                _LOGGER.warning("[maintenance] reaping sandboxes failed: %s", type(error).__name__)
        if elapsed >= SWEEP_SECONDS:
            elapsed = 0
            if runner is not None:
                try:
                    await asyncio.to_thread(runner.prune_unused, protected=runtime.sandbox_busy)
                except Exception as error:  # noqa: BLE001 - retry on the next sweep
                    _LOGGER.warning(
                        "[maintenance] workspace cleanup failed: %s", type(error).__name__
                    )
            try:
                await runtime.services.sweep(store)
            except Exception as error:  # noqa: BLE001 - the next pass tries again
                _LOGGER.warning("[maintenance] the sweep failed: %s", type(error).__name__)
