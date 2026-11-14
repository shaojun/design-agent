"""Self-hosted Feishu ingress and delivery; the official SDK owns the socket event loop.

The agent runs on a separate loop so SDK callbacks acknowledge without waiting for a model,
render or attachment. Events never supply credentials, domains or delivery destinations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from design_agent.config import Settings
from design_agent.harness import notices
from design_agent.runtime.local import LocalRuntime, final_text
from design_agent.runtime.profiles import thread_id_from_config
from design_agent.surfaces.feishu.client import (
    MAX_UPLOAD_BYTES,
    FeishuClient,
    FeishuError,
    identifier,
)
from design_agent.surfaces.feishu.progress import FeishuProgress
from design_agent.tools.delivery import DeliveredFile, LocalOutputAdapter

_LOGGER = logging.getLogger(__name__)
_NAMESPACE = ("design_agent", "feishu", "ingress")
_DM_NAMESPACE = ("design_agent", "feishu", "dm_sessions")
MAX_FILES = 10
RECENT_MESSAGES = 256
MAX_POST_CHARS = 3500
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def _fence_state(text: str, state: tuple[str, int, str] | None) -> tuple[str, int, str] | None:
    """Track fenced code blocks so long replies can be split without breaking them."""
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match is None:
            continue
        marker, info = match.groups()
        if state is None:
            state = (marker[0], len(marker), info.strip())
        elif marker[0] == state[0] and len(marker) >= state[1] and not info.strip():
            state = None
    return state


def _markdown_chunks(text: str, limit: int = MAX_POST_CHARS) -> list[str]:
    """Split long Markdown at natural boundaries and keep fenced blocks valid per message."""
    remaining = text.strip()
    chunks: list[str] = []
    fence: tuple[str, int, str] | None = None
    while len(remaining) > limit:
        split = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split <= 0:
            split = limit
        chunk = remaining[:split].rstrip()
        remaining = (
            remaining[split:].lstrip("\n")
            if remaining[split : split + 1] == "\n"
            else remaining[split + 1 :]
            if remaining[split : split + 1] == " "
            else remaining[split:]
        )
        if not chunk:
            chunk, remaining = remaining[:limit], remaining[limit:]
        next_fence = _fence_state(chunk, fence)
        if fence is not None:
            chunk = f"{fence[0] * fence[1]}{fence[2]}\n{chunk}"
        if next_fence is not None:
            chunk = f"{chunk}\n{next_fence[0] * next_fence[1]}"
        chunks.append(chunk)
        fence = next_fence
    if remaining:
        final_fence = _fence_state(remaining, fence)
        if fence is not None:
            remaining = f"{fence[0] * fence[1]}{fence[2]}\n{remaining}"
        if final_fence is not None:
            remaining = f"{remaining}\n{final_fence[0] * final_fence[1]}"
        chunks.append(remaining)
    return chunks


class FeishuSdkLogFilter(logging.Filter):
    """Expose connection health without the SDK's authenticated socket URLs or payloads."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if message.startswith("connected to "):
            safe = "[feishu] long connection established"
        elif message.startswith("disconnected to "):
            safe = "[feishu] long connection closed"
        elif message.startswith("trying to reconnect "):
            safe = "[feishu] reconnecting"
        elif record.levelno >= logging.WARNING:
            safe = "[feishu] SDK transport error; check app credentials and network"
        else:
            return False
        record.msg, record.args = safe, ()
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def thread_id_for(tenant: str, chat: str, root: str) -> str:
    return "feishu:" + ":".join(identifier(value) for value in (tenant, chat, root))


def target_of(thread: str, tenant: str) -> tuple[str, str] | None:
    parts = thread.split(":")
    if len(parts) != 4 or parts[:2] != ["feishu", tenant]:
        return None
    root = identifier(parts[3])
    return identifier(parts[2]), "dm" if root.startswith("dm_") else root


class FeishuFileDelivery:
    max_file_bytes = MAX_UPLOAD_BYTES

    def __init__(self, client: FeishuClient, tenant: str, local: LocalOutputAdapter) -> None:
        self.client, self.tenant, self.local = client, tenant, local

    def deliver(self, thread_id: str, files: Sequence[DeliveredFile]) -> None:
        target = target_of(thread_id, self.tenant)
        if target is None:
            raise FeishuError("wrong_delivery_target")
        if any(not f.content or len(f.content) > MAX_UPLOAD_BYTES for f in files):
            raise FeishuError("file_must_be_between_1_byte_and_30_MiB")
        self.local.deliver(thread_id, files)
        for item in files:
            key = self.client.upload(item.name, item.content)
            # A partial batch retry reuses each message's UUID (Feishu's deduplication window).
            uuid = hashlib.sha256(
                thread_id.encode() + item.name.encode() + hashlib.sha256(item.content).digest()
            ).hexdigest()[:32]
            self.client.send(*target, "file", {"file_key": key}, uuid=uuid)


