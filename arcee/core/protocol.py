"""Wire protocol between the harness stages (spec section 7).

Everything that crosses a stage boundary is one of these models. The JSON Schema
each model generates is sent to the model as part of its prompt and used to
validate the reply, so field names and descriptions here are prompt text: keep
them short and unambiguous.

Two kinds of models live here:

- Model replies (`PlannerOutput`, `CoreAction`) are written by an LLM. They
  ignore unknown keys, because a stray key is not worth a retry against a
  rate-limited free tier, and they normalise the few mistakes that have exactly
  one correct reading.
- Everything else is written by the harness and rejects unknown keys.

All models are immutable. Top-level objects carry `v` so stored history stays
readable when the protocol changes.
"""

from typing import Annotated, Any, Final, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

PROTOCOL_VERSION: Final = 1

THOUGHT_MAX_CHARS: Final = 300

# A tool name: dotted lowercase segments, e.g. "fs.read" or "mcp.github.search".
TOOL_NAME_PATTERN: Final = r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$"
# A Planner tool filter: a tool name, a namespace wildcard ("browser.*") or "*".
TOOL_FILTER_PATTERN: Final = r"^(\*|[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*(\.\*)?)$"

type StepStatus = Literal["in_progress", "step_done", "task_done", "stuck"]
type ResultStatus = Literal["ok", "error", "refused", "timeout", "cancelled"]
type TaskStatus = Literal[
    "queued",
    "planning",
    "running",
    "waiting_confirm",
    "waiting_user",
    "done",
    "failed",
    "cancelled",
]
type PlanItemState = Literal["done", "current", "pending"]
type Risk = Literal["low", "medium", "high"]


def _clip(limit: int) -> BeforeValidator:
    """Truncate an over-long string instead of rejecting it; a retry costs more than the tail."""

    def clip(value: object) -> object:
        return value[:limit] if isinstance(value, str) else value

    return BeforeValidator(clip)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


type Text = Annotated[str, AfterValidator(_not_blank)]
type ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]


class _HarnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ReplyModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Versioned(BaseModel):
    v: Literal[1] = PROTOCOL_VERSION


# --- 7.1 Planner output -----------------------------------------------------------


class PlanStep(_ReplyModel):
    id: int = Field(ge=1, description="1-based position in the plan.")
    do: Text = Field(description="What to do in this step, as one instruction.")
    tools: tuple[Annotated[str, Field(pattern=TOOL_FILTER_PATTERN)], ...] = Field(
        default=("*",),
        description='Tools the step may use: names, "namespace.*" or "*".',
    )
    success: Text = Field(description="Observable condition that means the step is done.")


class Needs(_ReplyModel):
    browser: bool = False
    desktop_gui: bool = False
    shell: bool = False


class PlannerOutput(_Versioned, _ReplyModel):
    """The Planner's structured task. The orchestrator attaches the task id."""

    goal: Text = Field(description="The user's goal restated precisely.")
    interpretation: str = Field(description="Assumptions made to resolve ambiguity.")
    clarify: str | None = Field(
        default=None,
        description="A question for the user when the request cannot be planned; else null.",
    )
    steps: tuple[PlanStep, ...] = ()
    constraints: tuple[str, ...] = ()
    needs: Needs = Needs()
    memory_used: tuple[str, ...] = Field(default=(), description="Ids of memory items relied on.")
    estimated_steps: int = Field(default=0, ge=0, description="Expected number of tool actions.")

    @model_validator(mode="after")
    def _check_steps(self) -> Self:
        if self.clarify is not None:
            return self
        if not self.steps:
            raise ValueError("steps must not be empty unless clarify is set")
        ids = [step.id for step in self.steps]
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError(f"step ids must be 1..{len(ids)} in order, got {ids}")
        return self


# --- 7.2 Core input ---------------------------------------------------------------


class TaskBrief(_HarnessModel):
    goal: str
    constraints: tuple[str, ...] = ()


