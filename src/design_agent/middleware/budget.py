"""An optional soft budget per job (`DESIGN_AGENT_JOB_BUDGET_USD`).

When a thread's metered model spend (`middleware/meter.py`, an estimate from `config/prices.toml`)
reaches the budget, the lead is told once, before its next model call, to ask the person before
starting more work. It stops nothing in code: whoever asked decides, and work already running
finishes. The note is written into the history once, like the date note, so the cached prefix is
not disturbed on every call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import HumanMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime

from design_agent.middleware.meter import METER


class BudgetState(AgentState[Any]):
    budget_noted: NotRequired[Annotated[bool, PrivateStateAttr]]
    budget_unpriced_noted: NotRequired[Annotated[bool, PrivateStateAttr]]


def _thread_id() -> str:
    try:
        return str((get_config().get("configurable") or {}).get("thread_id") or "")
    except RuntimeError:
        return ""


class JobBudgetMiddleware(AgentMiddleware[BudgetState, Any, Any]):
    """Tell the lead once when its job's spend reaches the budget (module doc)."""

    state_schema = BudgetState

    def __init__(self, budget_usd: float, *, spent: Callable[[str], float] | None = None) -> None:
        super().__init__()
        self._budget = budget_usd
        self._spent = spent or (lambda thread: METER.summary(thread).usd)

    async def abefore_model(
        self,
        state: BudgetState,
        runtime: Runtime[Any],  # noqa: ARG002 - hook signature
    ) -> dict[str, Any] | None:
        thread = _thread_id()
        unpriced = METER.summary(thread).unpriced_calls if thread else 0
        update: dict[str, Any] = {}
        messages: list[HumanMessage] = []
        if unpriced and not state.get("budget_unpriced_noted"):
            messages.append(
                HumanMessage(
                    "[Budget] Some model calls have no configured price. The recorded dollar spend "
                    "is incomplete, so it cannot establish that this job is within budget. Tell the "
                    "person about the missing costs. Media charges are excluded from this model budget."
                )
            )
            update["budget_unpriced_noted"] = True
        spent = self._spent(thread) if thread else 0.0
        if spent >= self._budget and not state.get("budget_noted"):
            messages.append(
                HumanMessage(
                    f"[Budget] This job's model spend is about ${spent:.2f} (an estimate), past its "
                    f"${self._budget:.2f} budget. Before starting more work, tell the person what is done "
                    "and what is left, and ask whether to continue."
                )
            )
            update["budget_noted"] = True
        if messages:
            update["messages"] = messages
            return update
        return None