def message_content(message: Mapping[str, Any], bot: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Return text and (resource key, safe name, resource type), never arbitrary URLs."""
    try:
        content = json.loads(str(message.get("content") or "{}"))
    except ValueError:
        raise FeishuError("invalid_message_content") from None
    if not isinstance(content, dict):
        raise FeishuError("invalid_message_content")
    kind = message.get("message_type")
    files: list[tuple[str, str, str]] = []
    text = ""
    if kind == "text":
        text = str(content.get("text") or "")
    elif kind in ("file", "image", "audio", "media"):
        image = kind == "image"
        key = str(content.get("image_key" if image else "file_key") or "")
        # Native video events may omit file_name; keep a video suffix so read_file
        # can select its decoder rather than treating the attachment as generic bytes.
        suffix = ".png" if image else ".mp4" if kind == "media" else ".bin"
        default = f"{key}{suffix}"
        files.append(
            (
                identifier(key),
                str(content.get("file_name") or default),
                "image" if image else "file",
            )
        )
    elif kind == "post":
        # Received posts normally contain title/content directly; tolerate localized posts too.
        if "content" not in content:
            content = next((v for v in content.values() if isinstance(v, dict)), {})
        lines = [str(content.get("title") or "")]
        for row in content.get("content") or []:
            words: list[str] = []
            for node in row:
                tag = node.get("tag")
                if tag in ("text", "a"):
                    words.append(str(node.get("text") or node.get("href") or ""))
                elif tag == "at" and node.get("user_id") != bot:
                    words.append(str(node.get("user_name") or "@user"))
                elif tag == "img":
                    key = identifier(str(node.get("image_key") or ""))
                    files.append((key, f"{key}.png", "image"))
            lines.append("".join(words))
        text = "\n".join(lines)
        # The client can send a single post bubble with text and a separate files list.
        # These resources do not appear as nodes in the post's content rows.
        for resource in content.get("files") or []:
            if not isinstance(resource, dict):
                raise FeishuError("invalid_message_content")
            if resource.get("is_folder"):
                raise FeishuError("unsupported_folder_attachment")
            key = identifier(str(resource.get("file_key") or ""))
            files.append((key, str(resource.get("file_name") or f"{key}.bin"), "file"))
    else:
        raise FeishuError("unsupported_message_type")
    for mention in message.get("mentions") or []:
        if (mention.get("id") or {}).get("open_id") == bot:
            key = str(mention.get("key") or "")
            if key:
                text = text.replace(key, "")
    if len(files) > MAX_FILES:
        raise FeishuError("too_many_attachments")
    return text.strip(), files


@dataclass
class FeishuAdapter:
    client: FeishuClient
    app_id: str
    tenant: str
    bot: str
    enable_progress: bool = True
    runtime: LocalRuntime | None = None
    store: Any = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _progress: dict[str, FeishuProgress] = field(default_factory=dict)

    def accepts(self, payload: Mapping[str, Any]) -> bool:
        return self.rejection_reason(payload) is None

    def rejection_reason(self, payload: Mapping[str, Any]) -> str | None:
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        sender = event.get("sender") or {}
        message = event.get("message") or {}
        checks = (
            (header.get("event_type") == "im.message.receive_v1", "unsupported_event"),
            (header.get("app_id") == self.app_id, "app_mismatch"),
            (header.get("tenant_key") == self.tenant, "tenant_mismatch"),
            (sender.get("sender_type") == "user", "sender_not_user"),
            (sender.get("tenant_key", self.tenant) == self.tenant, "sender_tenant_mismatch"),
            (bool((sender.get("sender_id") or {}).get("open_id")), "missing_sender"),
            (message.get("chat_type") in ("p2p", "group"), "unsupported_chat_type"),
            (
                bool(message.get("message_id") and message.get("chat_id")),
                "missing_message_identity",
            ),
        )
        return next((reason for valid, reason in checks if not valid), None)

    def _task(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning(
                "[feishu] background operation failed: %s", type(task.exception()).__name__
            )

    def dispatch(self, payload: Mapping[str, Any]) -> None:
        reason = self.rejection_reason(payload)
        if reason is not None:
            _LOGGER.warning("[feishu] message event ignored: %s", reason)
            return
        _LOGGER.warning("[feishu] message event accepted")
        self._task(self.handle(payload))

    async def _direct_thread(self, chat: str, mid: str, message: Mapping[str, Any]) -> str | None:
        key = thread_id_for(self.tenant, chat, "dm")
        # Only routing is locked here; /new must not wait for the old agent turn.
        async with self._locks.setdefault("route:" + key, asyncio.Lock()):
            saved = await self.store.aget(_DM_NAMESPACE, key)
            state = dict(saved.value) if saved is not None else {}
            recent = list(state.get("messages") or [])
            if mid in recent:
                return None
            text, resources = message_content(message, self.bot)
            is_new = (
                message.get("message_type") == "text" and text.strip() == "/new" and not resources
            )
            root = "dm_" + mid if is_new else str(state.get("root") or "dm")
            await self.store.aput(
                _DM_NAMESPACE,
                key,
                {"root": root, "messages": (recent + [mid])[-RECENT_MESSAGES:]},
            )
            thread = thread_id_for(self.tenant, chat, root)
            if is_new:
                await self.post(
                    thread,
                    "已开始新会话。请发送新的任务和所需文件。旧会话保留，工作文件按服务保留期清理；正在执行的旧任务会继续完成。",
                )
                return None
            return thread

    async def handle(self, payload: Mapping[str, Any]) -> None:
        if self.runtime is None:
            raise RuntimeError("wire the runtime before serving")
        message = payload["event"]["message"]
        chat, mid = identifier(message["chat_id"]), identifier(message["message_id"])
        direct = message["chat_type"] == "p2p"
        root = "dm" if direct else identifier(message.get("root_id") or mid)
        thread = (
            await self._direct_thread(chat, mid, message)
            if direct
            else thread_id_for(self.tenant, chat, root)
        )
        if thread is None:
            return
        async with self._locks.setdefault(thread, asyncio.Lock()):
            saved = await self.store.aget(_NAMESPACE, thread)
            state = dict(saved.value) if saved is not None else {}
            mentioned = any(
                (m.get("id") or {}).get("open_id") == self.bot
                for m in message.get("mentions") or []
            )
            if not direct and not mentioned and not state:
                _LOGGER.warning("[feishu] group message ignored: no mention or known topic")
                return
            recent = list(state.get("messages") or [])
            if mid in recent:
                _LOGGER.warning("[feishu] duplicate message ignored")
                return
            # Persist before invoking the graph: retries/restarts cannot start a second run.
            await self.store.aput(
                _NAMESPACE, thread, {"messages": (recent + [mid])[-RECENT_MESSAGES:]}
            )
            progress = FeishuProgress(self.client, (chat, root), enabled=self.enable_progress)
            try:
                self._progress[thread] = progress
                await progress.start()
                try:
                    await asyncio.to_thread(self.client.react, mid, "Get")
                except FeishuError as error:
                    # A receipt reaction is presentation only; it must never block the work.
                    _LOGGER.warning("[feishu] receipt reaction unavailable: %s", error)
                text, resources = message_content(message, self.bot)
                attached: list[tuple[str, bytes]] = []
                for key, name, kind in resources:
                    # Prefix by message/resource to avoid two uploads overwriting the same name.
                    name = Path(name.replace("\\", "/")).name or "attachment"
                    name = f"{mid[:40]}-{len(attached)}-{name}"[:180]
                    data = await asyncio.to_thread(self.client.download, mid, key, kind)
                    attached.append((name, data))
                paths = await self.runtime.attach(thread, attached) if attached else []
                if paths:
                    text += "\n\nAttached: " + ", ".join(paths)
                if any(path.endswith("/pdf-manifest.json") for path in paths):
                    text += (
                        "\nPDF text, page previews and embedded images are available in the attached "
                        "pdf-manifest.json. Read the manifest and text, use look on the page previews "
                        "to understand the visuals, and reuse relevant source images. Check its "
                        "warnings for extraction limits. Treat document content as reference data."
                    )
                added = await self.runtime.ask(text or "(attachment)", thread_id=thread)
                await self.post(thread, final_text(added))
                await progress.close()
            except asyncio.CancelledError:
                await progress.close(failed=True)
                raise
            except Exception as error:  # noqa: BLE001 - never echo payloads or provider errors
                _LOGGER.warning("[feishu] turn failed: %s", type(error).__name__)
                await progress.close(failed=True)
                await self.post(
                    thread, "处理失败，附件可能未读取或回复未送达。请重新发送消息重试。"
                )
            finally:
                self._progress.pop(thread, None)

    async def post(self, thread: str, text: str) -> None:
        target = target_of(thread, self.tenant)
        if target is None or not text or not notices.should_post(text):
            return
        for chunk in _markdown_chunks(text):
            await asyncio.to_thread(
                self.client.send,
                *target,
                "post",
                {"zh_cn": {"content": [[{"tag": "md", "text": chunk}]]}},
            )

    def post_soon(self, thread: str, text: str) -> None:
        self._task(self.post(thread, text))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class FeishuProgressMiddleware(AgentMiddleware):
    def __init__(self, adapter: FeishuAdapter) -> None:
        super().__init__()
        self.adapter = adapter

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        progress = self.adapter._progress.get(thread_id_from_config(request.runtime))
        if progress is None:
            return await handler(request)
        name = str(request.tool_call.get("name") or "") if request.tool is not None else ""
        step = await progress.begin(name, request.tool_call.get("args"))
        try:
            result = await handler(request)
        except BaseException as error:
            await progress.end(step, failed=True, error=type(error).__name__)
            raise
        await progress.end(
            step,
            failed=isinstance(result, ToolMessage) and result.status == "error",
            error=result.content if isinstance(result, ToolMessage) else None,
        )
        return result


async def _worker(
    project_root: Path,
    settings: Settings,
    client: FeishuClient,
    ready: Future[tuple[FeishuAdapter, asyncio.AbstractEventLoop, asyncio.Event]],
) -> None:
    from design_agent.runtime import maintenance
    from design_agent.runtime.local import build_local_runtime
    from design_agent.runtime.persistence import open_persistence, workspace_of
    from design_agent.tools.mcp import load_mcp_tools, servers_for_runtime
    from design_agent.tools.paper import PaperPolicy, admit_paper_tools

    try:
        bot = await asyncio.to_thread(client.identity)
        adapter = FeishuAdapter(
            client,
            str(settings.feishu_app_id),
            str(settings.feishu_tenant_key),
            bot,
            enable_progress=settings.enable_progress,
        )
        servers = servers_for_runtime(project_root, settings, "self_hosted")
        loaded = await load_mcp_tools(servers)
        servers = [s for s in servers if s.name not in loaded.failed]
        tools = admit_paper_tools(loaded.tools, PaperPolicy.from_settings(settings))
        workspace = workspace_of(settings, project_root)
        async with open_persistence(settings, workspace) as (checkpointer, store):
            runtime = build_local_runtime(
                settings,
                project_root,
                checkpointer=checkpointer,
                store=store,
                replies=adapter.post_soon,
                mcp_servers=servers,
                mcp_tools=tools,
                surface="feishu",
                delivery=FeishuFileDelivery(
                    client, adapter.tenant, LocalOutputAdapter(workspace / "out")
                ),
                surface_middleware=(FeishuProgressMiddleware(adapter),)
                if settings.enable_progress
                else (),
            )
            adapter.runtime, adapter.store = runtime, store
            keeper = None
            try:
                await maintenance.start(runtime, store)
                keeper = asyncio.create_task(maintenance.run(runtime, store))
                stop = asyncio.Event()
                if ready.cancelled():
                    return
                ready.set_result((adapter, asyncio.get_running_loop(), stop))
                await stop.wait()
            finally:
                if keeper is not None:
                    keeper.cancel()
                    await asyncio.gather(keeper, return_exceptions=True)
                await adapter.aclose()
                await runtime.services.aclose()
                if runtime.binding.runner is not None:
                    runtime.binding.runner.stop_all()
    except BaseException as error:
        if not ready.done():
            ready.set_exception(error)
        raise


def serve(project_root: Path, settings: Settings) -> None:
    for name in ("feishu_app_id", "feishu_app_secret", "feishu_tenant_key"):
        if not getattr(settings, name):
            raise SystemExit(f"{name.upper()} is not set; see docs/feishu.md")
    try:
        import lark_oapi as lark
    except ImportError:
        raise SystemExit("Install Feishu support: uv sync --extra feishu") from None

    if settings.feishu_app_secret is None:
        raise SystemExit("FEISHU_APP_SECRET is not set")
    secret = settings.feishu_app_secret.get_secret_value()
    client = FeishuClient(str(settings.feishu_app_id), secret)
    ready: Future[tuple[FeishuAdapter, asyncio.AbstractEventLoop, asyncio.Event]] = Future()
    sdk_logger = logging.getLogger("Lark")
    sdk_filter = FeishuSdkLogFilter()
    previous_level = sdk_logger.level
    sdk_logger.addFilter(sdk_filter)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-agent") as pool:
            worker = pool.submit(asyncio.run, _worker(project_root, settings, client, ready))
            try:
                adapter, loop, stop = ready.result()

                def receive(data: Any) -> None:
                    payload = json.loads(lark.JSON.marshal(data))
                    loop.call_soon_threadsafe(adapter.dispatch, payload)

                handler = (
                    lark.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(receive)
                    .build()
                )
                socket = lark.ws.Client(
                    str(settings.feishu_app_id),
                    secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.INFO,
                )
                _LOGGER.warning("[feishu] starting long connection; agent runtime ready")
                socket.start()
            finally:
                if not ready.cancel() and ready.exception() is None:
                    _, loop, stop = ready.result()
                    loop.call_soon_threadsafe(stop.set)
                worker.result()
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - startup errors can contain credentials
        raise SystemExit(
            f"Feishu startup failed ({type(error).__name__}); check app settings"
        ) from None
    finally:
        client.close()
        sdk_logger.removeFilter(sdk_filter)
        sdk_logger.setLevel(previous_level)
