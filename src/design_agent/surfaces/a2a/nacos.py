"""Nacos 3.3 HTTP Agent registry, independent of native gRPC SDKs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any
from uuid import uuid4

import httpx
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types.a2a_pb2 import AgentCard

from design_agent.config import Settings
from design_agent.surfaces.a2a.app import public_base

_LOGGER = logging.getLogger(__name__)
AGENTS = "/v3/client/ai/agents"


class NacosError(RuntimeError):
    def __init__(self, code: int) -> None:
        # Response text can contain credentials or infrastructure details; retain only a code.
        self.code = code
        super().__init__(f"Nacos request failed (code {code})")


class NacosRegistry:
    def __init__(
        self,
        settings: Settings,
        card: AgentCard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.nacos_url or not settings.nacos_username or settings.nacos_password is None:
            raise ValueError("Nacos requires URL, username and password")
        if not settings.a2a_url:
            raise ValueError("Nacos registration requires DESIGN_AGENT_A2A_URL")
        origin = public_base(settings.nacos_url)
        self.settings = settings
        self.card = card
        self.client_id = str(uuid4())
        self.client = httpx.AsyncClient(base_url=origin + "/nacos", timeout=15, transport=transport)
        self.token = ""
        self.expires = 0.0
        self.auth_lock = asyncio.Lock()
        self.interval = 5.0
        self.registered = False
        self.last_error: str | None = None
        self._runner: asyncio.Task[None] | None = None

    async def _login(self) -> None:
        async with self.auth_lock:
            if self.token and time.monotonic() < self.expires:
                return
            response = await self.client.post(
                "/v3/auth/user/login",
                data={
                    "username": self.settings.nacos_username or "",
                    "password": self.settings.nacos_password.get_secret_value()
                    if self.settings.nacos_password
                    else "",
                },
            )
            if response.status_code != 200:
                raise NacosError(response.status_code)
            data = response.json()
            if not data.get("accessToken"):
                raise NacosError(int(data.get("code", -1)))
            self.token = data["accessToken"]
            self.expires = time.monotonic() + max(1, float(data.get("tokenTtl", 18000)) * 0.8)

    async def request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        for attempt in range(2):
            await self._login()
            response = await self.client.request(
                method,
                path,
                data=data,
                params=params,
                headers={
                    "accessToken": self.token,
                    "X-Nacos-Client-Id": self.client_id,
                    "Request-Module": "AI",
                },
            )
            body = response.json() if "json" in response.headers.get("content-type", "") else {}
            code = int(body.get("code", response.status_code))
            if (response.status_code in {401, 403} or code in {401, 403}) and attempt == 0:
                self.token = ""
                continue
            if response.is_error or code != 0:
                raise NacosError(code)
            return body.get("data")
        raise NacosError(403)

    def interfaces(self) -> list[dict[str, Any]]:
        return [
            {
                "protocol": "a2a",
                "protocolVersion": "1.0",
                "descriptorMediaType": "application/json",
                "nativeDescriptor": agent_card_to_dict(self.card),
                "endpointSourceOrder": ["RUNTIME", "DECLARED"],
                "endpointSets": [],
            }
        ]

    async def publish(self) -> None:
        # Do not silently overwrite an immutable version or hide an access failure as 'missing'.
        query = {
            "namespaceId": self.settings.nacos_namespace,
            "agentName": self.settings.a2a_name,
            "version": self.settings.a2a_version,
        }
        try:
            existing = await self.request("GET", AGENTS, params=query)
        except NacosError as error:
            if error.code not in {404, 20004, 30004}:
                raise
        else:
            interfaces = existing.get("callInterfaces", [])
            if (
                not interfaces
                or interfaces[0].get("nativeDescriptor") != self.interfaces()[0]["nativeDescriptor"]
            ):
                raise ValueError(
                    "Published Agent version differs; choose a new DESIGN_AGENT_A2A_VERSION"
                )
            return
        published = await self.request(
            "POST",
            AGENTS,
            data={
                **query,
                "callInterfaces": json.dumps(self.interfaces()),
                "author": "design-agent",
                "changeDescription": "A2A interface",
                "autoSubmit": "true",
            },
        )
        if published.get("status") != "online":
            raise ValueError(
                "Nacos definition is awaiting publication; operator review is required"
            )

    async def register(self) -> None:
        info = await self.request(
            "POST",
            AGENTS + "/endpoints",
            data={
                "namespaceId": self.settings.nacos_namespace,
                "agentName": self.settings.a2a_name,
                "protocol": "a2a",
                "runtimeVersion": self.settings.a2a_version,
                "versionRange": f"[{self.settings.a2a_version}]",
                "endpoints": json.dumps(
                    [
                        {
                            "uri": public_base(self.settings.a2a_url or "") + "/a2a",
                            "transport": "JSONRPC",
                            "priority": 0,
                            "weight": 1.0,
                            "metadata": {},
                        }
                    ]
                ),
            },
        )
        self._liveness(info)
        self.registered = True
        self.last_error = None

    def _liveness(self, info: dict[str, Any]) -> None:
        interval = float(info["heartbeatIntervalMillis"]) / 1000
        if interval <= 0:
            raise ValueError("Invalid Nacos heartbeat interval")
        self.interval = interval

    async def heartbeat(self) -> None:
        try:
            self._liveness(await self.request("PUT", AGENTS + "/endpoints/heartbeat"))
        except NacosError as error:
            if error.code != 50404:
                raise
            await self.register()
        self.registered = True
        self.last_error = None

    async def deregister(self) -> None:
        await self.request(
            "DELETE",
            AGENTS + "/endpoints",
            params={
                "namespaceId": self.settings.nacos_namespace,
                "agentName": self.settings.a2a_name,
                "protocol": "a2a",
            },
        )
        self.registered = False

    def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="nacos-registration")

    async def _run(self) -> None:
        published = False
        retry = 1.0
        while True:
            try:
                # Lifespan starts before uvicorn binds; publish only once the real API is serving.
                host = (
                    "127.0.0.1"
                    if self.settings.api_host in {"0.0.0.0", "::"}  # noqa: S104
                    else self.settings.api_host
                )
                async with httpx.AsyncClient(timeout=3) as probe:
                    response = await probe.get(f"http://{host}:{self.settings.api_port}/health")
                    response.raise_for_status()
                    if not response.json().get("ok"):
                        raise ValueError("Agent not ready")
                if not published:
                    await self.publish()
                    published = True
                if not self.registered:
                    await self.register()
                else:
                    await self.heartbeat()
                retry = 1.0
                await asyncio.sleep(self.interval)
            except Exception as error:  # noqa: BLE001 - a registry outage must recover without killing jobs
                self.registered = False
                self.last_error = (
                    f"NacosError:{error.code}"
                    if isinstance(error, NacosError)
                    else type(error).__name__
                )
                _LOGGER.warning("Nacos registration unavailable: %s", self.last_error)
                await asyncio.sleep(retry)
                retry = min(retry * 2, 30)

    async def aclose(self) -> None:
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        try:
            # A failed heartbeat can leave a publication alive; withdraw even then.
            await self.deregister()
        except (httpx.HTTPError, NacosError):
            _LOGGER.warning(
                "Nacos deregistration failed; endpoint will expire by heartbeat timeout"
            )
        finally:
            await self.client.aclose()
