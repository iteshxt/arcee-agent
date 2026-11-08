"""Running requests: the models, tools and store built once, a request run to its end.

`arcee-cli run` uses one runner for one request; the daemon (`arceed`) keeps one for
its lifetime and runs requests from the panel one at a time. Who answers questions
and confirmations, and where progress goes, differ per caller and are passed in.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from arcee.context.conversation import Conversation, Turn
from arcee.context.digest import digest_task
from arcee.context.examples import ExampleLibrary
from arcee.context.recall import recall
from arcee.context.store import Store, data_dir
from arcee.core.agent import DEFAULT_LIMITS, Agent, Event, Limits, Planned, Sharing, ToolRan
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner, needs_plan
from arcee.core.policy import Policy
from arcee.core.presence import NoPresence, Presence
from arcee.core.protocol import ConfirmRequest, PlannerOutput
from arcee.models.base import ModelAdapter
from arcee.models.errors import ModelUnavailableError
from arcee.models.ollama import DEFAULT_HOST
from arcee.models.presets import (
    DEFAULT_CORE,
    core_ladder,
    digest_model_for,
    planner_for,
    share_budget,
)
from arcee.tools.builtin import builtin_registry
from arcee.tools.voice import Say

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


log = logging.getLogger(__name__)
# How long closing waits for digests still running.
DIGEST_WAIT_S = 30.0
# After arceed starts, how long before it fills in the days and weeks with no summary
# (it starts at login, when the user is likely to ask for something first).
DAYS_DELAY_S = 120.0


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
        policy: Policy | None = None,
        memory: bool = True,
        sharing: Sharing = "ask",
    ) -> None:
        """`memory`: recall what Arcee remembers into each request, and digest each
        finished task into a summary and facts (decision 0014). Off for evals."""
        self.core = core
        self.host = host
        self.ladder = core_ladder(core, host)
        self.planner_model = planner_for(core, host)
        share_budget(self.ladder, self.planner_model)
        self.memory = memory
        self.sharing: Sharing = sharing  # personal data to a cloud model (decision 0017)
        self.digest_model = digest_model_for(core, host)
        share_budget(self.ladder, self.digest_model)
        # Digests still running: aclose waits for them before closing the store.
        self._digests: set[asyncio.Task[None]] = set()
        self.store = store or Store(data_dir())
        # arceed's voice (decision 0019), set by arceed: `voice.say` fails without it.
        self.say_aloud: Say | None = None
        self.registry = builtin_registry(
            memory=self.store, reminders=self.store, say=lambda: self.say_aloud
        )
        self.presence = presence or NoPresence()
        self._limits = limits
        self._plan = plan
        self._policy = policy or Policy()
        self._examples = ExampleLibrary.load() if examples else None
        self._usable: list[ModelAdapter] = []
        # Set by whoever cancels a run because the user pressed Stop.
        self.stopped = False
        # The task running now (background jobs wait for theirs to end, decision 0020).
        self.current_task: str | None = None

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

    async def run(
        self,
        request: str,
        user: User,
        on_event: Callable[[Event], None],
        *,
        spoken: bool = False,
        conversation: Conversation | None = None,
    ) -> TaskResult:
        """Run `request` to its end and record it. A cancel is recorded, then re-raised;
        `stopped` names the cancel the user asked for (Stop) instead of an interrupt.
        `spoken`: the request was transcribed from speech, so words may be misheard.
        `conversation`: the turns it continues; it is added to it as the next one."""
        if not self._usable:
            _, problem = await self.check()
            if problem:
                raise ModelUnavailableError(problem)
        plans: list[PlannerOutput] = []
        did: list[str] = []

        def record(event: Event) -> None:
            if isinstance(event, Planned):
                plans.append(event.plan)
            if isinstance(event, ToolRan) and event.result.status == "ok":
                did.append(f"{event.result.tool}: {event.result.summary}")
            on_event(event)

        def turn(status: str, reply: str) -> None:
            if conversation is not None:
                conversation.add(Turn(request, reply, status, tuple(did)))

        planner = (
            None
            if self._plan == "never"
            else Planner(self.planner_model, self.registry, home=Path.home())
        )
        agent = Agent(
            self._usable,
            self.registry,
            Executor(Gate(self._policy, user), record=self.store.record_action),
            user,
            home=Path.home(),
            limits=self._limits,
            planner=planner,
            notes=self.store,
            examples=self._examples,
            plan_if=needs_plan if self._plan == "auto" else lambda request: True,
            on_event=record,
            presence=self.presence,
            recall=self._recall if self.memory else None,
            sharing=self.sharing,
        )
        task_id = await self.store.start_task(
            request, conversation=conversation.begin() if conversation is not None else None
        )
        started = time.monotonic()
        self.stopped = False
        self.current_task = task_id
        try:
            outcome = await agent.run(
                request,
                task_id=task_id,
                spoken=spoken,
                conversation=conversation.render() if conversation else "",
                earlier=conversation.requests() if conversation else (),
            )
        except ModelUnavailableError as exc:
            await self._finish(task_id, "failed", f"model unavailable: {exc}", plans)
            raise
        except asyncio.CancelledError:
            status = "stopped" if self.stopped else "cancelled"
            turn(status, "")
            await asyncio.shield(self._finish(task_id, status, "", plans))
            raise
        except Exception as exc:
            # A crash (the shell not answering) left the task "running" in History.
            await self._finish(task_id, "failed", f"{type(exc).__name__}: {exc}", plans)
            raise
        finally:
            self.current_task = None
        await self.store.finish_task(
            task_id,
            status=outcome.status,
            reply=outcome.reply,
            steps=outcome.steps,
            model_calls=outcome.model_calls,
            plan=plans[-1] if plans else None,
        )
        turn(outcome.status, outcome.reply)
        if self.memory:
            self._digest_later(task_id)
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

    def allow_always(self, tool: str) -> None:
        """The user's "Always allow" (policy ALLOWABLE): this running task's gate and
        the next ones let `tool` run without asking, and the setting keeps it."""
        from arcee.core.policy import ALLOWABLE
        from arcee.settings import load, save

        if tool not in ALLOWABLE:
            return
        self._policy.always_allow.add(tool)
        settings = load()
        if tool not in settings.always_allow:
            settings.always_allow = sorted({*settings.always_allow, tool})
            save(settings)

    async def _recall(self, query: str, task_id: str) -> str:
        return await recall(self.store, query, exclude_task=task_id)

    def _digest_later(self, task_id: str) -> None:
        """Digest the task in the background: the reply does not wait for memory."""
        task = asyncio.ensure_future(self._digest(task_id))
        self._digests.add(task)
        task.add_done_callback(self._digests.discard)

    async def _digest(self, task_id: str) -> None:
        try:
            # The local models summarise well but mistake results for facts.
            facts = self.core != "local"
            await digest_task(self.digest_model, self.store, task_id, facts=facts)
        except Exception as error:  # memory is an aid: a failed digest loses one summary
            log.warning("could not digest %s: %s", task_id, error)

    async def keep_days(self, *, delay: float = DAYS_DELAY_S) -> None:
        """arceed, while it runs: write the summaries of the days and weeks that are over
        (spec section 8), soon after it starts and then each night at 00:05."""
        from datetime import datetime

        from arcee.context.periods import catch_up, next_run

        if not self.memory:
            return
        await asyncio.sleep(delay)
        while True:
            try:
                if written := await catch_up(self.digest_model, self.store, datetime.now().date()):
                    log.info("wrote %d day and week summaries", written)
            except Exception as error:  # memory is an aid; try again tomorrow
                log.warning("could not write the day summaries: %s", error)
            await asyncio.sleep(next_run(datetime.now()))

    async def settle(self) -> None:
        """Let running digests finish, for up to DIGEST_WAIT_S (arcee-cli waits before
        it exits)."""
        if self._digests:
            await asyncio.wait(set(self._digests), timeout=DIGEST_WAIT_S)

    async def aclose(self) -> None:
        await self.settle()
        models = [*self.ladder, self.planner_model, self.digest_model]
        await asyncio.gather(*(model.aclose() for model in models))
        self.store.close()
