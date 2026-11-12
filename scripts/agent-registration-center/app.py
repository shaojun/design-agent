"""Local Nacos discovery + official A2A client demo. Run: uv run python scripts/agent-registration-center/app.py."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
import uvicorn
from a2a import types as a2a_types
from a2a.client import ClientConfig, ClientFactory
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import AgentCard, AgentInterface
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from google.protobuf.message import Message as ProtoMessage
from pydantic import BaseModel, Field, SecretStr

DEFAULT_NACOS = "https://arc.meerkatai.cn"
COOKIE = "arc_session"


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(400, "地址须为 http(s) origin，不附加 /nacos、路径或凭证")
    return value.rstrip("/")


def nacos_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        raise HTTPException(
            502, f"Nacos 返回非 JSON 响应（HTTP {response.status_code}），请检查实例地址或反向代理"
        ) from error
    if not isinstance(data, dict):
        raise HTTPException(502, "Nacos 响应格式无效，应为 JSON 对象")
    return data


class Login(BaseModel):
    url: str = DEFAULT_NACOS
    namespace: str = Field(default="public", min_length=1, max_length=200)
    username: str = ""
    password: SecretStr = SecretStr("")
    saved: bool = False


class Target(BaseModel):
    agent: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    endpoint: str
    token: SecretStr = SecretStr("")
    saved: bool = False


class Operation(Target):
    action: Literal[
        "SendMessage",
        "SendStreamingMessage",
        "GetTask",
        "ListTasks",
        "CancelTask",
        "SubscribeToTask",
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


class Download(Target):
    task_id: str
    url: str


# Public SDK request models cover every core A2A 1.0 JSONRPC operation.
OPERATIONS = {
    "SendMessage": ("send_message", a2a_types.SendMessageRequest),
    "SendStreamingMessage": ("send_message", a2a_types.SendMessageRequest),
    "GetTask": ("get_task", a2a_types.GetTaskRequest),
    "ListTasks": ("list_tasks", a2a_types.ListTasksRequest),
    "CancelTask": ("cancel_task", a2a_types.CancelTaskRequest),
    "SubscribeToTask": ("subscribe", a2a_types.SubscribeToTaskRequest),
    "CreateTaskPushNotificationConfig": (
        "create_task_push_notification_config",
        a2a_types.TaskPushNotificationConfig,
    ),
    "GetTaskPushNotificationConfig": (
        "get_task_push_notification_config",
        a2a_types.GetTaskPushNotificationConfigRequest,
    ),
    "ListTaskPushNotificationConfigs": (
        "list_task_push_notification_configs",
        a2a_types.ListTaskPushNotificationConfigsRequest,
    ),
    "DeleteTaskPushNotificationConfig": (
        "delete_task_push_notification_config",
        a2a_types.DeleteTaskPushNotificationConfigRequest,
    ),
    "GetExtendedAgentCard": ("get_extended_agent_card", a2a_types.GetExtendedAgentCardRequest),
}


CAPABILITY_FIELDS = {
    "SendStreamingMessage": "streaming",
    "SubscribeToTask": "streaming",
    "CreateTaskPushNotificationConfig": "pushNotifications",
    "GetTaskPushNotificationConfig": "pushNotifications",
    "ListTaskPushNotificationConfigs": "pushNotifications",
    "DeleteTaskPushNotificationConfig": "pushNotifications",
    "GetExtendedAgentCard": "extendedAgentCard",
}
OPERATION_DESCRIPTIONS = {
    "SendMessage": "发送消息；可等待回复或立即返回后台任务",
    "SendStreamingMessage": "发送消息并接收 SSE 任务与结果增量",
    "GetTask": "按 Task ID 获取状态、历史与结果",
    "ListTasks": "筛选、分页查询当前身份可访问的任务",
    "CancelTask": "请求取消任务；能否取消由服务和任务状态决定",
    "SubscribeToTask": "订阅已有任务，或在断线后重新接收更新",
    "CreateTaskPushNotificationConfig": "为任务创建远端回调配置",
    "GetTaskPushNotificationConfig": "获取指定回调配置",
    "ListTaskPushNotificationConfigs": "分页列出任务回调配置",
    "DeleteTaskPushNotificationConfig": "删除指定回调配置",
    "GetExtendedAgentCard": "鉴权后获取扩展 AgentCard",
}


def protocol_catalog() -> list[dict[str, Any]]:
    service = a2a_types.a2a_pb2.DESCRIPTOR.services_by_name["A2AService"]
    return [
        {
            "name": method.name,
            "description": OPERATION_DESCRIPTIONS[method.name],
            "capability": CAPABILITY_FIELDS.get(method.name),
            "requestType": method.input_type.name,
            "responseType": method.output_type.name,
            "streaming": method.server_streaming,
            "fields": [field.json_name for field in method.input_type.fields],
        }
        for method in service.methods
        if method.name in OPERATIONS
    ]


def requires_bearer(card: AgentCard) -> bool:
    names = {
        name
        for name, scheme in card.security_schemes.items()
        if scheme.HasField("http_auth_security_scheme")
        and scheme.http_auth_security_scheme.scheme.lower() == "bearer"
    }
    return bool(card.security_requirements) and all(
        any(name in names for name in requirement.schemes)
        for requirement in card.security_requirements
    )


def operation_request(body: Operation, card: AgentCard) -> ProtoMessage:
    if (
        body.action in {"SendStreamingMessage", "SubscribeToTask"}
        and not card.capabilities.streaming
        and not body.force
    ):
        raise HTTPException(409, "此 Agent 未声明 streaming 支持")
    if (
        "PushNotification" in body.action
        and not card.capabilities.push_notifications
        and not body.force
    ):
        raise HTTPException(409, "此 Agent 未声明 Push notification 支持")
    if (
        body.action == "GetExtendedAgentCard"
        and not card.capabilities.extended_agent_card
        and not body.force
    ):
        raise HTTPException(409, "此 Agent 未声明扩展 Card 支持")
    try:
        result = ParseDict(body.params, OPERATIONS[body.action][1]())
    except (ParseError, ValueError, TypeError) as error:
        raise HTTPException(400, "请求参数不符合 A2A 1.0 schema，请检查字段名与类型") from error
    if body.action.startswith("Send"):
        if not result.message.parts:
            raise HTTPException(400, "消息至少需要一个 Part")
        if (
            result.configuration.HasField("task_push_notification_config")
            and not card.capabilities.push_notifications
            and not body.force
        ):
            raise HTTPException(409, "此 Agent 不支持随消息设置 Push notification")
        total = sum(len(part.raw) for part in result.message.parts)
        if total > 8 * 1024 * 1024:
            raise HTTPException(413, "单条消息文件总量不能超过 8 MiB")
    return result


class Registry:
    def __init__(self, url: str, namespace: str, token: str, ttl: float) -> None:
        self.url, self.namespace, self.token = url, namespace, token
        self.expires = time.monotonic() + min(ttl, 8 * 3600)

    async def get(self, path: str, **params: Any) -> Any:
        if time.monotonic() >= self.expires:
            raise HTTPException(401, "Nacos 登录已过期，请重新连接")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                self.url + "/nacos/v3/client/ai/agents" + path,
                params={"namespaceId": self.namespace, **params},
                headers={"accessToken": self.token, "Request-Module": "AI"},
            )
        if response.status_code == 401:
            raise HTTPException(401, "Nacos 登录已失效，请重新连接")
        if response.status_code == 403:
            raise HTTPException(
                403, "Nacos 拒绝目录访问（HTTP 403），请检查账号在此 namespace 的 Agent 读取权限"
            )
        if response.is_error:
            raise HTTPException(
                502, f"Nacos 目录接口返回 HTTP {response.status_code}，请检查实例地址和服务状态"
            )
        data = nacos_json(response)
        if data.get("code") != 0:
            raise HTTPException(
                502, f"Nacos 请求失败（HTTP {response.status_code} / code {data.get('code')}）"
            )
        if not isinstance(data.get("data"), dict):
            raise HTTPException(502, "Nacos 目录响应缺少有效的 data 对象")
        return data["data"]

    async def catalog(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 1001):
            data = await self.get("/search", pageNo=page, pageSize=100)
            batch = data.get("pageItems", [])
            items.extend(batch)
            if len(items) >= data["totalCount"]:
                return items
            if not batch:
                break
        raise HTTPException(502, "Nacos 目录分页不完整，请刷新重试")

    async def detail(self, name: str, version: str) -> dict[str, Any]:
        return await self.get("", agentName=name, version=version)


def endpoints(detail: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for interface in detail.get("callInterfaces", []):
        if interface.get("protocol") != "a2a":
            continue
        for group in interface.get("endpointSets", []):
            for ep in group.get("endpoints", []):
                compatible = (
                    interface.get("protocolVersion") == "1.0" and ep.get("transport") == "JSONRPC"
                )
                result.append(
                    {
                        **ep,
                        "source": group["source"],
                        "protocolVersion": interface.get("protocolVersion"),
                        "callable": compatible
                        and ep.get("healthy") is True
                        and ep.get("enabled") is True,
                    }
                )
    return result


def selected_card(detail: dict[str, Any], endpoint: str) -> AgentCard:
    # Re-discover on every call. A browser-supplied URL alone never authorizes a destination.
    if not any(ep["uri"] == endpoint and ep["callable"] for ep in endpoints(detail)):
        raise HTTPException(409, "所选端点已不可用或不支持 A2A 1.0 JSONRPC，请刷新详情")
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(400, "端点包含不允许的凭证或 fragment")
    origin(f"{parsed.scheme}://{parsed.netloc}")
    for interface in detail["callInterfaces"]:
        if interface.get("protocol") != "a2a" or interface.get("protocolVersion") != "1.0":
            continue
        if not any(
            ep["uri"] == endpoint
            for group in interface.get("endpointSets", [])
            for ep in group.get("endpoints", [])
        ):
            continue
        card = ParseDict(interface["nativeDescriptor"], AgentCard())
        # Bind the official client to the discovered endpoint, never a stale/foreign Card URL.
        card.ClearField("supported_interfaces")
        card.supported_interfaces.append(
            AgentInterface(url=endpoint, protocol_binding="JSONRPC", protocol_version="1.0")
        )
        return card
    raise HTTPException(409, "未找到对应的 A2A Card")


class AgentAccessError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Agent HTTP {status}")


async def check_agent_access(response: httpx.Response) -> None:
    if response.status_code in {401, 403}:
        raise AgentAccessError(response.status_code)


async def invoke(
    card: AgentCard, body: Operation, token: str, request: ProtoMessage
) -> AsyncIterator[dict[str, Any]]:
    headers = {"A2A-Version": "1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(1800, connect=15),
        headers=headers,
        follow_redirects=False,
        event_hooks={"response": [check_agent_access]},
    ) as http:
        transport = JsonRpcTransport(http, card, body.endpoint, is_shared_httpx=True)
        if body.force:
            yield {
                "type": "warning",
                "message": "强制测试已启用；客户端将实际发送所选操作，是否支持以远端响应为准。",
            }
        # Public transport methods preserve the requested method even for an unadvertised capability.
        if (
            body.action == "GetExtendedAgentCard"
            and body.force
            and not card.capabilities.extended_agent_card
        ):
            # The SDK convenience method returns its cached Card when this flag is false.
            # Send the explicit standard RPC so the test observes the server, without changing the Card.
            response = await http.post(
                body.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": uuid4().hex,
                    "method": body.action,
                    "params": MessageToDict(request),
                },
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                yield {
                    "type": "error",
                    "message": f"远端拒绝 GetExtendedAgentCard（JSON-RPC code {payload['error'].get('code')}）",
                }
                return
            result = ParseDict(payload["result"], AgentCard())
            yield {"type": "result", "action": body.action, "data": MessageToDict(result)}
        elif body.action in {"SendMessage", "SendStreamingMessage", "SubscribeToTask"}:

            async def responses() -> AsyncIterator[ProtoMessage]:
                if body.action == "SendMessage":
                    result = await transport.send_message(request)
                    event = a2a_types.StreamResponse()
                    if result.HasField("task"):
                        event.task.CopyFrom(result.task)
                    elif result.HasField("message"):
                        event.message.CopyFrom(result.message)
                    else:
                        raise ValueError("Response has neither task nor message")
                    yield event
                else:
                    method = (
                        transport.send_message_streaming
                        if body.action == "SendStreamingMessage"
                        else transport.subscribe
                    )
                    async for event in method(request):
                        yield event

            task_id = ""
            async for event in responses():
                yield {"type": "event", "data": MessageToDict(event)}
                if event.HasField("task"):
                    task_id = event.task.id
                elif event.HasField("status_update"):
                    task_id = event.status_update.task_id
                elif event.HasField("artifact_update"):
                    task_id = event.artifact_update.task_id
            if task_id:
                # A failed follow-up query must not erase a successful send or trigger duplicate retries.
                try:
                    task = await transport.get_task(
                        a2a_types.GetTaskRequest(id=task_id, tenant=getattr(request, "tenant", ""))
                    )
                    yield {"type": "task", "data": MessageToDict(task)}
                except Exception as error:  # noqa: BLE001 -- preserve a successful submission.
                    yield {
                        "type": "warning",
                        "message": f"消息已提交（Task {task_id}），但后续 GetTask 失败：{type(error).__name__}；请查询任务，勿重复发送。",
                    }
        else:
            result = await getattr(transport, OPERATIONS[body.action][0])(request)
            yield {
                "type": "result",
                "action": body.action,
                "data": MessageToDict(result) if result is not None else {"deleted": True},
            }
        yield {"type": "done"}


def safe_error(error: Exception) -> dict[str, Any]:
    # Never echo server diagnostics: they may include credentials, URLs or model configuration.
    if isinstance(error, AgentAccessError):
        return {
            "type": "error",
            "code": "agent_auth",
            "http_status": error.status,
            "message": f"目标 Agent 拒绝调用（HTTP {error.status}）。"
            + (
                "凭证未被接受：请向目标 Agent 部署者确认 token 是否正确、有效且属于当前服务。"
                if error.status == 401
                else "访问被拒绝：请联系目标 Agent 部署者检查账号、任务及资源权限。"
            )
            + "Nacos 登录凭证不能代替 Agent token。",
        }
    return {
        "type": "error",
        "message": f"操作失败：{type(error).__name__}。请检查目标服务支持、任务状态与调用凭证；停止接收不会取消远端任务。",
    }


def create_app(saved: dict[str, str] | None = None) -> FastAPI:
    sessions: dict[str, Registry] = {}
    saved = saved or {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        sessions.clear()

    app = FastAPI(title="Agent Registration Center Explorer", lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, next_handler: Any) -> Response:
        # Restrict DNS rebinding and cross-site credentialed requests to this local demo.
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "[::1]", "::1"}:
            return Response("Localhost only", status_code=403)
        if request.method != "GET" and request.headers.get("x-arc-request") != "1":
            return Response("Missing local application header", status_code=403)
        incoming_origin = request.headers.get("origin")
        if incoming_origin and incoming_origin != str(request.base_url).rstrip("/"):
            return Response("Cross-origin request rejected", status_code=403)
        if request.method == "POST" and len(await request.body()) > 16 * 1024 * 1024:
            return Response("Request too large (16 MiB)", status_code=413)
        response = await next_handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def registry(request: Request) -> Registry:
        key = request.cookies.get(COOKIE, "")
        session = sessions.get(key)
        if session is None or time.monotonic() >= session.expires:
            sessions.pop(key, None)
            raise HTTPException(401, "请先连接 Nacos；登录过期后需重新连接")
        return session

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, _error: RequestValidationError) -> Response:
        return Response(
            json.dumps({"detail": "请求字段无效，请检查参数类型和长度"}),
            status_code=422,
            media_type="application/json",
        )

    @app.exception_handler(httpx.HTTPError)
    async def network_error(_: Request, error: httpx.HTTPError) -> Response:
        return Response(
            json.dumps({"detail": f"网络请求失败：{type(error).__name__}，请检查地址及网络连通性"}),
            status_code=502,
            media_type="application/json",
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return Path(__file__).with_name("index.html").read_text()

    @app.get("/assets/{name}")
    async def asset(name: str) -> Response:
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return Response(
            Path(__file__).with_name(name).read_text(),
            media_type="text/javascript" if name.endswith("js") else "text/css",
        )

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "url": DEFAULT_NACOS,
            "saved_available": bool(saved),
            "saved_agent_token": bool(saved.get("token")),
            "protocol": {
                "version": "1.0",
                "binding": "JSONRPC",
                "source": "a2a-sdk / A2AService protobuf descriptor",
                "operations": protocol_catalog(),
            },
        }

    @app.post("/api/connect")
    async def connect(body: Login, request: Request, response: Response) -> dict[str, Any]:
        url = origin(body.url)
        username, password = body.username, body.password.get_secret_value()
        if body.saved:
            if url != DEFAULT_NACOS or not saved:
                raise HTTPException(400, "本地凭证仅可用于默认 Nacos")
            username, password = saved["nacos_username"], saved["nacos_password"]
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.post(
                url + "/nacos/v3/auth/user/login", data={"username": username, "password": password}
            )
        if result.status_code in {401, 403}:
            raise HTTPException(
                401, f"Nacos 登录被拒绝（HTTP {result.status_code}），请填写并检查用户名和密码"
            )
        if result.is_error:
            raise HTTPException(
                502,
                f"Nacos 登录接口返回 HTTP {result.status_code}，请检查实例地址、反向代理或服务状态",
            )
        data = nacos_json(result)
        if not isinstance(data.get("accessToken"), str) or not data["accessToken"]:
            raise HTTPException(401, "Nacos 登录失败，请检查账号、密码或访问权限")
        try:
            ttl = float(data.get("tokenTtl", 18000))
        except (ValueError, TypeError) as error:
            raise HTTPException(502, "Nacos 返回无效的 tokenTtl") from error
        session = Registry(url, body.namespace, data["accessToken"], ttl)
        items = await session.catalog()
        # Replacing this browser's login does not affect other browser sessions.
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        for key in list(sessions):
            if sessions[key].expires <= time.monotonic():
                sessions.pop(key)
        key = secrets.token_urlsafe(32)
        sessions[key] = session
        response.set_cookie(
            COOKIE,
            key,
            httponly=True,
            samesite="strict",
            max_age=int(session.expires - time.monotonic()),
        )
        return {"items": items, "url": url, "namespace": body.namespace}

    @app.post("/api/disconnect")
    async def disconnect(request: Request, response: Response) -> dict[str, bool]:
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/agents")
    async def agents(request: Request) -> dict[str, Any]:
        return {"items": await registry(request).catalog()}

    @app.get("/api/detail")
    async def detail(request: Request, agent: str, version: str) -> dict[str, Any]:
        data = await registry(request).detail(agent, version)
        return {"detail": data, "endpoints": endpoints(data)}

    async def authorize(body: Target, request: Request) -> tuple[AgentCard, str]:
        card = selected_card(
            await registry(request).detail(body.agent, body.version), body.endpoint
        )
        token = body.token.get_secret_value().strip()
        if body.saved:
            parsed = urlsplit(body.endpoint)
            if f"{parsed.scheme}://{parsed.netloc}" != saved.get("origin") or not saved.get(
                "token"
            ):
                raise HTTPException(400, "本地 Agent 凭证未授权此端点，请手动填写对应凭证")
            token = saved["token"]
        if any(char.isspace() for char in token):
            raise HTTPException(
                400,
                "Agent token 格式无效：只填写 token 值，不要包含 Bearer 前缀、caller-id 或空格。",
            )
        if requires_bearer(card) and not token.strip():
            raise HTTPException(
                400,
                "此 Agent 的 AgentCard 要求 Bearer 鉴权，请填写目标 Agent token，或配置本地凭证文件。Nacos 登录凭证仅用于发现，不能代替 Agent 调用凭证。",
            )
        return card, token

    @app.post("/api/action")
    async def action(body: Operation, request: Request) -> StreamingResponse:
        card, token = await authorize(body, request)
        payload = operation_request(body, card)

        async def stream() -> AsyncIterator[str]:
            try:
                async with asyncio.timeout(1800):
                    async for event in invoke(card, body, token, payload):
                        yield json.dumps(event, ensure_ascii=False) + "\n"
            except Exception as error:  # noqa: BLE001 -- SDK/provider errors can contain credentials.
                yield json.dumps(safe_error(error), ensure_ascii=False) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/api/download")
    async def download(body: Download, request: Request) -> Response:
        card, token = await authorize(body, request)
        parsed, target = urlsplit(body.url), urlsplit(body.endpoint)
        if (
            (parsed.scheme, parsed.netloc) != (target.scheme, target.netloc)
            or parsed.username
            or parsed.password
        ):
            raise HTTPException(
                400, "仅代取目标 Agent 同 origin 的任务文件，跨 origin 文件请自行授权访问"
            )
        try:
            async with httpx.AsyncClient(
                timeout=60,
                headers={"Authorization": f"Bearer {token}"} if token else {},
                follow_redirects=False,
            ) as http:
                async with ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(
                    card
                ) as client:
                    task = await client.get_task(a2a_types.GetTaskRequest(id=body.task_id))
                parts = [
                    p
                    for a in task.artifacts
                    for p in a.parts
                    if p.HasField("url") and p.url == body.url
                ]
                if not parts:
                    raise HTTPException(403, "此文件不属于当前身份可读取的任务")
                async with http.stream("GET", body.url) as remote:
                    if remote.status_code != 200:
                        raise HTTPException(502, f"文件下载失败（HTTP {remote.status_code}）")
                    content = bytearray()
                    async for chunk in remote.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 20 * 1024 * 1024:
                            raise HTTPException(413, "演示客户端下载上限为 20 MiB")
                name = quote(parts[0].filename or "artifact", safe="")
                return Response(
                    bytes(content),
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
                )
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(502, safe_error(error)["message"]) from error

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=os.environ.get("ARC_CREDENTIALS_FILE") or None,
        help="Optional private JSON with nacos_username/nacos_password and origin/token; never served to browser",
    )
    args = parser.parse_args()
    saved = json.loads(args.credentials_file.read_text()) if args.credentials_file else None
    uvicorn.run(create_app(saved), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
