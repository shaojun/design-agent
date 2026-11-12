"""Nacos HTTP lifecycle with exact form requests, refresh, re-registration and outage recovery."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from design_agent.config import Settings
from design_agent.surfaces.a2a.app import agent_card, public_base
from design_agent.surfaces.a2a.nacos import AGENTS, NacosError, NacosRegistry


def settings() -> Settings:
    return Settings(
        _env_file=None,
        a2a_url="http://agent.example:8080",
        nacos_url="https://registry.example",
        nacos_username="application",
        nacos_password="test-only-password",
    )  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "url", ["http://u:password@host", "http://host/path", "ftp://host", "http://host?x=1"]
)
def test_urls_reject_credentials_and_paths(url: str) -> None:
    with pytest.raises(ValueError):
        public_base(url)


async def test_publish_register_heartbeat_refresh_expiry_and_deregister() -> None:
    calls: list[httpx.Request] = []
    logins = 0
    heartbeat = 0
    registers = 0
    interfaces: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal logins, heartbeat, registers, interfaces
        calls.append(request)
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if path.endswith("/login"):
            logins += 1
            assert form["username"] == "application"
            return httpx.Response(200, json={"accessToken": f"token-{logins}", "tokenTtl": 60})
        assert request.headers["accessToken"] == f"token-{logins}"
        assert request.headers["Request-Module"] == "AI"
        if path == "/nacos" + AGENTS:
            if request.method == "GET":
                # Discovery is a resolved view: the live 3.3 server omits source-order policy.
                discovered = [
                    {k: v for k, v in i.items() if k != "endpointSourceOrder"} for i in interfaces
                ]
                return (
                    httpx.Response(200, json={"code": 0, "data": {"callInterfaces": discovered}})
                    if interfaces
                    else httpx.Response(404, json={"code": 404})
                )
            interfaces = json.loads(form["callInterfaces"])
            assert interfaces[0]["endpointSourceOrder"] == ["RUNTIME", "DECLARED"]
            assert (
                interfaces[0]["nativeDescriptor"]["supportedInterfaces"][0]["protocolVersion"]
                == "1.0"
            )
            return httpx.Response(200, json={"code": 0, "data": {"status": "online"}})
        if path.endswith("/heartbeat"):
            heartbeat += 1
            if heartbeat == 1:
                return httpx.Response(403, json={"code": 403})
            if heartbeat == 3:
                return httpx.Response(200, json={"code": 50404})
        if request.method == "DELETE":
            assert request.url.params["agentName"] == "design-agent"
            return httpx.Response(200, json={"code": 0, "data": None})
        if request.method == "POST":
            registers += 1
            assert form["protocol"] == "a2a"
            endpoint = json.loads(form["endpoints"])[0]
            assert endpoint["uri"] == "http://agent.example:8080/a2a"
            assert endpoint["transport"] == "JSONRPC"
        return httpx.Response(200, json={"code": 0, "data": {"heartbeatIntervalMillis": 7123}})

    config = settings()
    registry = NacosRegistry(config, agent_card(config), transport=httpx.MockTransport(handle))
    await registry.publish()
    await registry.publish()  # Existing immutable definition is reused, not republished.
    await registry.register()
    assert registry.interval == 7.123
    await registry.heartbeat()
    assert logins == 2
    await registry.heartbeat()
    assert registers == 2  # Server lost its client; restore the complete desired publication.
    await registry.aclose()
    assert not registry.registered
    assert (
        len({r.headers["X-Nacos-Client-Id"] for r in calls if not r.url.path.endswith("/login")})
        == 1
    )
    assert len([r for r in calls if r.method == "POST" and r.url.path == "/nacos" + AGENTS]) == 1


async def test_permission_error_is_not_treated_as_missing_definition() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"accessToken": "test", "tokenTtl": 60})
        assert request.method == "GET"
        return httpx.Response(403, json={"code": 403, "message": "private diagnostic"})

    config = settings()
    registry = NacosRegistry(config, agent_card(config), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(NacosError) as error:
            await registry.publish()
        assert "private diagnostic" not in str(error.value)
    finally:
        await registry.client.aclose()
