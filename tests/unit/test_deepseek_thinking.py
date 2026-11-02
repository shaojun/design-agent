"""DeepSeek reasoning survives the real tool graph, streaming and durable recovery, offline."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore

from design_agent.config import Settings
from design_agent.harness.durable import TranscriptStore
from design_agent.models.factory import build_chat_model


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("THINKING_TEST_KEY", "offline-only")
    return Settings(
        _env_file=None,
        model="openai:deepseek-flash",
        model_base_url="https://deepseek.example/v1",
        model_api_key_env="THINKING_TEST_KEY",
    )


def response(n: int, streaming: bool) -> httpx.Response:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": f"answer-{n}",
        "reasoning_content": f"synthetic-{n}",
    }
    if n < 3:
        message["tool_calls"] = [
            {
                "id": f"call-{n}",
                "type": "function",
                "function": {"name": "step", "arguments": json.dumps({"n": n})},
            }
        ]
    reason = "tool_calls" if n < 3 else "stop"
    if not streaming:
        return httpx.Response(
            200,
            json={
                "id": f"reply-{n}",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": reason,
                    }
                ],
            },
        )
    deltas: list[dict[str, Any]] = [
        {"role": "assistant", "reasoning_content": "synthetic-"},
        {"reasoning_content": str(n)},
        {"content": message["content"]},
    ]
    if "tool_calls" in message:
        deltas.append({"tool_calls": [{"index": 0, **message["tool_calls"][0]}]})
    chunks = [
        {
            "id": f"reply-{n}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": None,
                }
            ],
        }
        for delta in deltas
    ]
    chunks.append(
        {
            "id": f"reply-{n}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": reason,
                }
            ],
        }
    )
    # The final usage-only chunk must not discard the accumulated reasoning.
    chunks.append(
        {
            "id": f"reply-{n}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("deep_agent", [False, True])
async def test_reasoning_survives_tools_checkpoint_and_next_user_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, streaming: bool, deep_agent: bool
) -> None:
    seen: list[dict[str, Any]] = []
    executed: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return response(len(seen), streaming)

    @tool
    def step(n: int) -> str:
        """Perform a synthetic offline step."""
        executed.append(n)
        return f"done-{n}"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = build_chat_model(
            settings(monkeypatch), http_async_client=client, streaming=streaming
        )
        build = create_deep_agent if deep_agent else create_agent
        config = {"configurable": {"thread_id": "thinking-test"}}
        database = str(tmp_path / "checkpoint.sqlite")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            graph = build(model, tools=[step], checkpointer=saver)
            result = await graph.ainvoke({"messages": [HumanMessage("Do two steps")]}, config)
        assert executed == [1, 2]
        replies = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert [m.additional_kwargs.get("reasoning_content") for m in replies] == [
            "synthetic-1",
            "synthetic-2",
            "synthetic-3",
        ]
        assert all("synthetic-" not in m.text for m in replies)
        transcripts = TranscriptStore(InMemoryStore())
        await transcripts.append("helper", result["messages"])
        restored = await transcripts.load("helper")
        assert [
            m.additional_kwargs.get("reasoning_content")
            for m in restored
            if isinstance(m, AIMessage)
        ] == ["synthetic-1", "synthetic-2", "synthetic-3"]
        # Reopen the SQLite checkpoint with a new graph, as after a process restart.
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            graph = build(model, tools=[step], checkpointer=saver)
            await graph.ainvoke({"messages": [HumanMessage("Continue")]}, config)
    assert len(seen) == 4
    for n, payload in enumerate(seen):
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        history = [m for m in payload["messages"] if m["role"] == "assistant"]
        assert [m.get("reasoning_content") for m in history] == [
            f"synthetic-{i}" for i in range(1, n + 1)
        ]
    assert executed == [1, 2]  # Recovery never repeats completed tool calls.


def test_old_non_thinking_history_is_preserved_without_inventing_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = build_chat_model(settings(monkeypatch))
    old = AIMessage("old", tool_calls=[{"id": "old-call", "name": "step", "args": {"n": 1}}])
    payload = model._get_request_payload(
        [
            HumanMessage("old request"),
            old,
            ToolMessage("done", tool_call_id="old-call"),
            AIMessage("old final"),
            HumanMessage("continue"),
        ]
    )
    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in assistants] == ["", ""]
    assert assistants[0]["tool_calls"][0]["id"] == "old-call"
    assert "reasoning_content" not in old.additional_kwargs


@pytest.mark.parametrize(
    "requested,actual",
    [(None, "high"), ("low", "low"), ("medium", "high"), ("xhigh", "high"), ("max", "max")],
)
def test_deepseek_effort_uses_its_own_wire_parameter(
    monkeypatch: pytest.MonkeyPatch, requested: str | None, actual: str
) -> None:
    model = build_chat_model(settings(monkeypatch), "helper", effort=requested)
    payload = model._get_request_payload([HumanMessage("test")])
    assert payload["reasoning_effort"] == actual
    assert "reasoning" not in payload
