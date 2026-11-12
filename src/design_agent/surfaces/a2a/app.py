"""Official SDK routing over the shared runtime; no second agent assembly."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from a2a.auth.user import User
from a2a.helpers.proto_helpers import new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    ServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    CancelTaskRequest,
    Message,
    Part,
    Role,
    Task,
    TaskState,
)
from a2a.utils.errors import TaskNotCancelableError, TaskNotFoundError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from design_agent.config import Settings
from design_agent.runtime.local import final_text
from design_agent.surfaces.a2a.progress import PROGRESS_KEY
from design_agent.surfaces.a2a.store import PersistentTaskStore

_LOGGER = logging.getLogger(__name__)


def public_base(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "DESIGN_AGENT_A2A_URL must be an http(s) origin without credentials or a path"
        )
    return url.rstrip("/")


def agent_card(settings: Settings) -> AgentCard:
    base = public_base(settings.a2a_url or "")
    card = AgentCard(
        name=settings.a2a_name,
        version=settings.a2a_version,
        description=(
            "Design stills, decks, diagrams, charts and motion graphics. "
            "Built on LangChain Deep Agents. The project supports separate A2A, HTTP API, "
            "Slack and Feishu surfaces; enabled channels depend on the deployment. "
            "Only the A2A interfaces listed here are callable through A2A."
        ),
        documentation_url=settings.a2a_documentation_url or "",
        supported_interfaces=[
            AgentInterface(url=f"{base}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/octet-stream"],
        skills=[
            AgentSkill(
                id="design",
                name="Design and motion",
                description="Create and revise on-brand visual deliverables from a text brief.",
                tags=["design", "slides", "charts", "motion"],
                examples=["Create a chart from these figures."],
            )
        ],
    )
    if not settings.a2a_allow_anonymous:
        card.security_schemes["bearer"].http_auth_security_scheme.scheme = "bearer"
        card.security_requirements.add().schemes["bearer"].SetInParent()
    return card


class Caller(User):
    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self.name


class CallerContext(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        # Only the verified identity crosses into SDK state; never retain auth headers in tasks.
        return ServerCallContext(
            user=Caller(request.state.a2a_caller),
            state={"headers": {"a2a-version": request.headers.get("a2a-version", "")}},
        )


def thread_for(caller: str, context_id: str) -> str:
    digest = hashlib.sha256(f"{len(caller)}:{caller}{context_id}".encode()).hexdigest()
    return f"a2a-{digest}"


class DesignExecutor(AgentExecutor):
    def __init__(self, app: FastAPI, base: str) -> None:
        self.app = app
        self.base = base
        self.locks: dict[str, asyncio.Lock] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        if not task_id or not context_id:
            raise ValueError("A2A task and context ids are required")
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_SUBMITTED,
                    history=[context.message] if context.message else [],
                )
            )
        await updater.submit()
        if context.configuration is not None and context.configuration.HasField(
            "task_push_notification_config"
        ):
            await updater.reject(
                self._message("Push notifications are not supported.", context_id, task_id)
            )
            return
        message = context.message
        if (
            message is None
            or message.role != Role.ROLE_USER
            or not message.parts
            or any(p.WhichOneof("content") != "text" for p in message.parts)
        ):
            await updater.reject(
                self._message("This interface accepts user text parts only.", context_id, task_id)
            )
            return
        text = context.get_user_input()
        if not text.strip() or len(text) > 20_000:
            await updater.reject(
                self._message("Supply 1 to 20000 characters of text.", context_id, task_id)
            )
            return
        state = self.app.state.api
        caller = context.call_context.user.user_name
        thread_id = thread_for(caller, context_id)
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            from design_agent.surfaces.api.app import THREADS, UPDATES, _files

            await state.store.aput(THREADS, thread_id, {"owner": caller})
            await updater.start_work(self._message("Agent started working.", context_id, task_id))

            async def progress(data: dict[str, Any]) -> None:
                labels = {"running": "开始", "completed": "完成", "failed": "失败"}
                text = f"工具 {data['tool']} · {labels[data['state']]} · {data['elapsedSeconds']}s"
                message = self._message(text, context_id, task_id)
                message.metadata.update({PROGRESS_KEY: data})
                await updater.update_status(TaskState.TASK_STATE_WORKING, message)

            self.app.state.a2a_progress[thread_id] = progress
            try:
                before = len(await state.store.asearch((*UPDATES, thread_id), limit=10_000))
                added = await state.runtime.ask(
                    text, thread_id=thread_id, configurable={"surface": "a2a"}
                )
                reply = final_text(added)
                if reply:
                    await updater.start_work(self._message(reply, context_id, task_id))
                # A lead can return while its helpers render. Keep the A2A task working.
                # Read helper replies while they run, rather than collecting only at completion.
                while True:
                    pending = state.runtime.thread_busy(thread_id)
                    updates = sorted(
                        await state.store.asearch((*UPDATES, thread_id), limit=10_000),
                        key=lambda i: i.key,
                    )
                    for item in updates[before:]:
                        reply = str(item.value.get("text", "")) or reply
                        await updater.start_work(self._message(reply, context_id, task_id))
                    before = len(updates)
                    if not pending:
                        break
                    await asyncio.sleep(0.5)
                if reply:
                    await updater.add_artifact([Part(text=reply)], name="reply")
                for name in _files(state.runtime, thread_id):
                    await updater.add_artifact(
                        [
                            Part(
                                url=f"{self.base}/threads/{thread_id}/files/{quote(name, safe='')}",
                                filename=name,
                                media_type=mimetypes.guess_type(name)[0]
                                or "application/octet-stream",
                            )
                        ],
                        name=name,
                    )
                await updater.complete(
                    self._message(reply or "Turn completed.", context_id, task_id)
                )
            except Exception as error:  # noqa: BLE001 - model/tool errors become safe task failures
                _LOGGER.warning("A2A execution failed: %s", type(error).__name__)
                await updater.failed(
                    self._message(
                        "Agent execution failed; contact the operator with this task id.",
                        context_id,
                        task_id,
                    )
                )
            finally:
                self.app.state.a2a_progress.pop(thread_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:  # noqa: ARG002
        # Stopping the lead alone would leave renders/helpers running and falsely claim cancellation.
        raise TaskNotCancelableError(
            message="This runtime cannot safely cancel a complete design job through A2A."
        )

    @staticmethod
    def _message(text: str, context_id: str, task_id: str) -> Message:
        return Message(
            message_id=uuid4().hex,
            role=Role.ROLE_AGENT,
            parts=[Part(text=text)],
            context_id=context_id,
            task_id=task_id,
        )


class DesignRequestHandler(DefaultRequestHandler):
    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        # Reject before the SDK attempts to cancel the producer: helpers may still be rendering.
        if await self.task_store.get(params.id, context) is None:
            raise TaskNotFoundError()
        raise TaskNotCancelableError(
            message="A2A job cancellation is not supported by this runtime."
        )


def install_a2a(app: FastAPI, settings: Settings) -> DefaultRequestHandler:
    from design_agent.surfaces.api.app import ANONYMOUS_A2A_CALLER, _caller

    card = agent_card(settings)
    app.state.a2a_progress = {}
    store = PersistentTaskStore(app.state)
    handler = DesignRequestHandler(
        DesignExecutor(app, public_base(settings.a2a_url or "")), store, card
    )
    app.state.a2a_store = store
    app.state.a2a_card = card
    app.routes.extend(create_agent_card_routes(card))
    app.routes.extend(
        create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=CallerContext())
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if request.url.path.rstrip("/") == "/a2a":
            try:
                request.state.a2a_caller = (
                    ANONYMOUS_A2A_CALLER if settings.a2a_allow_anonymous else _caller(request)
                )
            except HTTPException as error:
                return JSONResponse(
                    {"detail": error.detail}, status_code=error.status_code, headers=error.headers
                )
        return await call_next(request)

    return handler
