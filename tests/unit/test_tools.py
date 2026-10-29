import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import Field

from arcee.models.base import provider_schema
from arcee.tools.base import NoArgs, Tier, Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.registry import (
    Registry,
    ToolCallError,
    native_tools,
    prompt_block,
    wire_name,
)

CTX = ToolContext(task_id="t_1", home=Path("/home/u"))


class ListArgs(ToolArgs):
    path: str = Field(description="Directory to list")
    glob: str | None = None


class FsList(Tool[ListArgs]):
    name = "fs.list"
    description = "List a directory."
    usage = Usage(when="You need to see what is in a folder.")
    args_model = ListArgs

    async def run(self, args: ListArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("listed", data={"path": args.path, "count": 0})


class SearchArgs(ToolArgs):
    query: str
    root: str = "~"
    by: Literal["name", "content"] = "name"


class FsSearch(Tool[SearchArgs]):
    name = "fs.search"
    description = "Find files by name or content."
    usage = Usage(when="You do not know where a file is.", avoid="You already have its path.")
    args_model = SearchArgs

    async def run(self, args: SearchArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("searched")


class TrashArgs(ToolArgs):
    paths: list[str] = Field(min_length=1, max_length=50)


class FsTrash(Tool[TrashArgs]):
    name = "fs.trash"
    description = "Move files to the trash."
    usage = Usage(
        when="You know the exact files to delete.",
        avoid="You have not listed the folder yet.",
        mistakes="Passing the folder instead of the files.",
        example='fs.trash(paths=["~/a.deb"])',
    )
    args_model = TrashArgs
    tier = Tier.CONFIRM
    risk = "medium"

    def sentence(self, args: TrashArgs) -> str:
        return f"Move {len(args.paths)} item(s) to the trash?"

    async def run(self, args: TrashArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(f"Trashed {len(args.paths)}", data={"count": len(args.paths)})


class Done(Tool[NoArgs]):
    name = "control.done"
    description = "Finish the task."
    usage = Usage(when="The task is answered.")
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("done")


def registry() -> Registry:
    return Registry([FsList(), FsSearch(), FsTrash(), Done()])


class TestBase:
    def test_usage_card_renders_only_given_fields(self) -> None:
        assert Usage(when="w").render() == "Use when: w"
        assert FsTrash.usage.render().splitlines() == [
            "Use when: You know the exact files to delete.",
            "Avoid: You have not listed the folder yet.",
            "Common mistakes: Passing the folder instead of the files.",
            'Example: fs.trash(paths=["~/a.deb"])',
        ]

    def test_failed_result_summary_defaults_to_error(self) -> None:
        result = ToolResult.fail("no such folder")

        assert (result.status, result.summary, result.error) == (
            "error",
            "no such folder",
            "no such folder",
        )

    def test_default_sentence_names_the_tool(self) -> None:
        assert FsList().sentence(ListArgs(path="~")) == "Run fs.list?"

    async def test_run_returns_structured_data(self) -> None:
        result = await FsTrash().run(TrashArgs(paths=["~/a", "~/b"]), CTX)

        assert result.data == {"count": 2}


class TestRegistration:
    def test_lookup_by_dotted_or_wire_name(self) -> None:
        tools = registry()

        assert tools.get("fs.list") is tools.get("fs_list")
        assert "fs_trash" in tools
        assert "fs.nope" not in tools
        assert len(tools) == 4

    def test_keeps_registration_order(self) -> None:
        assert [tool.name for tool in registry()] == [
            "fs.list",
            "fs.search",
            "fs.trash",
            "control.done",
        ]

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            Registry([FsList(), FsList()])

    def test_rejects_wire_name_collisions(self) -> None:
        class Clash(FsList):
            name = "fs_list.x"

        class Other(FsList):
            name = "fs.list_x"

        with pytest.raises(ValueError, match="share the wire name 'fs_list_x'"):
            Registry([Clash(), Other()])

    @pytest.mark.parametrize("name", ["list", "Fs.list", "fs..list", "fs.list "])
    def test_rejects_malformed_names(self, name: str) -> None:
        class Bad(FsList):
            pass

        Bad.name = name
        with pytest.raises(ValueError, match="dotted lowercase"):
            Registry([Bad()])

    def test_wire_name_replaces_dots(self) -> None:
        assert wire_name("mcp.github.search") == "mcp_github_search"


class TestSelect:
    @pytest.mark.parametrize(
        ("filters", "exclude", "expected"),
        [
            (["*"], [], ["fs.list", "fs.search", "fs.trash", "control.done"]),
            (["fs.*"], [], ["fs.list", "fs.search", "fs.trash"]),
            (["control.done", "fs.list"], [], ["fs.list", "control.done"]),
            (["*"], ["fs.trash", "control.*"], ["fs.list", "fs.search"]),
            (["browser.*"], [], []),
        ],
    )
    def test_filters(self, filters: list[str], exclude: list[str], expected: list[str]) -> None:
        selected = registry().select(filters, exclude=exclude)

        assert [tool.name for tool in selected] == expected

    def test_rejects_malformed_filters(self) -> None:
        with pytest.raises(ValueError, match="bad tool filter"):
            registry().select(["fs.*.list"])


class TestValidate:
    def test_parses_arguments_with_defaults(self) -> None:
        call = registry().validate("fs.search", {"query": "tax"})

        assert call.tool.name == "fs.search"
        assert call.args == SearchArgs(query="tax", root="~", by="name")

    def test_accepts_wire_names(self) -> None:
        assert registry().validate("fs_list", {"path": "~"}).tool.name == "fs.list"

    def test_unknown_tool_suggests_and_lists_in_the_models_naming(self) -> None:
        tools = registry()

        with pytest.raises(ToolCallError) as dotted:
            tools.validate("fs.lst", {})
        with pytest.raises(ToolCallError) as wire:
            tools.validate("fs_serch", {})

        assert str(dotted.value).startswith(
            "unknown tool 'fs.lst' (did you mean 'fs.list'?); tools are fs.list, fs.search"
        )
        assert "(did you mean 'fs_search'?); tools are fs_list, fs_search, fs_trash" in str(
            wire.value
        )

    def test_tool_not_offered_this_step(self) -> None:
        tools = registry()

        with pytest.raises(
            ToolCallError, match=r"'fs.trash' is not available for this step; tools are fs.list$"
        ):
            tools.validate("fs.trash", {"paths": ["~/a"]}, offered=tools.select(["fs.list"]))

    def test_unknown_argument_suggests_and_lists_arguments(self) -> None:
        with pytest.raises(ToolCallError) as caught:
            registry().validate("fs.search", {"qeury": "tax"})

        assert str(caught.value) == (
            "fs.search: missing argument 'query'; "
            "unknown argument 'qeury' (did you mean 'query'?); "
            "arguments are query, root?, by?"
        )

    def test_reports_bad_values_with_what_was_given(self) -> None:
        with pytest.raises(
            ToolCallError,
            match=r"argument 'by': Input should be 'name' or 'content' \(got 'path'\)",
        ):
            registry().validate("fs.search", {"query": "x", "by": "path"})

    def test_reports_errors_inside_lists(self) -> None:
        with pytest.raises(
            ToolCallError, match=r"argument 'paths.1': Input should be a valid string"
        ):
            registry().validate("fs.trash", {"paths": ["~/a", 3]})

    def test_caps_reported_errors(self) -> None:
        many = {f"extra{i}": i for i in range(8)}

        with pytest.raises(ToolCallError, match=r"and 4 more; arguments are path, glob\?$"):
            registry().validate("fs.list", many)

    def test_tool_without_arguments(self) -> None:
        with pytest.raises(ToolCallError, match=r"arguments are none$"):
            registry().validate("control.done", {"reply": "hi"})


class TestRendering:
    def test_native_tools_use_wire_names_cards_and_flat_schemas(self) -> None:
        trash = native_tools([FsTrash()])[0]

        assert trash["type"] == "function"
        function = trash["function"]
        assert function["name"] == "fs_trash"
        assert function["description"].splitlines() == [
            "Move files to the trash.",
            *FsTrash.usage.render().splitlines(),
            "The user is asked to confirm before it runs.",
        ]
        assert function["parameters"] == provider_schema(TrashArgs)
        assert "title" not in json.dumps(function["parameters"])

    def test_native_parameters_are_independent_copies(self) -> None:
        first: dict[str, Any] = native_tools([FsList()])[0]["function"]["parameters"]
        first["properties"] = {}

        assert native_tools([FsList()])[0]["function"]["parameters"]["properties"]

    def test_prompt_block_has_signatures_and_cards(self) -> None:
        block = prompt_block(registry().select(["fs.*"]))

        assert block.split("\n\n") == [
            "fs.list(path: string, glob?: string): List a directory.\n"
            "Use when: You need to see what is in a folder.",
            "fs.search(query: string, root?: string, by?: name|content): "
            "Find files by name or content.\n"
            "Use when: You do not know where a file is.\n"
            "Avoid: You already have its path.",
            "fs.trash(paths: list[string]): Move files to the trash.\n"
            + FsTrash.usage.render()
            + "\nThe user is asked to confirm before it runs.",
        ]

    def test_empty_args_render_as_empty_signature(self) -> None:
        assert prompt_block([Done()]).startswith("control.done(): Finish the task.")
