"""A2A 1.0 through SDK routes, real shared graph, durable state and authenticated callers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from design_agent.config import Settings
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.api.app import create_app
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import RespondingChatModel, ScriptedChatModel, reply
from tests.contract.test_api_contracts import ROOT, A, B, lead_turn


def client(
    tmp_path: Path,
    *,
    anonymous: bool = False,
    tokens: str | None = "token-alice-0123456789:alice,token-bob-0123456789:bob",
) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path / "ws",
        brand_path=Path("examples/brands/example-studio"),
        access_token_callers=tokens,
        a2a_url="http://testserver",
        a2a_allow_anonymous=anonymous,
    )  # type: ignore[call-arg]

    def build(settings_: Settings, root: Path, **kwargs: Any) -> Any:
        return build_local_runtime(
            settings_,
            root,
            model=RespondingChatModel(respond=lead_turn),
            role_models={"low": ScriptedChatModel(steps=[lambda _m: reply("Three frames saved.")])},
            binding=folder_binding(tmp_path / "sb", ROOT),
            **kwargs,
        )

    return TestClient(create_app(ROOT, settings, build=build))


def rpc(
    api: TestClient, method: str, params: dict[str, Any], headers: dict[str, str] = A
) -> dict[str, Any]:
    response = api.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": uuid4().hex, "method": method, "params": params},
        headers={**headers, "A2A-Version": "1.0"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def message(text: str = "hello", context_id: str = "shared") -> dict[str, Any]:
    return {
        "message": {
            "messageId": uuid4().hex,
            "role": "ROLE_USER",
            "contextId": context_id,
            "parts": [{"text": text}],
        }
    }


def task_of(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response
    return response["result"]["task"]


@pytest.mark.parametrize("tokens", [None, "token-alice-0123456789:alice"])
def test_anonymous_messages_tasks_stream_and_files(tmp_path: Path, tokens: str | None) -> None:
    assert Settings(_env_file=None).a2a_allow_anonymous is True  # type: ignore[call-arg]
    with client(tmp_path, anonymous=True, tokens=tokens) as api:
        card = api.get("/.well-known/agent-card.json").json()
        assert not card.get("securitySchemes")
        assert not card.get("securityRequirements")
        made = task_of(rpc(api, "SendMessage", message("Make the card."), headers={}))
        assert made["status"]["state"] == "TASK_STATE_COMPLETED"
        file = next(p for a in made["artifacts"] for p in a["parts"] if "url" in p)
        assert api.get(file["url"]).content == b"card"
        # Anonymous mode always has one identity, even if the client sends an old token.
        assert rpc(api, "GetTask", {"id": made["id"]}, headers=B)["result"]["id"] == made["id"]
        assert rpc(api, "ListTasks", {}, headers={})["result"]["tasks"]
        streamed = api.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage", "params": message()},
            headers={"A2A-Version": "1.0"},
        )
        assert "TASK_STATE_COMPLETED" in streamed.text
        assert api.post("/threads/plain/messages", json={"text": "hello"}).status_code == 401
        assert api.get("/threads/plain/files/card.txt").status_code == 401
        assert api.get("/threads/a2a-missing/files/card.txt").status_code == 404


def test_anonymous_mode_keeps_previous_authenticated_tasks_private(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        private = task_of(rpc(api, "SendMessage", message("Make the card.")))
        file = next(p for a in private["artifacts"] for p in a["parts"] if "url" in p)
    with client(tmp_path, anonymous=True) as api:
        assert "error" in rpc(api, "GetTask", {"id": private["id"]}, headers={})
        assert api.get(file["url"]).status_code == 404
        assert api.get(file["url"], headers=A).status_code == 404


def test_authenticated_a2a_requires_configured_tokens(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="DESIGN_AGENT_ACCESS_TOKEN_CALLERS"):
        client(tmp_path, anonymous=False, tokens=None)


def test_card_auth_real_graph_files_and_caller_isolation(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        card = api.get("/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
        assert api.post("/a2a", json={}).status_code == 401
        made = task_of(rpc(api, "SendMessage", message("Make the card.")))
        assert made["status"]["state"] == "TASK_STATE_COMPLETED"
        artifacts = made["artifacts"]
        file = next(p for a in artifacts for p in a["parts"] if "url" in p)
        assert api.get(file["url"], headers=A).content == b"card"
        assert api.get(file["url"], headers=B).status_code == 404
        assert "error" in rpc(api, "GetTask", {"id": made["id"]}, headers=B)
        assert rpc(api, "ListTasks", {}, headers=B)["result"]["tasks"] == []
        # Same caller-supplied context ID never grants another caller the conversation.
        other = task_of(rpc(api, "SendMessage", message(), headers=B))
        assert other["id"] != made["id"]
        assert "error" not in rpc(api, "GetTask", {"id": made["id"]})
        assert "error" in rpc(api, "CancelTask", {"id": made["id"]})
        assert (
            task_of(
                rpc(
                    api,
                    "SendMessage",
                    {
                        **message(),
                        "configuration": {
                            "taskPushNotificationConfig": {"url": "http://localhost/callback"}
                        },
                    },
                )
            )["status"]["state"]
            == "TASK_STATE_REJECTED"
        )
    # New app/runtime process reads the same persisted A2A tasks.
    with client(tmp_path) as api:
        fetched = rpc(api, "GetTask", {"id": made["id"]})
        assert fetched["result"]["id"] == made["id"]
        assert fetched["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_stream_and_nonblocking_tasks(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        response = api.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage", "params": message()},
            headers={**A, "A2A-Version": "1.0"},
        )
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "TASK_STATE_WORKING" in response.text
        assert "TASK_STATE_COMPLETED" in response.text
        started = task_of(
            rpc(
                api,
                "SendMessage",
                {**message(context_id="async"), "configuration": {"returnImmediately": True}},
            )
        )
        for _ in range(600):
            fetched = rpc(api, "GetTask", {"id": started["id"]})["result"]
            if fetched["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            time.sleep(0.05)
        assert fetched["status"]["state"] == "TASK_STATE_COMPLETED"


def test_reject_unsupported_parts_and_hide_failure_details(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        params = message()
        params["message"]["parts"] = [{"url": "http://localhost/private", "mediaType": "image/png"}]
        rejected = task_of(rpc(api, "SendMessage", params))
        assert rejected["status"]["state"] == "TASK_STATE_REJECTED"
        runtime = api.app.state.api.runtime  # type: ignore[union-attr]

        async def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("private-provider-key")

        runtime.ask = fail
        failed = rpc(api, "SendMessage", message())
        assert task_of(failed)["status"]["state"] == "TASK_STATE_FAILED"
        assert "private-provider-key" not in str(failed)


def test_restart_marks_incomplete_tasks_failed(tmp_path: Path) -> None:
    from a2a.server.context import ServerCallContext
    from a2a.types.a2a_pb2 import Task, TaskState, TaskStatus

    from design_agent.surfaces.a2a.app import Caller

    with client(tmp_path) as api:
        store = api.app.state.a2a_store  # type: ignore[union-attr]
        context = ServerCallContext(user=Caller("alice"))
        api.portal.call(
            store.save,
            Task(
                id="interrupted",
                context_id="ctx",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            ),
            context,
        )
    with client(tmp_path) as api:
        task = rpc(api, "GetTask", {"id": "interrupted"})["result"]
        assert task["status"]["state"] == "TASK_STATE_FAILED"


def test_background_helper_finishes_before_a2a_completion(tmp_path: Path, monkeypatch: Any) -> None:
    from design_agent.harness import inbox
    from design_agent.harness.tasks import REGISTRY

    monkeypatch.setattr(inbox, "COALESCE_SECONDS", 0.05)
    try:
        with client(tmp_path) as api:
            result = task_of(rpc(api, "SendMessage", message("Grab three frames.")))
            assert result["status"]["state"] == "TASK_STATE_COMPLETED"
            assert result["artifacts"][0]["parts"][0]["text"] == "The frame grabs are ready."
    finally:
        REGISTRY.clear()


def test_cancel_rejection_keeps_running_task_and_reserved_threads(tmp_path: Path) -> None:
    import asyncio

    from design_agent.surfaces.a2a.app import thread_for

    with client(tmp_path) as api:
        reserved = thread_for("alice", "reserved")
        assert (
            api.post(
                f"/threads/{reserved}/messages", json={"text": "injection"}, headers=B
            ).status_code
            == 404
        )
        runtime = api.app.state.api.runtime  # type: ignore[union-attr]
        original = runtime.ask
        gate = asyncio.Event()

        async def blocked(*args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return await original(*args, **kwargs)

        runtime.ask = blocked
        running = task_of(
            rpc(
                api,
                "SendMessage",
                {**message(context_id="reserved"), "configuration": {"returnImmediately": True}},
            )
        )
        assert "error" in rpc(api, "CancelTask", {"id": running["id"]})
        assert rpc(api, "GetTask", {"id": running["id"]})["result"]["status"]["state"] in {
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
        }
        api.portal.call(gate.set)
        for _ in range(600):
            fetched = rpc(api, "GetTask", {"id": running["id"]})["result"]
            if fetched["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            time.sleep(0.05)
        assert fetched["status"]["state"] == "TASK_STATE_COMPLETED"


def test_tool_progress_reaches_task_store_before_execution_finishes(tmp_path: Path) -> None:
    import asyncio

    from design_agent.surfaces.a2a.progress import PROGRESS_KEY

    with client(tmp_path) as api:
        runtime = api.app.state.api.runtime  # type: ignore[union-attr]
        original = runtime.ask
        gate = asyncio.Event()

        async def delayed(*args: Any, **kwargs: Any) -> Any:
            added = await original(*args, **kwargs)
            await gate.wait()
            return added

        runtime.ask = delayed
        running = task_of(
            rpc(
                api,
                "SendMessage",
                {
                    **message("Make the card."),
                    "configuration": {"returnImmediately": True},
                },
            )
        )
        try:
            for _ in range(600):
                task = rpc(api, "GetTask", {"id": running["id"]})["result"]
                progress = (
                    task.get("status", {}).get("message", {}).get("metadata", {}).get(PROGRESS_KEY)
                )
                if progress and progress["state"] == "completed":
                    break
                time.sleep(0.05)
            assert task["status"]["state"] == "TASK_STATE_WORKING"
            assert progress["tool"]
            assert progress["stepId"]
            assert progress["elapsedSeconds"] >= 0
            assert set(progress) == {"tool", "stepId", "state", "elapsedSeconds"}
            assert "error" in rpc(api, "GetTask", {"id": running["id"]}, headers=B)
        finally:
            api.portal.call(gate.set)
        for _ in range(600):
            task = rpc(api, "GetTask", {"id": running["id"]})["result"]
            if task["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            time.sleep(0.05)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert not api.app.state.a2a_progress  # type: ignore[union-attr]
