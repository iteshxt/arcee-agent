"""Bookmarks in Chrome through the extension."""

from arcee.core.toolsets import BROWSER, filters_for
from arcee.models.base import Message
from arcee.tools.browser import BookmarkArgs, BrowserBookmark, BrowserError

from .test_browser import CTX, session


async def test_the_page_open_is_bookmarked_in_a_folder() -> None:
    shared, client = session(
        {"bookmark": {"id": "9", "title": "Dal recipe", "url": "https://x/dal", "already": False}}
    )
    result = await BrowserBookmark(shared).run(BookmarkArgs(folder="Recipes"), CTX)
    method, params = client.calls[-1]
    assert (method, params["folder"], params["url"]) == ("bookmark", "Recipes", None)
    assert result.summary == "Bookmarked Dal recipe in Recipes"


async def test_one_already_saved_is_said_so_and_found() -> None:
    shared, _ = session(
        {
            "bookmark": {"id": "9", "title": "Bank", "url": "https://bank", "already": True},
            "find_bookmarks": [{"title": "Bank", "url": "https://bank"}],
        }
    )
    tool = BrowserBookmark(shared)
    again = await tool.run(BookmarkArgs(url="https://bank"), CTX)
    found = await tool.run(BookmarkArgs(action="find", query="bank"), CTX)
    assert again.summary == "Already bookmarked: Bank"
    assert found.data == {"bookmarks": [{"title": "Bank", "url": "https://bank"}]}


async def test_an_extension_without_the_permission_asks_for_a_reload() -> None:
    shared, _ = session(
        {"bookmark": BrowserError("Cannot read properties of undefined (reading 'bookmarks')")}
    )
    result = await BrowserBookmark(shared).run(BookmarkArgs(), CTX)
    assert result.error is not None
    assert "needs reloading" in result.error


def test_bookmark_words_bring_the_browser() -> None:
    assert set(BROWSER) <= set(filters_for([Message("user", "Bookmark this page")]))
