"""What a tool is (spec section 11): a name, an args model, a permission tier and `run`.

A tool also carries a usage card, the guidance a small model needs to pick it
correctly, and returns structured `data` so the model reads facts (counts,
matched paths) instead of re-deriving them from raw text (decision 0003).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from arcee.core.policy import Policy, Tier
from arcee.core.protocol import Risk

__all__ = [
    "NoArgs",
    "ObservationKind",
    "Tier",
    "Tool",
    "ToolArgs",
    "ToolContext",
    "ToolResult",
    "Usage",
]


type ObservationKind = Literal[
    "none",
    "text",
    "shell_output",
    "browser_tree",
    "desktop_tree",
    "screenshot_marks",
    "screenshot_raw",
    "file_content",
    "screenshot_auto",
]


@dataclass(frozen=True, slots=True)
class Usage:
    """A tool's usage card: rendered next to its description, only when the tool is offered."""

    when: str
    avoid: str = ""
    mistakes: str = ""
    example: str = ""

    def render(self) -> str:
        lines = [f"Use when: {self.when}"]
        if self.avoid:
            lines.append(f"Avoid: {self.avoid}")
        if self.mistakes:
            lines.append(f"Common mistakes: {self.mistakes}")
        if self.example:
            lines.append(f"Example: {self.example}")
        return "\n".join(lines)


class ToolArgs(BaseModel):
    """Base for every args model: unknown arguments are errors the model must fix."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoArgs(ToolArgs):
    pass


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a running tool may use. Grows with the platform, browser and store layers."""

    task_id: str
    home: Path


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A tool's own report; the executor wraps it into a protocol `ExecutorResult`.

    `summary` is one line for compressed history. `data` holds the facts the model
    should use as they are; `output` is raw text for tools with nothing structured
    to return.
    """

    status: Literal["ok", "error"]
    summary: str
    data: dict[str, Any] | None = None
    output: str | None = None
    error: str | None = None
    screen_changed: bool | None = None

    @classmethod
    def ok(
        cls, summary: str, *, data: dict[str, Any] | None = None, output: str | None = None
    ) -> ToolResult:
        return cls("ok", summary, data=data, output=output)

    @classmethod
    def fail(cls, error: str, *, summary: str | None = None) -> ToolResult:
        return cls("error", summary or error, error=error)


class Tool[A: ToolArgs](ABC):
    """One tool. Subclasses set the class attributes and implement `run`.

    `args_model` is the class the registry validates the model's arguments with;
    its field descriptions are prompt text, so keep them short.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    usage: ClassVar[Usage]
    args_model: type[A]
    tier: ClassVar[Tier] = Tier.AUTO
    risk: ClassVar[Risk] = "low"
    timeout_s: ClassVar[float] = 30.0
    observation_kind: ClassVar[ObservationKind] = "text"

    def tier_for(self, args: A, policy: Policy) -> Tier:
        """This call's tier. Tools whose risk depends on the arguments override it."""
        return self.tier

    def refusal(self, args: A, policy: Policy) -> str:
        """Why a `never` call is refused, in words the model can act on."""
        return f"{self.name} is not allowed"

    def sentence(self, args: A) -> str:
        """The confirmation question shown to the user; tools that can need one override it."""
        return f"Run {self.name}?"

    def preview(self, args: A) -> tuple[str, ...]:
        """The concrete targets listed under the confirmation question."""
        return ()

    @abstractmethod
    async def run(self, args: A, ctx: ToolContext) -> ToolResult: ...