class PlanItem(_HarnessModel):
    id: int
    do: str
    state: PlanItemState


class StepDetail(_HarnessModel):
    id: int
    do: str
    success: str


class HistoryEntry(_HarnessModel):
    n: int
    action: str
    result: str


class MemorySnippet(_HarnessModel):
    id: str
    text: str


class NoObservation(_HarnessModel):
    kind: Literal["none"] = "none"


class TextObservation(_HarnessModel):
    kind: Literal["text", "shell_output", "file_content"]
    text: str
    source: str | None = None


# Tree and screenshot observations join this union in phases 2 and 3.
type Observation = Annotated[NoObservation | TextObservation, Field(discriminator="kind")]


class CoreInput(_Versioned, _HarnessModel):
    task: TaskBrief
    plan: tuple[PlanItem, ...]
    step: StepDetail
    history: tuple[HistoryEntry, ...] = ()
    memory: tuple[MemorySnippet, ...] = ()
    observation: Observation = NoObservation()
    tools: tuple[str, ...]


# --- 7.3 Core output --------------------------------------------------------------


class Action(_ReplyModel):
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict[str, Any])


# Control tools that end the step loop fix the step status; the model only has to
# name the tool. Any other pairing with these statuses is ambiguous and rejected.
_STATUS_FOR_TOOL: Final[dict[str, StepStatus]] = {
    "control.done": "task_done",
    "control.replan": "stuck",
}
_TOOL_FOR_STATUS: Final = {status: tool for tool, status in _STATUS_FOR_TOOL.items()}


class CoreAction(_Versioned, _ReplyModel):
    thought: Annotated[str, _clip(THOUGHT_MAX_CHARS)] = Field(
        default="",
        max_length=THOUGHT_MAX_CHARS,
        description="One or two sentences of reasoning; shown to the user.",
    )
    action: Action
    step_status: StepStatus = Field(
        default="in_progress",
        description=(
            "step_done when this action completes the current step; "
            "task_done only with control.done; stuck only with control.replan."
        ),
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = Field(
        default=False, description="Ask the user before running this action."
    )

    @model_validator(mode="before")
    @classmethod
    def _status_from_control_tool(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, object], data)
        action = fields.get("action")
        tool = cast(dict[str, object], action).get("tool") if isinstance(action, dict) else None
        status = _STATUS_FOR_TOOL.get(tool) if isinstance(tool, str) else None
        return {**fields, "step_status": status} if status else fields

    @model_validator(mode="after")
    def _check_status_pairing(self) -> Self:
        required_tool = _TOOL_FOR_STATUS.get(self.step_status)
        if required_tool and self.action.tool != required_tool:
            raise ValueError(f"step_status {self.step_status!r} requires tool {required_tool!r}")
        return self


# --- 7.4 Executor result ----------------------------------------------------------


class ExecutorResult(_Versioned, _HarnessModel):
    n: int = Field(ge=1)
    tool: str
    status: ResultStatus
    summary: str
    data: dict[str, Any] | None = Field(
        default=None, description="Structured facts from the tool; what the model reads first."
    )
    output: str | None = None
    error: str | None = None
    duration_ms: int = Field(ge=0)
    screen_changed: bool | None = None


# --- 7.6 Confirmation request -----------------------------------------------------


class ConfirmRequest(_Versioned, _HarnessModel):
    request_id: str
    task_id: str
    tool: str
    sentence: str
    risk: Risk
    args_preview: tuple[str, ...] = ()
    timeout_s: int = Field(default=120, ge=1)


# --- 7.7 Replan context -----------------------------------------------------------


class ReplanContext(_HarnessModel):
    """What the Planner receives on top of the original request when the Core is stuck."""

    failed_step: int = Field(ge=1)
    history_summary: str
    tried: tuple[str, ...]
    replan_count: int = Field(ge=1)
