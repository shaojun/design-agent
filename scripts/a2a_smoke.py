"""Use the official A2A 1.0 client against any operator-selected agent origin.

Credentials come from an environment variable, never from CLI arguments or an AgentCard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest, TaskState

from design_agent.surfaces.a2a.app import public_base


async def run(url: str, token: str, prompt: str, *, streaming: bool) -> dict[str, object]:
    origin = public_base(url)
    # Fetch public metadata without a bearer token. A foreign Card must not redirect credentials.
    async with httpx.AsyncClient(timeout=30) as metadata:
        card = await A2ACardResolver(metadata, origin).get_agent_card()
    if not card.supported_interfaces or any(
        (urlsplit(i.url).scheme, urlsplit(i.url).netloc)
        != (urlsplit(origin).scheme, urlsplit(origin).netloc)
        for i in card.supported_interfaces
    ):
        raise ValueError(
            "Card advertises a different origin; select and authorize that origin explicitly"
        )
    async with httpx.AsyncClient(
        timeout=600, headers={"Authorization": f"Bearer {token}"} if token else {}
    ) as http:
        async with ClientFactory(ClientConfig(httpx_client=http, streaming=streaming)).create(
            card
        ) as client:
            task_id = ""
            events = 0
            request = SendMessageRequest(
                message=Message(
                    message_id=uuid4().hex, role=Role.ROLE_USER, parts=[Part(text=prompt)]
                )
            )
            async for event in client.send_message(request):
                events += 1
                if event.HasField("task"):
                    task_id = event.task.id
                elif event.HasField("status_update"):
                    task_id = event.status_update.task_id
            if not task_id:
                raise RuntimeError("Agent returned no task")
            task = await client.get_task(GetTaskRequest(id=task_id))
            if task.status.state != TaskState.TASK_STATE_COMPLETED:
                raise RuntimeError(f"Task ended in {TaskState.Name(task.status.state)}")
            return {
                "agent": card.name,
                "protocol": "1.0",
                "task_id": task.id,
                "state": TaskState.Name(task.status.state),
                "events": events,
                "reply": "\n".join(
                    p.text for a in task.artifacts for p in a.parts if p.HasField("text")
                ),
                "files": [
                    {"name": p.filename, "url": p.url}
                    for artifact in task.artifacts
                    for p in artifact.parts
                    if p.HasField("url")
                ],
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Agent origin, e.g. http://127.0.0.1:8080")
    parser.add_argument("--token-env", default="A2A_TOKEN")
    parser.add_argument("--prompt", default="Reply with exactly A2A_OK. Do not call tools.")
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "")
    print(
        json.dumps(
            asyncio.run(run(args.url, token, args.prompt, streaming=not args.no_stream)),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
