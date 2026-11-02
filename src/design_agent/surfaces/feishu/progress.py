"""Retained tool steps in collapsed Feishu cards; presentation failures never stop a turn."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from design_agent.surfaces.feishu.client import FeishuClient, FeishuError
from design_agent.surfaces.slack.phrasebook import phrase

_LOGGER = logging.getLogger(__name__)
MIN_UPDATE_SECONDS = 3.0
STEPS_PER_GROUP = 25
MAX_CARD_BYTES = 27_000  # Leave room for the API envelope and later status changes.


@dataclass
class Step:
    doing: str
    done: str
    started: float
    ended: float | None = None
    failed: bool = False
    reason: str = ""


def failure_reason(error: object) -> str:
    """Classify evidence into fixed text; tool output may contain secrets or user data."""
    text = str(error).lower()
    for needles, reason in (
        (("cancellederror",), "任务被取消或应用停止"),
        (("timeout", "timed out"), "操作超时"),
        (
            ("permissionerror", "permission denied", "access denied", "forbidden"),
            "权限不足，操作被拒绝",
        ),
        (("filenotfounderror", "no such file", "not found"), "所需文件或资源不存在"),
        (
            ("connectionerror", "connecterror", "connection refused", "connection reset"),
            "连接服务失败",
        ),
        (("rate limit", "ratelimit", "too many requests"), "请求过于频繁，服务限流"),
        (("validationerror", "invalid argument", "invalid parameter"), "工具参数不符合要求"),
    ):
        if any(needle in text for needle in needles):
            return reason
    return "工具报告失败，未提供可安全展示的具体原因"


def duration(seconds: float) -> str:
    if seconds < 1:
        return "<1秒"
    seconds = int(seconds)
    return f"{seconds // 60}分{seconds % 60}秒" if seconds >= 60 else f"{seconds}秒"


class FeishuProgress:
    def __init__(
        self, client: FeishuClient, target: tuple[str, str], *, enabled: bool = True
    ) -> None:
        self.client, self.target = client, target
        self.started = time.monotonic()
        self.steps: list[Step] = []
        self.messages: list[str] = []
        self._sent: list[dict[str, Any]] = []
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task[None] | None = None
        self._closed = False
        self._disabled = not enabled
        self._revision = 0
        self.outcome = ""
        self._page_starts = [0]

    async def start(self) -> None:
        await self._flush()

    async def begin(self, name: str, args: Mapping[str, Any] | None = None) -> Step:
        doing, done = phrase(name, args)
        step = Step(doing[:100], done[:100], time.monotonic())
        self.steps.append(step)
        self._schedule()
        return step

    async def end(self, step: Step, *, failed: bool = False, error: object = None) -> None:
        step.ended, step.failed = time.monotonic(), failed
        if failed:
            step.reason = failure_reason(error)
        self._schedule()

    def _schedule(self) -> None:
        self._revision += 1
        if self._closed or self._disabled or self._pending is not None:
            return
        self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0, self._last + MIN_UPDATE_SECONDS - time.monotonic()))
                revision = self._revision
                await self._flush()
                if self._closed or self._disabled or revision == self._revision:
                    break
        finally:
            self._pending = None

    @staticmethod
    def _wire_size(card: dict[str, Any]) -> int:
        content = json.dumps(card, ensure_ascii=False)
        return len(json.dumps({"content": content}, ensure_ascii=False).encode())

    def _paginate(self) -> None:
        # Boundaries only move forward: a sent step never migrates to another message.
        while True:
            start = self._page_starts[-1]
            end = min(start + STEPS_PER_GROUP, len(self.steps))
            if end <= start:
                return
            accepted = end
            while end <= len(self.steps):
                candidate = self._card(len(self._page_starts) - 1, start, end, reserve=True)
                if self._wire_size(candidate) > MAX_CARD_BYTES:
                    break
                accepted = end
                if end == len(self.steps):
                    return
                end = min(end + STEPS_PER_GROUP, len(self.steps))
            self._page_starts.append(accepted)

    def card(self, page: int) -> dict[str, Any]:
        self._paginate()
        start = self._page_starts[page]
        end = self._page_starts[page + 1] if page + 1 < len(self._page_starts) else len(self.steps)
        return self._card(page, start, end)

    def _card(self, page: int, start: int, end: int, *, reserve: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        completed = sum(s.ended is not None and not s.failed for s in self.steps)
        failed = sum(s.failed for s in self.steps)
        summary = (
            f"{self.outcome} · 已完成 {completed} 步"
            if self.outcome
            else f"已完成 {completed} 步 · 继续处理中…"
        )
        if failed:
            summary += f" · {failed} 步失败"
        summary += f" · 本轮总耗时 {duration(now - self.started)}"
        if page:
            summary = f"处理步骤续页 {page + 1} · 步骤 {start + 1}–{end}"
        panels = []
        for group_start in range(start, max(start + 1, end), STEPS_PER_GROUP):
            group_end = min(group_start + STEPS_PER_GROUP, end)
            rows = []
            for index in range(group_start, group_end):
                step = self.steps[index]
                icon = "❌" if step.failed else "✅" if step.ended is not None else "⏳"
                text = step.done if step.ended is not None and not step.failed else step.doing
                suffix = " · 失败" if step.failed else ""
                if reserve:
                    text = max((step.doing, step.done), key=lambda x: len(x.encode()))
                    suffix = " · 失败"
                rows.append(
                    f"{index + 1}. {icon} {text}{suffix} · t "
                    f"{duration((step.ended if step.ended is not None else now) - step.started)}"
                )
                if step.failed or reserve:
                    reason = step.reason or failure_reason(None)
                    rows.append(f"   原因：{reason}")
            panels.append(
                {
                    "tag": "collapsible_panel",
                    "element_id": f"progress_steps_{group_start}",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"处理步骤 {group_start + 1}–{group_end}（点击展开）"
                            if end
                            else "处理步骤（点击展开）",
                        },
                        "icon": {
                            "tag": "standard_icon",
                            "token": "down-small-ccm_outlined",
                            "size": "16px 16px",
                        },
                        "icon_position": "right",
                        "icon_expanded_angle": -180,
                    },
                    "elements": [
                        {"tag": "markdown", "content": "\n".join(rows) or "已收到请求，正在准备…"}
                    ],
                }
            )
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": summary}},
            "body": {"elements": panels},
        }

    async def _flush(self) -> None:
        async with self._lock:
            if self._disabled:
                return
            self._last = time.monotonic()
            try:
                self._paginate()
                pages = len(self._page_starts)
                for page in range(pages):
                    card = self.card(page)
                    if page == len(self.messages):
                        message = await asyncio.to_thread(
                            self.client.send, *self.target, "interactive", card
                        )
                        self.messages.append(message)
                        self._sent.append(card)
                    elif card != self._sent[page]:
                        await asyncio.to_thread(self.client.edit_card, self.messages[page], card)
                        self._sent[page] = card
            except FeishuError as error:
                self._disabled = True
                _LOGGER.warning("[feishu] progress card stopped: %s", error)

    async def close(self, *, failed: bool = False) -> None:
        self._closed = True
        if self._pending:
            # Let an in-flight HTTP update finish before the final update.
            await self._pending
        self.outcome = "本轮处理失败" if failed else "本轮处理完成"
        for step in self.steps:
            if step.ended is None:
                step.ended, step.failed = time.monotonic(), True
                step.reason = "本轮已结束，该步骤未返回结果"
        if not failed and any(s.failed for s in self.steps):
            self.outcome = "本轮处理完成（含失败步骤）"
        await self._flush()
