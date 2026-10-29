"""The tool registry (spec section 11).

One place answers four questions about tools: which exist, which are offered
for a step, whether a model's call is valid, and how the offered tools look to
the model in each call mode. Validation errors are written for the retry prompt:
they name the problem, suggest the closest real name and list what is accepted,
because small models mostly fail by writing a near-miss name (decision 0003).
"""

import copy
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from fnmatch import fnmatchcase
from typing import Any, Final, cast

from pydantic import ValidationError

from arcee.core.protocol import TOOL_FILTER_PATTERN, TOOL_NAME_PATTERN
from arcee.models.base import provider_schema
from arcee.tools.base import Tier, Tool, ToolArgs

MAX_REPORTED_ERRORS: Final = 5

type AnyTool = Tool[Any]


def wire_name(name: str) -> str:
    """The name a tool goes by in providers' tool-calling fields, which forbid dots."""
    return name.replace(".", "_")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A call that passed validation: the tool and its parsed arguments."""

    tool: AnyTool
    args: ToolArgs


class ToolCallError(ValueError):
    """The model's call cannot run as written. The message is meant for the retry prompt."""


class Registry:
    """Tools by name, in registration order."""

    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools: dict[str, AnyTool] = {}
        self._by_wire: dict[str, AnyTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AnyTool) -> None:
        name = tool.name
        if not re.fullmatch(TOOL_NAME_PATTERN, name):
            raise ValueError(f"tool name {name!r} must be dotted lowercase, like 'fs.list'")
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        wire = wire_name(name)
        if (other := self._by_wire.get(wire)) is not None:
            raise ValueError(f"tools {other.name!r} and {name!r} share the wire name {wire!r}")
        self._tools[name] = tool
        self._by_wire[wire] = tool

    def get(self, name: str) -> AnyTool | None:
        """Look a tool up by its dotted name or its wire name."""
        return self._tools.get(name) or self._by_wire.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[AnyTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def select(
        self, filters: Iterable[str] = ("*",), *, exclude: Iterable[str] = ()
    ) -> list[AnyTool]:
        """Tools matching any of `filters` and none of `exclude`, in registration order.

        Filters use the Planner's syntax: an exact name, a namespace like
        `browser.*`, or `*` for everything.
        """
        include, skip = _patterns(filters), _patterns(exclude)
        return [
            tool for tool in self if _matches(tool.name, include) and not _matches(tool.name, skip)
        ]

    def validate(
        self, name: str, args: Mapping[str, Any], offered: Sequence[AnyTool] | None = None
    ) -> ToolCall:
        """Resolve `name` among `offered` (default: all tools) and parse `args`.

        Raises `ToolCallError` with a message the model can act on.
        """
        pool = list(self) if offered is None else list(offered)
        tool = self.get(name)
        if tool is None or tool not in pool:
            raise ToolCallError(_unknown_tool(name, pool, registered=tool is not None))
        try:
            parsed = tool.args_model.model_validate(dict(args))
        except ValidationError as exc:
            raise ToolCallError(f"{tool.name}: {_argument_errors(exc, tool)}") from exc
        return ToolCall(tool, parsed)


# --- Rendering ------------------------------------------------------------------


def native_tools(tools: Iterable[AnyTool], *, compact: bool = False) -> list[dict[str, Any]]:
    """Provider tool definitions for native call mode (OpenAI-style, which Ollama uses).

    `compact` is for capable models: one-line descriptions without usage cards, and
    argument schemas without their descriptions.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": wire_name(tool.name),
                "description": _short(tool) if compact else _description(tool),
                "parameters": _bare(copy.deepcopy(dict(provider_schema(tool.args_model))))
                if compact
                else copy.deepcopy(dict(provider_schema(tool.args_model))),
            },
        }
        for tool in tools
    ]


def _short(tool: AnyTool) -> str:
    text = tool.description.split(". ")[0].rstrip(".") + "."
    return text + (" Asks the user first." if tool.tier is Tier.CONFIRM else "")


def _bare(schema: Any) -> Any:
    """A schema without descriptions and defaults: names, types, enums and limits stay."""
    if isinstance(schema, list):
        return [_bare(item) for item in cast(list[Any], schema)]
    if not isinstance(schema, dict):
        return schema
    node = cast(dict[str, Any], schema)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("description", "default", "examples"):
            continue
        if key == "properties":
            out[key] = {name: _bare(sub) for name, sub in cast(dict[str, Any], value).items()}
        else:
            out[key] = _bare(value)
    return out


def prompt_block(tools: Iterable[AnyTool]) -> str:
    """The tool list for schema call mode: one signature line and the card per tool."""
    return "\n\n".join(f"{tool.name}({_signature(tool)}): {_description(tool)}" for tool in tools)


def _description(tool: AnyTool) -> str:
    text = f"{tool.description}\n{tool.usage.render()}"
    if tool.tier is Tier.CONFIRM:
        text += "\nThe user is asked to confirm before it runs."
    return text


def _signature(tool: AnyTool) -> str:
    schema = provider_schema(tool.args_model)
    required = set(schema.get("required", ()))
    properties: dict[str, Any] = schema.get("properties", {})
    return ", ".join(
        f"{name}{'' if name in required else '?'}: {_type_name(prop)}"
        for name, prop in properties.items()
    )


def _type_name(schema: Mapping[str, Any]) -> str:
    if "enum" in schema:
        return "|".join(str(value) for value in schema["enum"])
    if "anyOf" in schema:
        options = cast(list[Mapping[str, Any]], schema["anyOf"])
        return "|".join(_type_name(option) for option in options if option.get("type") != "null")
    kind = schema.get("type", "any")
    if kind == "array":
        return f"list[{_type_name(schema.get('items', {}))}]"
    return str(kind)


# --- Error messages -------------------------------------------------------------


def _unknown_tool(name: str, pool: Sequence[AnyTool], *, registered: bool) -> str:
    # Answer in the naming style the model used, so native mode sees wire names.
    wire = "." not in name
    names = [wire_name(tool.name) if wire else tool.name for tool in pool]
    problem = (
        f"tool {name!r} is not available for this step" if registered else f"unknown tool {name!r}"
    )
    return f"{problem}{_hint(name, names)}; tools are {', '.join(names) or 'none'}"


def _argument_errors(exc: ValidationError, tool: AnyTool) -> str:
    fields = tool.args_model.model_fields
    parts: list[str] = []
    for error in exc.errors(include_url=False)[:MAX_REPORTED_ERRORS]:
        where = ".".join(str(part) for part in error["loc"])
        match error["type"]:
            case "extra_forbidden":
                parts.append(f"unknown argument {where!r}{_hint(where, list(fields))}")
            case "missing":
                parts.append(f"missing argument {where!r}")
            case _:
                part = f"argument {where!r}: {error['msg']}"
                value = error.get("input")
                if isinstance(value, str | int | float | bool):
                    part += f" (got {value!r:.60})"
                parts.append(part)
    if (hidden := exc.error_count() - MAX_REPORTED_ERRORS) > 0:
        parts.append(f"and {hidden} more")
    accepted = ", ".join(name + ("" if f.is_required() else "?") for name, f in fields.items())
    parts.append(f"arguments are {accepted or 'none'}")
    return "; ".join(parts)


def _hint(name: str, choices: Sequence[str]) -> str:
    close = get_close_matches(name, choices, n=1, cutoff=0.6)
    return f" (did you mean {close[0]!r}?)" if close else ""


# --- Filters --------------------------------------------------------------------


def _patterns(filters: Iterable[str]) -> tuple[str, ...]:
    patterns = tuple(filters)
    for pattern in patterns:
        if not re.fullmatch(TOOL_FILTER_PATTERN, pattern):
            raise ValueError(f"bad tool filter {pattern!r}; use 'fs.read', 'fs.*' or '*'")
    return patterns


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)
