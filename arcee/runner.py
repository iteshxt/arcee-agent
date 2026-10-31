"""Running requests: the models, tools and store built once, a request run to its end.

`arcee-cli run` uses one runner for one request; the daemon (`arceed`) keeps one for
its lifetime and runs requests from the panel one at a time. Who answers questions
and confirmations, and where progress goes, differ per caller and are passed in.
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from arcee.context.examples import ExampleLibrary
from arcee.context.store import Store, data_dir
from arcee.core.agent import DEFAULT_LIMITS, Agent, Event, Limits, Planned
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner, needs_plan
from arcee.core.policy import Policy
from arcee.core.presence import NoPresence, Presence
from arcee.core.protocol import ConfirmRequest, PlannerOutput
from arcee.core.routing import start_tier
from arcee.models.base import ModelAdapter
from arcee.models.errors import ModelUnavailableError
from arcee.models.ollama import DEFAULT_HOST
from arcee.models.presets import DEFAULT_CORE, core_ladder, planner_for, share_budget
from arcee.tools.builtin import builtin_registry

PlanMode = Literal["auto", "always", "never"]


class User(Protocol):
    """Who answers the agent's questions and confirms risky actions."""

    async def ask(self, question: str, options: Sequence[str] = ()) -> str: ...
    async def confirm(self, request: ConfirmRequest) -> bool: ...


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    status: str
    reply: str
    steps: int
    model_calls: int
    seconds: float


class TaskRunner:
    def __init__(
        self,
        *,
        core: str = DEFAULT_CORE,
        host: str = DEFAULT_HOST,
        limits: Limits = DEFAULT_LIMITS,
        plan: PlanMode = "auto",
        examples: bool = False,
        presence: Presence | None = None,
        store: Store | None = None,
    ) -> None:
        self.core = core
        self.host = host
        self.ladder = core_ladder(core, host)
        self.planner_model = planner_for(core, host)
        share_budget(self.ladder, self.planner_model)
        self.store = store or Store(data_dir())
        self.registry = builtin_registry()
        self.presence = presence or NoPresence()
        self._limits = limits
        self._plan = plan
        self._examples = ExampleLibrary.load() if examples else None
        self._usable: list[ModelAdapter] = []
        # Set by whoever cancels a run because the user pressed Stop.
        self.stopped = False

    async def check(self) -> tuple[list[str], str | None]:
        """Notes about skipped tiers, and why nothing can run (None when it can).

        The ladder's tiers whose model is reachable; the first one must be.
        """
        healthy = await asyncio.gather(*(model.health() for model in self.ladder))
        if not healthy[0]:
            first = self.ladder[0].profile.model
            return [], (
                f"Ollama is not running at {self.host}, or the model is missing. "
                f"Start it, then: ollama pull {first}"
            )
        notes = [
            f"note: {model.profile.model} is not pulled, so escalation skips it "
            f"(ollama pull {model.profile.model})"
            for model, ok in zip(self.ladder, healthy, strict=True)
            if not ok
        ]
        self._usable = [model for model, ok in zip(self.ladder, healthy, strict=True) if ok]
        return notes, None

    async def run(self, request: str, user: User, on_event: Callable[[Event], None]) -> TaskResult:
        """Run `request` to its end and record it. A cancel is recorded, then re-raised;
        `stopped` names the cancel the user asked for (Stop) instead of an interrupt."""
        if not self._usable:
            _, problem = await self.check()
            if problem:
                raise ModelUnavailableError(problem)
        plans: list[PlannerOutput] = []

        def record(event: Event) -> None:
            if isinstance(event, Planned):
                plans.append(event.plan)
            on_event(event)

        planner = (
            None
            if self._plan == "never"
            else Planner(self.planner_model, self.registry, home=Path.home())
        )
        agent = Agent(
            self._usable,
            self.registry,
            Executor(Gate(Policy(), user), record=self.store.record_action),
            user,
            home=Path.home(),
            limits=self._limits,
            planner=planner,
            notes=self.store,
            examples=self._examples,
            plan_if=needs_plan if self._plan == "auto" else lambda request: True,
            start_tier=start_tier if self.core == "groq" else lambda request: 0,
            on_event=record,
            presence=self.presence,
        )
        task_id = await self.store.start_task(request)
        started = time.monotonic()
        self.stopped = False
        try:
            outcome = await agent.run(request, task_id=task_id)
        except ModelUnavailableError as exc:
            await self._finish(task_id, "failed", f"model unavailable: {exc}", plans)
            raise
        except asyncio.CancelledError:
            status = "stopped" if self.stopped else "cancelled"
            await asyncio.shield(self._finish(task_id, status, "", plans))
            raise
        await self.store.finish_task(
            task_id,
            status=outcome.status,
            reply=outcome.reply,
            steps=outcome.steps,
            model_calls=outcome.model_calls,
            plan=plans[-1] if plans else None,
        )
        return TaskResult(
            task_id,
            outcome.status,
            outcome.reply,
            outcome.steps,
            outcome.model_calls,
            time.monotonic() - started,
        )

    async def _finish(
        self, task_id: str, status: str, reply: str, plans: list[PlannerOutput]
    ) -> None:
        await self.store.finish_task(
            task_id,
            status=status,
            reply=reply,
            steps=0,
            model_calls=0,
            plan=plans[-1] if plans else None,
        )

    async def aclose(self) -> None:
        await asyncio.gather(*(model.aclose() for model in [*self.ladder, self.planner_model]))
        self.store.close()
