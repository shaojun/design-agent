"""A2A tasks share the runtime's SQLite/Postgres store, isolated by authenticated caller."""

from __future__ import annotations

import hashlib
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types.a2a_pb2 import ListTasksRequest, ListTasksResponse, Task, TaskState
from google.protobuf.json_format import MessageToDict, ParseDict

TASKS = ("design_agent", "a2a_tasks")
TERMINAL = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
}


def owner_key(context: ServerCallContext) -> str:
    return hashlib.sha256(context.user.user_name.encode()).hexdigest()


class PersistentTaskStore(TaskStore):
    def __init__(self, state: Any) -> None:
        self.state = state

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await self.state.api.store.aput((*TASKS, owner_key(context)), task.id, MessageToDict(task))

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        item = await self.state.api.store.aget((*TASKS, owner_key(context)), task_id)
        return ParseDict(item.value, Task()) if item else None

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await self.state.api.store.adelete((*TASKS, owner_key(context)), task_id)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        # Reuse the SDK's cursor, filtering and ordering semantics; storage remains durable.
        view = InMemoryTaskStore()
        offset = 0
        while items := await self.state.api.store.asearch(
            (*TASKS, owner_key(context)), limit=500, offset=offset
        ):
            for item in items:
                await view.save(ParseDict(item.value, Task()), context)
            offset += len(items)
        return await view.list(params, context)

    async def recover(self) -> None:
        """A killed execution cannot remain 'working' after this single worker restarts."""
        offset = 0
        while items := await self.state.api.store.asearch(TASKS, limit=500, offset=offset):
            for item in items:
                task = ParseDict(item.value, Task())
                if task.status.state not in TERMINAL:
                    task.status.state = TaskState.TASK_STATE_FAILED
                    task.status.message.parts.add(
                        text="Server restarted; submit a new task to continue."
                    )
                    task.status.timestamp.GetCurrentTime()
                    await self.state.api.store.aput(item.namespace, item.key, MessageToDict(task))
            offset += len(items)
