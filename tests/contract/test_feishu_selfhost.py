"""Feishu ingress through the real shared graph, with offline REST and sandbox boundaries."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from design_agent.config import Settings
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.feishu.app import (
    FeishuAdapter,
    FeishuFileDelivery,
    FeishuProgressMiddleware,
    message_content,
    target_of,
    thread_id_for,
)
from design_agent.surfaces.feishu.client import FeishuClient, FeishuError
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call
from design_agent.tools.delivery import DeliveredFile, LocalOutputAdapter

ROOT = Path(__file__).resolve().parents[2]


def event(mid: str = "om_first", *, direct: bool = False) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": "cli_test",
            "tenant_key": "tenant",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_person"},
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": mid,
                "chat_id": "oc_chat",
                "chat_type": "p2p" if direct else "group",
                "message_type": "post",
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
                "content": json.dumps(
                    {
                        "title": "Make a card",
                        "content": [
                            [
                                {"tag": "text", "text": "Use this reference"},
                                {"tag": "img", "image_key": "img_reference"},
                            ]
                        ],
                    }
                ),
            },
        },
    }


class FakeFeishu:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.sent: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self.respond)
        self.client = FeishuClient("cli_test", "test-secret", transport=self.transport)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        assert request.headers["authorization"] == "Bearer test-token"
        if "/resources/" in path:
            return httpx.Response(200, content=b"reference", headers={"content-type": "image/png"})
        if path == "/open-apis/im/v1/files":
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_result"}})
        if path == "/open-apis/bot/v3/info":
            return httpx.Response(200, json={"code": 0, "bot": {"open_id": "ou_bot"}})
        if request.method == "POST" and (
            path.endswith("/reply") or path == "/open-apis/im/v1/messages"
        ):
            self.sent.append({"path": path, **json.loads(request.content)})
        return httpx.Response(
            200, json={"code": 0, "data": {"message_id": f"om_sent_{len(self.calls)}"}}
        )


def lead(messages: Sequence[BaseMessage]) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage) and last.name == "write_file":
        return tool_call("upload_files", {"paths": ["/workspace/projects/card/card.txt"]})
    if isinstance(last, ToolMessage):
        return reply("Here is your card.")
    return tool_call(
        "write_file", {"file_path": "/workspace/projects/card/card.txt", "content": "card"}
    )


async def test_real_graph_attachments_delivery_retries_and_restart(tmp_path: Path) -> None:
    fake = FakeFeishu()
    store = InMemoryStore()
    adapter = FeishuAdapter(fake.client, "cli_test", "tenant", "ou_bot", store=store)
    model = RespondingChatModel(respond=lead)
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),  # type: ignore[call-arg]
        ROOT,
        model=model,
        binding=folder_binding(tmp_path / "sb", ROOT),
        store=store,
        replies=adapter.post_soon,
        surface="feishu",
        surface_middleware=(FeishuProgressMiddleware(adapter),),
        delivery=FeishuFileDelivery(fake.client, "tenant", LocalOutputAdapter(tmp_path / "out")),
    )
    adapter.runtime = runtime
    try:
        adapter.dispatch(event())
        adapter.dispatch(event())
        await asyncio.gather(*adapter._tasks)
        assert model.call_count == 3
        thread = thread_id_for("tenant", "oc_chat", "om_first")
        assert target_of(thread, "tenant") == ("oc_chat", "om_first")
        assert (
            tmp_path / "sb/workspace/attachments/om_first-0-img_reference.png"
        ).read_bytes() == b"reference"
        reactions = [
            r for r in fake.calls if r.method == "POST" and r.url.path.endswith("/reactions")
        ]
        assert len(reactions) == 1
        assert reactions[0].url.path.endswith("/messages/om_first/reactions")
        assert json.loads(reactions[0].content) == {"reaction_type": {"emoji_type": "Get"}}
        files = [item for item in fake.sent if item["msg_type"] == "file"]
        assert len(files) == 1 and files[0]["reply_in_thread"] is True
        assert files[0]["path"].endswith("/om_first/reply")
        cards = [item for item in fake.sent if item["msg_type"] == "interactive"]
        assert len(cards) == 1
        assert json.loads(cards[0]["content"])["body"]["elements"][0]["expanded"] is False
        updates = [r for r in fake.calls if r.method == "PATCH"]
        final_card = json.loads(json.loads(updates[-1].content)["content"])
        assert "本轮处理完成 · 已完成 2 步" in final_card["header"]["title"]["content"]
        steps = final_card["body"]["elements"][0]["elements"][0]["content"]
        assert "1. ✅" in steps and "2. ✅" in steps
        assert "test-secret" not in str(runtime.graph.get_graph())
        assert any(p.read_text() == "card" for p in (tmp_path / "out").rglob("card.txt"))
        assert (
            len(
                [
                    m
                    for m in fake.sent
                    if m["msg_type"] == "post"
                    and json.loads(m["content"])["zh_cn"]["content"][0][0]["text"]
                    == "Here is your card."
                ]
            )
            == 1
        )

        # A new adapter with the same persisted store ignores provider replay.
        restarted = FeishuAdapter(
            fake.client, "cli_test", "tenant", "ou_bot", runtime=runtime, store=store
        )
        await restarted.handle(event())
        assert model.call_count == 3
        # A file-only follow-up inside this known topic is accepted without another @.
        follow = event("om_follow")
        follow["event"]["message"].update(
            root_id="om_first",
            mentions=[],
            message_type="file",
            content=json.dumps({"file_key": "file_input", "file_name": "../../input.pdf"}),
        )
        await restarted.handle(follow)
        assert model.call_count > 3
        assert (
            tmp_path / "sb/workspace/attachments/om_follow-0-input.pdf"
        ).read_bytes() == b"reference"
    finally:
        await adapter.aclose()
        await runtime.services.aclose()
        fake.client.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["header"].update(tenant_key="other"),
        lambda p: p["header"].update(app_id="other"),
        lambda p: p["event"]["sender"].update(sender_type="app"),
        lambda p: p["event"]["sender"].update(sender_type="bot"),
        lambda p: p["event"]["sender"].update(tenant_key="other"),
        lambda p: p["event"]["message"].update(chat_type="unknown"),
    ],
)
def test_untrusted_identity_cannot_dispatch(mutate: Any, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeFeishu()
    adapter = FeishuAdapter(fake.client, "cli_test", "tenant", "ou_bot")
    payload = event()
    assert adapter.accepts(payload)
    mutate(payload)
    adapter.dispatch(payload)
    assert not adapter._tasks and not fake.calls
    assert "message event ignored:" in caplog.text
    assert "ou_person" not in caplog.text and "Use this reference" not in caplog.text
    fake.client.close()


@pytest.mark.parametrize("enable_progress", [True, False])
async def test_unmentioned_group_is_ignored_and_dm_context_is_continuous(
    enable_progress: bool,
) -> None:
    fake = FakeFeishu()
    store = InMemoryStore()

    class Runtime:
        threads: list[str] = []

        async def ask(self, text: str, *, thread_id: str) -> list[AIMessage]:
            self.threads.append(thread_id)
            return [reply("answer")]

    runtime = Runtime()
    adapter = FeishuAdapter(
        fake.client,
        "cli_test",
        "tenant",
        "ou_bot",
        runtime=runtime,
        store=store,
        enable_progress=enable_progress,
    )  # type: ignore[arg-type]
    payload = event()
    payload["event"]["message"].update(mentions=[])
    await adapter.handle(payload)
    assert not fake.calls
    for mid in ("om_dm1", "om_dm2"):
        payload = event(mid, direct=True)
        payload["event"]["message"].update(message_type="text", content='{"text":"hello"}')
        await adapter.handle(payload)
    assert runtime.threads == ["feishu:tenant:oc_chat:dm"] * 2
    assert all(m.get("receive_id") == "oc_chat" for m in fake.sent)
    assert sum(m["msg_type"] == "interactive" for m in fake.sent) == (2 if enable_progress else 0)
    assert sum(m["msg_type"] == "post" for m in fake.sent) == 2
    fake.client.close()


def test_rich_text_and_mentions_and_unsafe_resources() -> None:
    payload = event()["event"]["message"]
    text, files = message_content(payload, "ou_bot")
    assert "Use this reference" in text and files == [
        ("img_reference", "img_reference.png", "image")
    ]
    payload.update(message_type="text", content='{"text":"@_user_1 hi"}')
    assert message_content(payload, "ou_bot") == ("hi", [])
    payload.update(message_type="image", content='{"image_key":"../../secret"}')
    with pytest.raises(FeishuError, match="invalid_identifier"):
        message_content(payload, "ou_bot")
    payload.update(
        message_type="post",
        content=json.dumps({"content": [[{"tag": "img", "image_key": "img_ok"}] * 11]}),
    )
    with pytest.raises(FeishuError, match="too_many_attachments"):
        message_content(payload, "ou_bot")


def test_client_errors_never_echo_secrets_and_download_does_not_follow_redirects() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        return httpx.Response(302, headers={"location": "https://example.invalid/secret"})

    client = FeishuClient("app", "test-secret", transport=httpx.MockTransport(respond))
    with pytest.raises(FeishuError, match="download_http_302"):
        client.download("om_message", "file_key", "file")
    client.close()
    for status, body in [(200, {"code": 42, "msg": "test-secret"}), (500, {"msg": "test-secret"})]:
        client = FeishuClient(
            "app",
            "test-secret",
            transport=httpx.MockTransport(lambda r, s=status, b=body: httpx.Response(s, json=b)),
        )
        with pytest.raises(FeishuError) as caught:
            client.identity()
        assert "test-secret" not in str(caught.value)
        client.close()


def test_download_accepts_json_files_and_caps_the_actual_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from design_agent.surfaces.feishu import client as client_module

    body = b'{"palette":["blue"]}'

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = FeishuClient("app", "test-secret", transport=httpx.MockTransport(respond))
    assert client.download("om_message", "file_key", "file") == body
    monkeypatch.setattr(client_module, "MAX_DOWNLOAD_BYTES", 3)
    with pytest.raises(FeishuError, match="attachment_too_large"):
        client.download("om_message", "file_key", "file")
    client.close()


def test_delivery_rejects_wrong_tenant_and_entire_oversized_batch(tmp_path: Path) -> None:
    fake = FakeFeishu()
    delivery = FeishuFileDelivery(fake.client, "tenant", LocalOutputAdapter(tmp_path))
    with pytest.raises(FeishuError, match="wrong_delivery_target"):
        delivery.deliver("feishu:other:oc_chat:dm", [DeliveredFile("a", b"a")])
    with pytest.raises(FeishuError):
        delivery.deliver(
            "feishu:tenant:oc_chat:dm", [DeliveredFile("a", b"a"), DeliveredFile("empty", b"")]
        )
    assert not fake.calls
    fake.client.close()


async def test_resource_failure_does_not_invoke_agent() -> None:
    fake = FakeFeishu()
    original = fake.respond

    def respond(request: httpx.Request) -> httpx.Response:
        if "/resources/" in request.url.path:
            return httpx.Response(403)
        return original(request)

    client = FeishuClient("app", "test-secret", transport=httpx.MockTransport(respond))

    class Runtime:
        async def ask(self, *args: Any, **kwargs: Any) -> None:
            pytest.fail("must not run without the supplied reference")

    adapter = FeishuAdapter(
        client, "cli_test", "tenant", "ou_bot", runtime=Runtime(), store=InMemoryStore()
    )  # type: ignore[arg-type]
    await adapter.handle(copy.deepcopy(event()))
    assert any(
        m["msg_type"] == "post"
        and "处理失败" in json.loads(m["content"])["zh_cn"]["content"][0][0]["text"]
        for m in fake.sent
    )
    client.close()


async def test_reaction_failure_does_not_block_message_processing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeFeishu()
    original = fake.respond

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reactions"):
            return httpx.Response(403, json={"code": 999, "msg": "private response"})
        return original(request)

    client = FeishuClient("cli_test", "test-secret", transport=httpx.MockTransport(respond))

    class Runtime:
        called = False

        async def ask(self, *args: Any, **kwargs: Any) -> list[AIMessage]:
            self.called = True
            return [reply("answer")]

    runtime = Runtime()
    adapter = FeishuAdapter(
        client, "cli_test", "tenant", "ou_bot", runtime=runtime, store=InMemoryStore()
    )  # type: ignore[arg-type]
    payload = event("om_react_fail", direct=True)
    payload["event"]["message"].update(message_type="text", content='{"text":"hello"}')
    await adapter.handle(payload)
    assert runtime.called
    assert "receipt reaction unavailable" in caplog.text
    assert "private response" not in caplog.text
    await adapter.aclose()
    client.close()


async def test_new_dm_session_survives_restart_and_does_not_wait_for_old_turn() -> None:
    fake = FakeFeishu()
    store = InMemoryStore()
    old_started, release_old = asyncio.Event(), asyncio.Event()

    class Runtime:
        threads: list[str] = []

        async def ask(self, text: str, *, thread_id: str) -> list[AIMessage]:
            self.threads.append(thread_id)
            if text == "old task":
                old_started.set()
                await release_old.wait()
            return [reply("answer")]

    runtime = Runtime()
    adapter = FeishuAdapter(
        fake.client, "cli_test", "tenant", "ou_bot", runtime=runtime, store=store
    )  # type: ignore[arg-type]

    def dm(mid: str, text: str) -> dict[str, Any]:
        payload = event(mid, direct=True)
        payload["event"]["message"].update(message_type="text", content=json.dumps({"text": text}))
        return payload

    old = asyncio.create_task(adapter.handle(dm("om_old", "old task")))
    try:
        await asyncio.wait_for(old_started.wait(), 3)
        await asyncio.wait_for(adapter.handle(dm("om_new", "/new")), 3)
        assert not old.done()
        assert runtime.threads == [thread_id_for("tenant", "oc_chat", "dm")]
        assert len([m for m in fake.sent if m["msg_type"] == "post"]) == 1
        restarted = FeishuAdapter(
            fake.client, "cli_test", "tenant", "ou_bot", runtime=runtime, store=store
        )  # type: ignore[arg-type]
        await restarted.handle(dm("om_new", "/new"))
        assert len([m for m in fake.sent if m["msg_type"] == "post"]) == 1
        await asyncio.wait_for(restarted.handle(dm("om_next", "new task")), 3)
        new_thread = runtime.threads[-1]
        assert new_thread != runtime.threads[0]
        assert target_of(new_thread, "tenant") == ("oc_chat", "dm")
        from design_agent.runtime.sandbox.docker import DockerSandboxRunner

        runner = DockerSandboxRunner(image="test-image")
        assert runner.names_for(new_thread) != runner.names_for(runtime.threads[0])
        # All replies, including the old task's eventual reply, stay in the DM.
        release_old.set()
        await old
        assert all(m.get("receive_id") == "oc_chat" for m in fake.sent)
        assert all("reply_in_thread" not in m for m in fake.sent)
        await restarted.handle(dm("om_again", "/new"))
        await restarted.handle(dm("om_final", "another task"))
        assert runtime.threads[-1] != new_thread
    finally:
        release_old.set()
        await old
        await adapter.aclose()
        fake.client.close()
