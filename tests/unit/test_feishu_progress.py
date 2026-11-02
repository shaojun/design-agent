"""Progress keeps rapid and parallel steps, survives API refusal, and bounds card sizes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from design_agent.surfaces.feishu import progress as module
from design_agent.surfaces.feishu.client import FeishuError
from design_agent.surfaces.feishu.progress import MAX_CARD_BYTES, STEPS_PER_GROUP, FeishuProgress


class Client:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []

    def send(self, chat: str, root: str, kind: str, card: dict[str, Any]) -> str:
        assert (chat, root, kind) == ("oc_chat", "dm", "interactive")
        self.sent.append(card)
        return f"om_{len(self.sent)}"

    def edit_card(self, message: str, card: dict[str, Any]) -> None:
        self.edited.append(card)


@pytest.fixture(autouse=True)
def fast_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MIN_UPDATE_SECONDS", 0.01)


async def test_parallel_and_rapid_steps_are_retained_with_actual_outcomes() -> None:
    client = Client()
    progress = FeishuProgress(client, ("oc_chat", "dm"))  # type: ignore[arg-type]
    await progress.start()
    first, second = await asyncio.gather(progress.begin("read_file"), progress.begin("write_file"))
    await progress.end(second, failed=True)
    assert first.ended is None
    await progress.end(first)
    await progress.close()
    card = client.edited[-1]
    assert len(client.sent) == 1
    assert "含失败步骤） · 已完成 1 步 · 1 步失败" in card["header"]["title"]["content"]
    panel = card["body"]["elements"][0]
    assert panel["expanded"] is False
    rows = panel["elements"][0]["content"]
    assert "1. ✅" in rows and "2. ❌" in rows
    assert first.done in rows and second.doing in rows
    assert not progress._pending


async def test_many_steps_continue_in_new_cards_without_discarding_history() -> None:
    client = Client()
    progress = FeishuProgress(client, ("oc_chat", "dm"))  # type: ignore[arg-type]
    await progress.start()
    for _ in range(400):
        step = await progress.begin("generate_image")
        await progress.end(step)
    await progress.close()
    assert len(client.sent) > 1
    assert len(progress.steps) == 400
    recorded = []
    for page in range(len(client.sent)):
        card = progress.card(page)
        assert progress._wire_size(card) < MAX_CARD_BYTES
        if page:
            assert card["header"]["title"]["content"].startswith("处理步骤续页")
        for panel in card["body"]["elements"]:
            rows = panel["elements"][0]["content"].splitlines()
            assert len(rows) <= STEPS_PER_GROUP
            recorded.extend(int(row.split(".")[0]) for row in rows)
    assert recorded == list(range(1, 401))


async def test_card_refusal_does_not_fail_turn_or_retry_forever() -> None:
    class Refused(Client):
        def send(self, *args: Any) -> str:
            raise FeishuError("230099")

    progress = FeishuProgress(Refused(), ("oc_chat", "dm"))  # type: ignore[arg-type]
    await progress.start()
    await progress.end(await progress.begin("read_file"))
    await progress.close()
    assert progress._disabled and not progress._pending


async def test_running_step_is_not_marked_successful_when_turn_fails() -> None:
    client = Client()
    progress = FeishuProgress(client, ("oc_chat", "dm"))  # type: ignore[arg-type]
    await progress.start()
    await progress.begin("generate_image")
    await progress.close(failed=True)
    assert progress.steps[0].failed
    assert client.edited[-1]["header"]["title"]["content"].startswith("本轮处理失败")


async def test_step_descriptions_and_failure_reasons_do_not_expose_arguments() -> None:
    client = Client()
    progress = FeishuProgress(client, ("oc_chat", "dm"))  # type: ignore[arg-type]
    await progress.start()
    step = await progress.begin("read_file", {"file_path": "/workspace/private-script.py"})
    assert step.done == "Read a script"
    await progress.end(step, failed=True, error="PermissionError: private-secret")
    await progress.close()
    rendered = str(client.edited[-1])
    assert "权限不足，操作被拒绝" in rendered
    assert "private-script" not in rendered and "private-secret" not in rendered
    assert " · t <1秒" in rendered
    assert "已记录" not in rendered


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0, "<1秒"), (0.9, "<1秒"), (1.2, "1秒"), (726, "12分6秒")]
)
def test_duration_distinguishes_subsecond_tools_from_total_elapsed(
    seconds: float, expected: str
) -> None:
    assert module.duration(seconds) == expected
