"""Consumer demo: discovery pagination, destinations, credential and browser isolation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from a2a.utils.errors import A2AError
from fastapi import HTTPException

_spec = importlib.util.spec_from_file_location(
    "arc_web", Path(__file__).parents[2] / "scripts/agent-registration-center/app.py"
)
assert _spec and _spec.loader
web = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = web
_spec.loader.exec_module(web)


def definition() -> dict:
    return {
        "callInterfaces": [
            {
                "protocol": "a2a",
                "protocolVersion": "1.0",
                "nativeDescriptor": {
                    "name": "test-agent",
                    "supportedInterfaces": [{"url": "https://stale.example/a2a"}],
                },
                "endpointSets": [
                    {
                        "source": "RUNTIME",
                        "endpoints": [
                            {
                                "uri": "https://live.example/custom/a2a",
                                "healthy": True,
                                "enabled": True,
                                "transport": "JSONRPC",
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_only_healthy_discovered_endpoint_receives_credentials() -> None:
    data = definition()
    card = web.selected_card(data, "https://live.example/custom/a2a")
    assert [i.url for i in card.supported_interfaces] == ["https://live.example/custom/a2a"]
    with pytest.raises(HTTPException, match="409"):
        web.selected_card(data, "https://stale.example/a2a")
    data["callInterfaces"][0]["endpointSets"][0]["endpoints"][0]["healthy"] = False
    with pytest.raises(HTTPException, match="409"):
        web.selected_card(data, "https://live.example/custom/a2a")


async def test_catalog_reads_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def get(self, path, **params):
        calls.append(params["pageNo"])
        return {"pageItems": [{"agentName": f"agent-{params['pageNo']}"}], "totalCount": 2}

    monkeypatch.setattr(web.Registry, "get", get)
    registry = web.Registry("https://registry.example", "public", "test-registry-token", 100)
    assert len(await registry.catalog()) == 2
    assert calls == [1, 2]


async def test_browser_sessions_headers_and_secret_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    outbound = []

    def upstream(request):
        outbound.append(request)
        if request.url.path.endswith("/login"):
            assert request.headers.get("authorization") is None
            return httpx.Response(200, json={"accessToken": "test-nacos-access", "tokenTtl": 100})
        assert request.headers["accessToken"] == "test-nacos-access"
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"code": 0, "data": {"pageItems": [], "totalCount": 0}})
        return httpx.Response(200, json={"code": 0, "data": definition()})

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", client_factory)
    app = web.create_app()
    async with real_client(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as browser:
        assert (await browser.get("/api/agents")).status_code == 401
        assert (await browser.post("/api/connect", json={})).status_code == 403
        headers = {"X-ARC-Request": "1"}
        assert (
            await browser.post(
                "/api/connect", headers={**headers, "Origin": "https://foreign.example"}, json={}
            )
        ).status_code == 403
        response = await browser.post(
            "/api/connect",
            headers=headers,
            json={
                "url": "https://registry.example",
                "username": "reader",
                "password": "test-password",
            },
        )
        assert response.status_code == 200
        assert "test-nacos-access" not in response.text
        assert "httponly" in response.headers["set-cookie"].lower()
        assert (await browser.get("/api/agents")).status_code == 200
        async with real_client(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as other:
            assert (await other.get("/api/agents")).status_code == 401
        response = await browser.post(
            "/api/action",
            headers=headers,
            json={
                "agent": "test-agent",
                "version": "1",
                "endpoint": "https://foreign.example/a2a",
                "token": "test-agent-token",
                "action": "GetTask",
                "params": {"id": "task-1"},
            },
        )
        assert response.status_code == 409
        assert all(r.url.host == "registry.example" for r in outbound)
        assert "test-agent-token" not in " ".join(
            str(r.headers) + r.content.decode() for r in outbound
        )
        await browser.post("/api/disconnect", headers=headers)
        assert (await browser.get("/api/agents")).status_code == 401
    async with real_client(
        transport=httpx.ASGITransport(app), base_url="http://foreign.example"
    ) as foreign:
        assert (await foreign.get("/api/config")).status_code == 403


async def test_saved_credentials_are_not_sent_to_another_registry() -> None:
    app = web.create_app(
        {"nacos_username": "reader", "nacos_password": "test-password", "token": "test-agent-token"}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/config")).json()["saved_available"] is True
        response = await client.post(
            "/api/connect",
            headers={"X-ARC-Request": "1"},
            json={"url": "https://other.example", "saved": True},
        )
        assert response.status_code == 400
        assert "test-password" not in response.text


@pytest.mark.parametrize("action", list(web.OPERATIONS))
async def test_all_protocol_actions_use_official_client_and_discovered_uri(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test actual SDK JSONRPC encoding/SSE decoding, not a mocked SDK method."""
    import base64
    import json

    real_client = httpx.AsyncClient
    data = definition()
    data["callInterfaces"][0]["nativeDescriptor"]["capabilities"] = {
        "streaming": True,
        "pushNotifications": True,
        "extendedAgentCard": True,
    }
    task = {
        "id": "task-1",
        "contextId": "context-1",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"artifactId": "artifact-1", "parts": [{"text": "hello"}]}],
    }
    push = {"taskId": "task-1", "id": "config-1", "url": "https://callback.example/events"}
    seen = []

    def upstream(request):
        if request.url.host == "registry.example":
            if request.url.path.endswith("/login"):
                return httpx.Response(
                    200, json={"accessToken": "test-registry-token", "tokenTtl": 100}
                )
            assert request.headers["accessToken"] == "test-registry-token"
            content = (
                {"pageItems": [], "totalCount": 0} if request.url.path.endswith("/search") else data
            )
            return httpx.Response(200, json={"code": 0, "data": content})
        assert str(request.url) == "https://live.example/custom/a2a"
        assert request.headers["authorization"] == "Bearer test-agent-token"
        assert request.headers.get("accessToken") is None
        assert request.headers["a2a-version"] == "1.0"
        payload = json.loads(request.content)
        seen.append(payload)
        method = payload["method"]
        if method in {"SendStreamingMessage", "SubscribeToTask"}:
            stream = [
                {"task": task},
                {
                    "artifactUpdate": {
                        "taskId": "task-1",
                        "contextId": "context-1",
                        "artifact": {"artifactId": "artifact-1", "parts": [{"text": " world"}]},
                        "append": True,
                        "lastChunk": True,
                    }
                },
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(
                    "data: "
                    + json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": event})
                    + "\n\n"
                    for event in stream
                ),
            )
        result = {
            "SendMessage": {"task": task},
            "GetTask": task,
            "ListTasks": {"tasks": [task], "nextPageToken": "page-2"},
            "CancelTask": {**task, "status": {"state": "TASK_STATE_CANCELED"}},
            "CreateTaskPushNotificationConfig": push,
            "GetTaskPushNotificationConfig": push,
            "ListTaskPushNotificationConfigs": {"configs": [push], "nextPageToken": "push-page-2"},
            "DeleteTaskPushNotificationConfig": {},
            "GetExtendedAgentCard": data["callInterfaces"][0]["nativeDescriptor"],
        }[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    params = {}
    if action.startswith("Send"):
        params = {
            "message": {
                "messageId": "message-1",
                "role": "ROLE_USER",
                "parts": [
                    {"text": "hi"},
                    {
                        "raw": base64.b64encode(b"file-bytes").decode(),
                        "filename": "test.txt",
                        "mediaType": "text/plain",
                    },
                    {"data": {"value": 42}},
                ],
            },
            "configuration": {"returnImmediately": True},
            "metadata": {"example": True},
        }
    elif action in {"GetTask", "CancelTask", "SubscribeToTask"}:
        params = {"id": "task-1"}
    elif "PushNotification" in action:
        params = {"taskId": "task-1"}
        if action == "CreateTaskPushNotificationConfig":
            params.update(push)
        elif action != "ListTaskPushNotificationConfigs":
            params["id"] = "config-1"
    async with real_client(
        transport=httpx.ASGITransport(web.create_app()), base_url="http://localhost"
    ) as client:
        headers = {"X-ARC-Request": "1"}
        assert (
            await client.post(
                "/api/connect", headers=headers, json={"url": "https://registry.example"}
            )
        ).status_code == 200
        response = await client.post(
            "/api/action",
            headers=headers,
            json={
                "agent": "test-agent",
                "version": "1",
                "endpoint": "https://live.example/custom/a2a",
                "token": "test-agent-token",
                "action": action,
                "params": params,
            },
        )
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert not any(e["type"] == "error" for e in events), events
        assert events[-1] == {"type": "done"}
        assert seen[0]["method"] == action
        if action.startswith("Send"):
            assert (
                seen[0]["params"]["message"]["parts"][1]["raw"]
                == base64.b64encode(b"file-bytes").decode()
            )
            assert seen[0]["params"]["metadata"] == {"example": True}
        if action in {"SendStreamingMessage", "SubscribeToTask"}:
            assert any(e.get("data", {}).get("artifactUpdate", {}).get("append") for e in events)


def test_capability_and_schema_errors_are_explicit() -> None:
    from a2a.types import AgentCard

    card = AgentCard()
    for action in [
        "SendStreamingMessage",
        "SubscribeToTask",
        "CreateTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    ]:
        with pytest.raises(HTTPException, match="409"):
            web.operation_request(
                web.Operation(
                    agent="a", version="1", endpoint="https://a.example/a2a", action=action
                ),
                card,
            )
    with pytest.raises(HTTPException, match="400"):
        web.operation_request(
            web.Operation(
                agent="a",
                version="1",
                endpoint="https://a.example/a2a",
                action="GetTask",
                params={"invalidField": "bad"},
            ),
            card,
        )
    with pytest.raises(HTTPException, match="400"):
        web.operation_request(
            web.Operation(
                agent="a",
                version="1",
                endpoint="https://a.example/a2a",
                action="SendMessage",
                params={},
            ),
            card,
        )


async def test_download_checks_task_ownership_origin_and_file_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    real_client = httpx.AsyncClient
    seen_files = []
    url = "https://live.example/files/result.txt"

    def upstream(request):
        if request.url.host == "registry.example":
            if request.url.path.endswith("/login"):
                return httpx.Response(
                    200, json={"accessToken": "test-registry-token", "tokenTtl": 100}
                )
            data = (
                {"pageItems": [], "totalCount": 0}
                if request.url.path.endswith("/search")
                else definition()
            )
            return httpx.Response(200, json={"code": 0, "data": data})
        assert request.headers["authorization"] == "Bearer test-agent-token"
        if request.method == "POST":
            payload = json.loads(request.content)
            task = {
                "id": payload["params"]["id"],
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"artifactId": "a", "parts": [{"url": url, "filename": "result.txt"}]}
                ],
            }
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": task})
        seen_files.append(str(request.url))
        return httpx.Response(200, content=b"FILE_OK")

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    async with real_client(
        transport=httpx.ASGITransport(web.create_app()), base_url="http://localhost"
    ) as client:
        headers = {"X-ARC-Request": "1"}
        await client.post("/api/connect", headers=headers, json={"url": "https://registry.example"})
        body = {
            "agent": "test-agent",
            "version": "1",
            "endpoint": "https://live.example/custom/a2a",
            "token": "test-agent-token",
            "task_id": "task-1",
            "url": url,
        }
        response = await client.post("/api/download", headers=headers, json=body)
        assert response.status_code == 200
        assert response.content == b"FILE_OK"
        assert "attachment" in response.headers["content-disposition"]
        assert (
            await client.post(
                "/api/download",
                headers=headers,
                json={**body, "url": "https://live.example/private.txt"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/download",
                headers=headers,
                json={**body, "url": "https://foreign.example/result.txt"},
            )
        ).status_code == 400
        assert seen_files == [url]


@pytest.mark.parametrize(
    ("upstream_status", "upstream_body", "expected_status"),
    [
        (403, "user not found", 401),
        (401, "unauthorized", 401),
        (502, "<html>Bad gateway</html>", 502),
        (200, "<html>Unexpected proxy page</html>", 502),
        (200, "[]", 502),
    ],
)
async def test_login_handles_nacos_non_json_errors(
    upstream_status: int, upstream_body: str, expected_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(upstream_status, text=upstream_body))
    monkeypatch.setattr(
        web.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    async with real_client(
        transport=httpx.ASGITransport(web.create_app(), raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.post(
            "/api/connect",
            headers={"X-ARC-Request": "1"},
            json={"username": "invalid-reader", "password": "test-private-password"},
        )
        assert response.status_code == expected_status
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()
        assert upstream_body not in response.text
        assert "test-private-password" not in response.text


async def test_catalog_handles_non_json_permission_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient

    def upstream(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"accessToken": "test-registry-token", "tokenTtl": 100})
        return httpx.Response(403, text="permission denied")

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    async with real_client(
        transport=httpx.ASGITransport(web.create_app(), raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.post(
            "/api/connect",
            headers={"X-ARC-Request": "1"},
            json={"username": "reader", "password": "test-password"},
        )
        assert response.status_code == 403
        assert "权限" in response.json()["detail"]


def test_protocol_catalog_comes_from_sdk_descriptor() -> None:
    from a2a.types import a2a_pb2

    catalog = web.protocol_catalog()
    assert {item["name"] for item in catalog} == {
        method.name for method in a2a_pb2.DESCRIPTOR.services_by_name["A2AService"].methods
    }
    assert len(catalog) == 11
    stream = next(item for item in catalog if item["name"] == "SendStreamingMessage")
    assert stream["capability"] == "streaming"
    assert "message" in stream["fields"]
    assert next(item for item in catalog if item["name"] == "CancelTask")["capability"] is None


@pytest.mark.parametrize(
    "action",
    [
        "SendStreamingMessage",
        "SubscribeToTask",
        "CreateTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    ],
)
async def test_force_sends_unadvertised_operation_without_mutating_card(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from a2a.types import AgentCard
    from google.protobuf.json_format import ParseDict

    real_client = httpx.AsyncClient
    card = ParseDict(definition()["callInterfaces"][0]["nativeDescriptor"], AgentCard())
    seen = []

    def upstream(request):
        payload = json.loads(request.content)
        seen.append(payload["method"])
        error = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "error": {"code": -32004, "message": "not supported"},
        }
        if action in {"SendStreamingMessage", "SubscribeToTask"}:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(error) + "\n\n",
            )
        return httpx.Response(200, json=error)

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    params = (
        {"message": {"messageId": "test", "role": "ROLE_USER", "parts": [{"text": "hello"}]}}
        if action.startswith("Send")
        else {}
    )
    body = web.Operation(
        agent="test",
        version="1",
        endpoint="https://live.example/custom/a2a",
        action=action,
        params=params,
        force=True,
    )
    request = web.operation_request(body, card)
    try:
        events = [event async for event in web.invoke(card, body, "test-agent-token", request)]
        assert any(event["type"] == "error" for event in events)
    except A2AError:
        pass
    assert seen == [action]
    assert not card.capabilities.streaming
    assert not card.capabilities.push_notifications
    assert not card.capabilities.extended_agent_card


async def test_required_agent_token_is_explained_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    data = definition()
    data["callInterfaces"][0]["nativeDescriptor"].update(
        {
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
            "securityRequirements": [{"schemes": {"bearer": {}}}],
        }
    )
    destinations = []

    def upstream(request):
        destinations.append(request.url.host)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"accessToken": "test-registry-token", "tokenTtl": 100})
        content = (
            {"pageItems": [], "totalCount": 0} if request.url.path.endswith("/search") else data
        )
        return httpx.Response(200, json={"code": 0, "data": content})

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    async with real_client(
        transport=httpx.ASGITransport(web.create_app()), base_url="http://localhost"
    ) as client:
        headers = {"X-ARC-Request": "1"}
        await client.post("/api/connect", headers=headers, json={"url": "https://registry.example"})
        response = await client.post(
            "/api/action",
            headers=headers,
            json={
                "agent": "test",
                "version": "1",
                "endpoint": "https://live.example/custom/a2a",
                "action": "SendMessage",
                "force": True,
                "params": {},
            },
        )
        assert response.status_code == 400
        assert "Bearer" in response.json()["detail"]
        assert "Nacos" in response.json()["detail"]
        assert set(destinations) == {"registry.example"}


@pytest.mark.parametrize("status", [401, 403])
async def test_agent_auth_failures_provide_safe_structured_feedback(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from a2a.types import AgentCard
    from google.protobuf.json_format import ParseDict

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="private diagnostic test-agent-token")
            ),
            **kwargs,
        ),
    )
    card = ParseDict(definition()["callInterfaces"][0]["nativeDescriptor"], AgentCard())
    body = web.Operation(
        agent="test",
        version="1",
        endpoint="https://live.example/custom/a2a",
        action="SendMessage",
        params={"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hello"}]}},
    )
    with pytest.raises(web.AgentAccessError) as error:
        async for _ in web.invoke(
            card, body, "test-agent-token", web.operation_request(body, card)
        ):
            pass
    feedback = web.safe_error(error.value)
    assert feedback["code"] == "agent_auth"
    assert feedback["http_status"] == status
    assert str(status) in feedback["message"]
    assert "部署者" in feedback["message"]
    assert "test-agent-token" not in str(feedback)
    assert "private diagnostic" not in str(feedback)
