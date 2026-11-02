# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The app's Memory page (spec section 13): what Arcee remembers about the user.

Each fact with where it came from (learned from a task, or told), to edit or forget;
a field to tell Arcee something; export to JSON; and a switch to stop learning. The
page writes history.db directly, the way arceed does (WAL mode lets them share it;
decision 0014).
"""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from arcee.context.store import MemoryRow, Store, data_dir  # noqa: E402

# A fact the user tells Arcee here matters to them.
TOLD_IMPORTANCE: Final = 0.8
Runner = Callable[..., None]  # (work, done[, failed]), as Background.run


async def _with_store(work: Callable[[Store], Any]) -> Any:
    """Run `work(store)` on a store of its own, closed after."""
    store = Store(data_dir())
    try:
        return await work(store)
    finally:
        store.close()


class MemoryPage(Adw.PreferencesPage):
    def __init__(
        self,
        run: Runner,
        toast: Callable[[str, Callable[[], None] | None], None],
        learning: bool,
        on_learning: Callable[[bool], None],
    ) -> None:
        super().__init__()
        self._run = run
        self._toast = toast

        settings = Adw.PreferencesGroup(
            title="Memory",
            description="Arcee learns lasting facts about you from finished tasks, and "
            "brings back the ones that matter to each request. They stay on this computer, "
            "in Arcee's history.",
        )
        switch = Adw.SwitchRow(
            title="Learn from tasks",
            subtitle="Off: nothing new is remembered, and nothing is brought back",
            active=learning,
        )
        switch.connect("notify::active", lambda row, _p: on_learning(row.get_active()))
        settings.add(switch)
        self.add(settings)

        self._facts = Adw.PreferencesGroup(title="What Arcee remembers")
        export = Gtk.Button(
            icon_name="document-save-symbolic", tooltip_text="Export", valign=Gtk.Align.CENTER
        )
        export.add_css_class("flat")
        export.connect("clicked", lambda _b: self._export())
        reload = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Refresh", valign=Gtk.Align.CENTER
        )
        reload.add_css_class("flat")
        reload.connect("clicked", lambda _b: self.load())
        suffix = Gtk.Box(spacing=4)
        suffix.append(export)
        suffix.append(reload)
        self._facts.set_header_suffix(suffix)
        self._add_row = Adw.EntryRow(title="Tell Arcee something to remember")
        self._add_row.set_show_apply_button(True)
        self._add_row.connect("apply", lambda row: self._add(row.get_text().strip()))
        self._facts.add(self._add_row)
        self._rows: list[Gtk.Widget] = []
        self.add(self._facts)
        self.load()

    # --- reading ----------------------------------------------------------------------

    def load(self) -> None:
        self._run(_with_store(lambda store: store.all_memories()), self._show)

    def _show(self, rows: list[MemoryRow]) -> None:
        for row in self._rows:
            self._facts.remove(row)
        self._rows = []
        if not rows:
            empty = Adw.ActionRow(
                title="Nothing remembered yet",
                subtitle="Facts appear here as Arcee learns them from your tasks",
            )
            empty.add_css_class("dim-label")
            self._facts.add(empty)
            self._rows.append(empty)
            return
        for memory in rows:
            self._facts.add(self._fact_row(memory))

    def _fact_row(self, memory: MemoryRow) -> Gtk.Widget:
        when = time.strftime("%d %b %Y", time.localtime(memory.updated))
        source = "You told Arcee" if memory.source == "note" else "Learned from a task"
        weight = " · important" if memory.importance >= 0.8 else ""
        row = Adw.EntryRow(title=f"{source} · {when}{weight}", text=memory.text)
        row.set_show_apply_button(True)
        row.connect("apply", lambda r, m=memory: self._edit(m, r.get_text().strip()))
        forget = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text="Forget", valign=Gtk.Align.CENTER
        )
        forget.add_css_class("flat")
        forget.connect("clicked", lambda _b, m=memory: self._forget(m))
        row.add_suffix(forget)
        self._rows.append(row)
        return row

    # --- changing ---------------------------------------------------------------------

    def _add(self, text: str) -> None:
        if not text:
            return

        async def save(store: Store) -> str:
            return await store.save_memory(text, TOLD_IMPORTANCE, source="note")

        def saved(_id: str) -> None:
            self._add_row.set_text("")
            self._toast("Remembered", None)
            self.load()

        self._run(_with_store(save), saved, self._failed)

    def _edit(self, memory: MemoryRow, text: str) -> None:
        if not text or text == memory.text:
            return

        async def save(store: Store) -> bool:
            return await store.update_memory(memory.id, text=text)

        self._run(_with_store(save), lambda _ok: self._toast("Saved", None), self._failed)

    def _forget(self, memory: MemoryRow) -> None:
        async def forget(store: Store) -> bool:
            return await store.forget_memory(memory.id)

        def undo() -> None:
            async def restore(store: Store) -> str:
                return await store.save_memory(
                    memory.text, memory.importance, task_id=memory.task_id, source=memory.source
                )

            self._run(_with_store(restore), lambda _id: self.load(), self._failed)

        def forgot(_ok: bool) -> None:
            self._toast(f"Forgot “{memory.text[:40]}”", undo)
            self.load()

        self._run(_with_store(forget), forgot, self._failed)

    def _failed(self, error: BaseException) -> None:
        self._toast(f"Could not change memory: {error}", None)

    # --- export -----------------------------------------------------------------------

    def _export(self) -> None:
        dialog = Gtk.FileDialog(title="Export Arcee's memory", initial_name="arcee-memory.json")

        def chosen(source: Any, result: Any) -> None:
            try:
                file = source.save_finish(result)
            except GLib.Error:
                return  # cancelled
            if file is None:
                return
            self._run(
                _with_store(lambda store: store.all_memories()), lambda rows: write(rows, file)
            )

        def write(rows: list[MemoryRow], file: Gio.File) -> None:
            data = [
                {
                    "id": r.id,
                    "text": r.text,
                    "importance": r.importance,
                    "source": r.source,
                    "task_id": r.task_id,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r.created)),
                    "updated": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r.updated)),
                }
                for r in rows
            ]
            Path(file.get_path()).write_text(json.dumps(data, indent=2, ensure_ascii=False))
            self._toast(f"Exported {len(data)} facts", None)

        dialog.save(self.get_root(), None, chosen)
