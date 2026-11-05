# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The Arcee app (phase 4): models and today's budget, history, permissions, diagnostics.

GTK 4 and libadwaita. Slow work (reading the history, running the checks) runs on a
background asyncio loop and comes back to GTK with GLib.idle_add, so the window never
freezes. Settings changes are saved to ~/.config/arcee/settings.json and arceed is
restarted when it is idle, so the next request uses them.
"""

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Final, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from arcee import install, local_models  # noqa: E402
from arcee.app import diagnostics  # noqa: E402
from arcee.app.agent_page import AgentPage  # noqa: E402
from arcee.app.formatting import show_markdown  # noqa: E402
from arcee.app.local_setup import LocalSetupDialog  # noqa: E402
from arcee.app.memory_page import MemoryPage  # noqa: E402
from arcee.app.setup import SetupDialog  # noqa: E402
from arcee.app.voice_group import VoiceGroup  # noqa: E402
from arcee.context.store import Store, data_dir  # noqa: E402
from arcee.core.policy import ALLOWABLE  # noqa: E402
from arcee.keys import KEYS, keyring, source  # noqa: E402
from arcee.models.presets import GROQ_MODELS, read_budget  # noqa: E402
from arcee.settings import Core, Sharing, load, save, save_always_allow  # noqa: E402
from arcee.voice import SpeechUsage  # noqa: E402

APP_ID: Final = "me.iteshxt.Arcee"
# Settings.personal_to_cloud, as the Permissions page offers it.
SHARING_CHOICES: Final[tuple[tuple[Sharing, str], ...]] = (
    ("ask", "Ask each time"),
    ("always", "Always allow"),
    ("off", "Never"),
)  # under the developer's own domain, iteshxt.me
BUDGET_REFRESH_S: Final = 5
HISTORY_LIMIT: Final = 50
# Automatic actions the user may make ask first, in their words, by where they act.
# Deleting, sending, paying and the like ask whatever is set here.
ASKABLE: Final = (
    ("app.open", "Opening apps", "Starting an app, or switching to it"),
    ("ui.act", "Acting in app windows", "Pressing buttons and menus in apps"),
    ("browser.click", "Clicking in Chrome", "Links and buttons on web pages"),
    ("browser.fill", "Typing into web pages", "Search boxes and forms"),
    ("screen.click", "Clicking on the screen", "Where an app has no other way in"),
    ("kbd.type", "Typing on the keyboard", "Into the window in front"),
    ("fs.write", "Writing files", "In your Arcee folder; anywhere else it asks"),
    ("fs.move", "Moving and renaming files", "Inside your home folder"),
    ("shell.run", "Running safe commands", "Commands that only read, such as ls and df"),
)
CORE_CHOICES: Final = (
    (
        "groq",
        "Groq",
        "gpt-oss-20b does most of the work; qwen3.8-27b and gpt-oss-120b take turns when "
        "it is busy, and 120b takes over when it struggles. Fast; what Arcee reads goes "
        "to Groq.",
    ),
    ("local", "Local (Ollama)", "LFM models on this computer. Private; slower and less able."),
)


class Background:
    """An asyncio loop on its own thread; results come back on the GTK thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="arcee-app").start()

    def run[T](
        self,
        work: Coroutine[Any, Any, T],
        done: Callable[[T], None],
        failed: Callable[[BaseException], None] = lambda exc: None,
    ) -> concurrent.futures.Future[T]:
        """Run `work`; its future, which `cancel()` stops (and then calls neither)."""
        future = asyncio.run_coroutine_threadsafe(work, self._loop)

        def finish(f: Any) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            GLib.idle_add(lambda: (failed(exc) if exc else done(f.result())) and False)

        future.add_done_callback(finish)
        return future


def restart_daemon_when_idle(background: Background, told: Callable[[str], None]) -> None:
    """Ask arceed to quit if it is idle, and start it again with the new settings when
    it listens for "hey Arcee" (D-Bus would start it only on the next request, and the
    wake word would be deaf until then)."""

    async def restart() -> str:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:

            def call(member: str) -> Message:
                return Message(
                    destination="me.iteshxt.Arcee.Agent",
                    path="/me/iteshxt/Arcee/Agent",
                    interface="me.iteshxt.Arcee.Agent",
                    member=member,
                )

            status = await bus.call(call("Status"))
            if status is None or status.message_type is not MessageType.METHOD_RETURN:
                return "Saved. Arcee will use it when it next starts."
            if '"running"' in str(status.body[0]):
                return "Saved. It takes effect after the current request."
            await bus.call(call("Quit"))
            from arcee.settings import load

            if load().listen:
                for _ in range(20):  # until the old one has let its name go
                    await asyncio.sleep(0.25)
                    owner = await bus.call(
                        Message(
                            destination="org.freedesktop.DBus",
                            path="/org/freedesktop/DBus",
                            interface="org.freedesktop.DBus",
                            member="NameHasOwner",
                            signature="s",
                            body=["me.iteshxt.Arcee.Agent"],
                        )
                    )
                    if owner is not None and owner.body and not owner.body[0]:
                        break
                await bus.call(call("Status"))  # D-Bus starts it: it listens again
            return "Saved."
        finally:
            bus.disconnect()

    background.run(restart(), told, lambda exc: told("Saved."))


class ArceeWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, from_keyring: set[str]) -> None:
        super().__init__(application=app, title="Arcee", default_width=1000, default_height=800)
        self._from_keyring = from_keyring
        self._syncing = False  # switches being set together, not by the user
        self._background = Background()
        self._settings = load()
        self._toasts = Adw.ToastOverlay()

        stack = self._stack = Adw.ViewStack()
        stack.add_titled_with_icon(AgentPage(), "agent", "Agent", "starred-symbolic")
        stack.add_titled_with_icon(
            self._models_page(), "models", "Models", "applications-science-symbolic"
        )
        stack.add_titled_with_icon(
            self._history_page(), "history", "History", "document-open-recent-symbolic"
        )
        self._memory = MemoryPage(
            self._background.run,
            self._toast_with,
            learning=self._settings.memory,
            on_learning=self._on_learning,
        )
        stack.add_titled_with_icon(self._memory, "memory", "Memory", "bookmark-new-symbolic")
        stack.add_titled_with_icon(
            self._permissions_page(), "permissions", "Permissions", "security-medium-symbolic"
        )
        stack.add_titled_with_icon(
            self._settings_page(), "settings", "Settings", "emblem-system-symbolic"
        )

        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Run setup", "app.setup")
        menu.append("About Arcee", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        header.set_title_widget(Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(stack)
        self._toasts.set_child(view)
        self.set_content(self._toasts)

        self._refresh_budget()
        GLib.timeout_add_seconds(BUDGET_REFRESH_S, self._refresh_budget)

    @property
    def settings(self) -> Any:
        return self._settings

    def show_page(self, name: str) -> None:
        name = {"diagnostics": "settings"}.get(name, name)  # Diagnostics is in Settings
        if self._stack.get_child_by_name(name) is not None:
            self._stack.set_visible_child_name(name)

    def turn_on_extension_once(self) -> None:
        """After the login that loaded the extension, turn it on (once) by ourselves.

        The .deb installs it for every user, but GNOME loads new extensions only at
        login and starts them off; people did not know to turn it on.
        """
        if self._settings.extension_turned_on:
            return

        async def turn_on() -> str:
            import asyncio

            info = await diagnostics.extension_info()
            if diagnostics.is_on(info):
                return "on"
            if diagnostics.is_off(info):
                return (
                    "turned on" if await asyncio.to_thread(install.enable_shell_extension) else ""
                )
            return ""

        def done(result: str) -> None:
            if not result:
                return  # not loaded yet (no login since installing): next time
            self._settings.extension_turned_on = True
            self._save_settings()
            if result == "turned on":
                self._toast(
                    "Turned on Arcee's GNOME Shell extension: look for its icon in the top bar."
                )
                self._run_checks()

        self._background.run(turn_on(), done)

    def show_setup(self) -> None:
        def done() -> None:
            self._settings.setup_done = True
            self._save_settings()
            self._show_key_states()
            self._run_checks()
            self._voice_group.refresh()  # setup may have turned on and trained the wake word

        SetupDialog(self._background.run, done, self._settings, self._saved).present(self)

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def _toast_with(self, text: str, undo: Callable[[], None] | None) -> None:
        """A toast, with Undo when `undo` is given."""
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=5 if undo else 3)
        if undo is not None:
            toast.set_button_label("Undo")
            toast.connect("button-clicked", lambda _t: undo())
        self._toasts.add_toast(toast)

    def _on_learning(self, on: bool) -> None:
        if self._settings.memory != on:
            self._settings.memory = on
            self._saved()  # arceed starts again with the new setting when idle

    def _save_settings(self) -> None:
        # "Listen for Hey Arcee" and "Always allow" as arceed keeps them (the panel and
        # the pill change them too), not as this window last saw them.
        saved = load()
        self._settings.listen = saved.listen
        self._settings.always_allow = saved.always_allow
        save(self._settings)

    def _saved(self) -> None:
        self._save_settings()
        restart_daemon_when_idle(self._background, self._toast)

    # --- Models ---------------------------------------------------------------------

    def _models_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        page.add(self._keys_group())
        thinking = Adw.PreferencesGroup(
            title="Thinking", description="Which models read your requests and decide."
        )
        first: Gtk.CheckButton | None = None
        self._core_checks: dict[str, Gtk.CheckButton] = {}
        for key, title, about in CORE_CHOICES:
            check = Gtk.CheckButton(active=self._settings.core == key, group=first)
            first = first or check
            self._core_checks[key] = check
            check.connect("toggled", self._on_core, key)
            row = Adw.ActionRow(title=title, subtitle=about, activatable_widget=check)
            row.add_prefix(check)
            thinking.add(row)
        page.add(thinking)

        self._budget = Adw.PreferencesGroup(
            title="Today", description="Groq's free allowance per model; it refills over the day."
        )
        reload = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Check now", valign=Gtk.Align.CENTER
        )
        reload.add_css_class("flat")
        reload.connect("clicked", lambda _b: self._refresh_budget())
        self._budget.set_header_suffix(reload)
        self._budget_rows: dict[str, tuple[Adw.ActionRow, Gtk.LevelBar]] = {}
        for model in GROQ_MODELS:
            bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=140)
            row = Adw.ActionRow(title=model.split("/")[-1], subtitle="No use recorded yet")
            row.add_suffix(bar)
            self._budget.add(row)
            self._budget_rows[model] = (row, bar)
        page.add(self._budget)

        speech = Adw.PreferencesGroup(
            title="Groq speech allowance",
            description="When speech goes to Groq: whisper-large-v3-turbo, on its own key and "
            "allowance.",
        )
        self._speech_bar = Gtk.LevelBar(
            min_value=0, max_value=1, value=1, valign=Gtk.Align.CENTER, width_request=140
        )
        self._speech_row = Adw.ActionRow(title="whisper-large-v3-turbo", subtitle="No use yet")
        self._speech_row.add_suffix(self._speech_bar)
        speech.add(self._speech_row)
        page.add(speech)
        return page

    def _keys_group(self) -> Adw.PreferencesGroup:
        self._keys = Adw.PreferencesGroup(
            title="Keys", description="Kept in your GNOME Keyring, encrypted with your login."
        )
        self._key_rows: list[Gtk.Widget] = []
        self._show_key_states()
        return self._keys

    def _show_key_states(self) -> None:
        """A set key shows as set, with Change and Remove; a missing one asks for it."""
        for row in self._key_rows:
            self._keys.remove(row)
        self._key_rows = []
        for key in KEYS:
            where = source(key.env, self._from_keyring)
            if where == "missing":
                self._add_key_entry(key.title, key.env, key.get_url)
                continue
            row = Adw.ActionRow(title=key.title, subtitle="Set", tooltip_text=key.purpose)
            done = Gtk.Image(icon_name="object-select-symbolic")
            done.add_css_class("success")
            row.add_prefix(done)
            change = Gtk.Button(label="Change", valign=Gtk.Align.CENTER)
            change.add_css_class("flat")
            change.connect("clicked", lambda _b, k=key: self._change_key(k))
            row.add_suffix(change)
            if where == "keyring":
                remove = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
                remove.add_css_class("flat")
                remove.connect("clicked", lambda _b, env=key.env: self._save_key(env, ""))
                row.add_suffix(remove)
            self._keys.add(row)
            self._key_rows.append(row)

    def _add_key_entry(self, title: str, env: str, url: str) -> None:
        entry = Adw.PasswordEntryRow(title=title, show_apply_button=True)
        link = Gtk.LinkButton(uri=url, label="Get a key", valign=Gtk.Align.CENTER)
        entry.add_suffix(link)
        entry.connect("apply", lambda row: self._save_key(env, row.get_text().strip()))
        self._keys.add(entry)
        self._key_rows.append(entry)

    def _change_key(self, key: Any) -> None:
        """Swap the key's row for a field to enter a new one."""
        self._show_key_states()
        index = next(i for i, k in enumerate(KEYS) if k.env == key.env)
        for row in self._key_rows[index:]:
            self._keys.remove(row)
        rest = self._key_rows[index + 1 :]
        self._key_rows = self._key_rows[:index]
        self._add_key_entry(f"New {key.title.lower()}", key.env, key.get_url)
        for row in rest:
            self._keys.add(row)
            self._key_rows.append(row)

    def _save_key(self, env: str, value: str) -> None:
        ring = keyring()
        if ring is None:
            self._toast("No keyring here: put the key in ~/.config/arcee/env instead.")
            return

        async def write() -> None:
            # Off the GTK thread: on a new account GNOME first asks for a password for the
            # new keyring, and waiting for that here froze the app ("not responding").
            import asyncio

            if value:
                await asyncio.to_thread(ring.store, env, value)
            else:
                await asyncio.to_thread(ring.clear, env)

        def saved(_none: None) -> None:
            if value:
                os.environ[env] = value
                self._from_keyring.add(env)
            else:
                self._from_keyring.discard(env)
                os.environ.pop(env, None)
            self._show_key_states()

        self._background.run(
            write(), saved, lambda exc: self._toast(f"Could not save the key: {exc}")
        )
        self._toast("Key saved." if value else "Key removed.")
        restart_daemon_when_idle(self._background, lambda _text: None)

    def _on_core(self, check: Gtk.CheckButton, key: str) -> None:
        if not check.get_active() or self._settings.core == key:
            return
        if key != "local":
            self._use_core(key)
            return

        # Local needs Ollama and the model: if either is missing, set them up first.
        def checked(status: local_models.LocalStatus) -> None:
            if status.ready:
                self._use_core("local")
                return
            LocalSetupDialog(
                self._background.run,
                on_ready=lambda: self._use_core("local"),
                on_cancel=lambda: self._core_checks[self._settings.core].set_active(True),
            ).present(self)

        self._background.run(local_models.status(), checked)

    def _use_core(self, key: str) -> None:
        self._settings.core = cast(Core, key)
        self._core_checks[key].set_active(True)
        self._saved()

    def _refresh_budget(self) -> bool:
        self._refresh_speech()
        snapshot = read_budget().snapshot()
        for model, (row, bar) in self._budget_rows.items():
            counts = snapshot.get(model)
            if not counts:
                continue
            parts = []
            if "day_tokens_limit" in counts:
                left, limit = counts["day_tokens_left"], counts["day_tokens_limit"]
                parts.append(f"{left / 1000:.0f}K of {limit / 1000:.0f}K tokens left today")
                bar.set_value(left / limit if limit else 0)
            if "requests_limit" in counts:
                parts.append(
                    f"{counts['requests_left']:.0f} of {counts['requests_limit']:.0f} requests"
                )
            if "tokens_limit" in counts:
                parts.append(f"{counts['tokens_left']:.0f} tokens this minute")
            row.set_subtitle(" · ".join(parts))
        return GLib.SOURCE_CONTINUE

    def _refresh_speech(self) -> None:
        use = SpeechUsage().snapshot()
        if not use["requests_day"]:
            return
        day_left = use["audio_s_day_limit"] - use["audio_s_day"]
        hour_left = use["audio_s_hour_limit"] - use["audio_s_hour"]
        self._speech_row.set_subtitle(
            f"{day_left / 60:.0f} of {use['audio_s_day_limit'] / 60:.0f} min of audio left "
            f"today · {hour_left / 60:.0f} min this hour · {use['requests_day']:.0f} of "
            f"{use['requests_day_limit']:.0f} requests"
        )
        self._speech_bar.set_value(max(0.0, day_left / use["audio_s_day_limit"]))

    # --- History --------------------------------------------------------------------

    def _history_page(self) -> Gtk.Widget:
        self._history = Adw.NavigationView()
        self._history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._history_list.add_css_class("boxed-list")
        self._history_list.connect("row-activated", self._on_task)
        clamp = Adw.Clamp(
            child=self._history_list,
            margin_top=18,
            margin_bottom=18,
            margin_start=12,
            margin_end=12,
        )
        scroll = Gtk.ScrolledWindow(child=clamp, vexpand=True)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda _b: self._load_history())
        clear = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete all history")
        clear.connect("clicked", lambda _b: self._confirm_delete_all())
        bar = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        bar.pack_end(refresh)
        bar.pack_end(clear)
        view = Adw.ToolbarView(content=scroll)
        view.add_top_bar(bar)
        self._history.add(Adw.NavigationPage(child=view, title="Recent requests"))
        self._load_history()
        return self._history

    def _load_history(self) -> None:
        async def read() -> list[Any]:
            store = Store(data_dir())
            try:
                return await store.recent(HISTORY_LIMIT)
            finally:
                store.close()

        self._background.run(read(), self._show_history)

    def _show_history(self, tasks: list[Any]) -> None:
        self._history_list.remove_all()
        for task in tasks:
            when = time.strftime("%d %b %H:%M", time.localtime(task.started))
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(task.request[:90]),
                subtitle=f"{task.status} · {when} · {task.steps} steps",
                activatable=True,
            )
            row.task_id = task.id  # type: ignore[attr-defined]
            delete = Gtk.Button(
                icon_name="user-trash-symbolic",
                tooltip_text="Delete this request",
                valign=Gtk.Align.CENTER,
            )
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, task_id=task.id: self._delete_history([task_id]))
            row.add_suffix(delete)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            self._history_list.append(row)
        if not tasks:
            self._history_list.append(Adw.ActionRow(title="No requests yet"))

    def _on_task(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task_id = getattr(row, "task_id", None)
        if task_id is None:
            return

        async def read() -> Any:
            store = Store(data_dir())
            try:
                return await store.task(task_id)
            finally:
                store.close()

        self._background.run(read(), self._show_task)

    def _show_task(self, found: Any) -> None:
        if found is None:
            return
        task, steps = found
        page = Adw.PreferencesPage()
        reply = Adw.PreferencesGroup(title="Reply")
        label = Gtk.Label(wrap=True, xalign=0, selectable=True)
        show_markdown(label, task.reply or task.status)
        reply.add(label)
        page.add(reply)
        group = Adw.PreferencesGroup(title=f"{len(steps)} steps")
        for step in steps:
            mark = "✓" if step.status == "ok" else "✗"
            asked = " · you allowed it" if step.approved_by == "user" else ""
            group.add(
                Adw.ActionRow(
                    title=GLib.markup_escape_text(f"{mark} {step.summary}"[:120]),
                    subtitle=f"{step.tool} · {step.duration_ms} ms{asked}",
                )
            )
        page.add(group)
        view = Adw.ToolbarView(content=page)
        bar = Adw.HeaderBar(show_end_title_buttons=False)
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete this request")
        delete.connect("clicked", lambda _b: self._delete_history([task.id], back=True))
        bar.pack_end(delete)
        view.add_top_bar(bar)
        self._history.push(Adw.NavigationPage(child=view, title=task.request[:60]))
        # A selectable label selects all its text when it first takes focus; undo that.
        GLib.idle_add(lambda: label.select_region(0, 0) and False)

    def _confirm_delete_all(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete all history?",
            body="Every request, the steps Arcee took and what it saw are deleted. "
            "What it learned about you stays in Memory, where you can delete it too.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete all")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response: self._delete_history(None) if response == "delete" else None,
        )
        dialog.present(self)

    def _delete_history(self, task_ids: list[str] | None, back: bool = False) -> None:
        """Delete these requests (None: all of them) from History."""

        async def delete() -> int:
            store = Store(data_dir())
            try:
                return await store.delete_tasks(task_ids)
            finally:
                store.close()

        def done(deleted: int) -> None:
            if back:
                self._history.pop()
            self._load_history()
            self._toast("Deleted the request." if deleted == 1 else f"Deleted {deleted} requests.")

        self._background.run(delete(), done)

    # --- Permissions ----------------------------------------------------------------

    def _permissions_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        asking = Adw.PreferencesGroup(
            title="How Arcee asks",
            description="Arcee never needs your permission to read or look. Sending, "
            "paying, deleting for good, signing out and running scripts always ask, "
            "whatever is set here.",
        )
        all_row = Adw.SwitchRow(
            title="Ask before every action",
            subtitle="Even opening apps and clicking: slower, but you see every step",
            active=self._settings.confirm_all,
        )
        all_row.connect("notify::active", self._on_confirm_all)
        asking.add(all_row)
        page.add(asking)

        allowed = Adw.PreferencesGroup(
            title="Allowed without asking",
            description="Routine actions that otherwise ask each time, and are easily "
            "undone. “Always allow”, when Arcee asks, turns them on here too.",
        )
        self._allow_rows: dict[str, Adw.SwitchRow] = {}
        for tool, title in ALLOWABLE.items():
            row = Adw.SwitchRow(title=title, active=tool in self._settings.always_allow)
            row.connect("notify::active", self._on_always_allow, tool)
            allowed.add(row)
            self._allow_rows[tool] = row
        page.add(allowed)

        always = Adw.PreferencesGroup(
            title="Ask before these too",
            description="They run without asking unless you turn them on here.",
        )
        self._ask_all = Adw.SwitchRow(
            title="All of these",
            active=all(tool in self._settings.always_ask for tool, _, _ in ASKABLE),
        )
        self._ask_all.connect("notify::active", self._on_ask_all)
        always.add(self._ask_all)
        self._ask_rows: dict[str, Adw.SwitchRow] = {}
        for tool, title, about in ASKABLE:
            row = Adw.SwitchRow(
                title=title, subtitle=about, active=tool in self._settings.always_ask
            )
            row.connect("notify::active", self._on_always_ask, tool)
            always.add(row)
            self._ask_rows[tool] = row
        page.add(always)

        # Personal data to a cloud model (decision 0017): a privacy choice, not an action's.
        privacy = Adw.PreferencesGroup(
            title="Privacy",
            description="To answer about your mail, calendar or contacts, the model reads them. "
            "On a local model they stay on this computer; on Groq they are sent to Groq.",
        )
        sharing = Adw.ComboRow(
            title="Mail, calendar and contacts to a cloud model",
            model=Gtk.StringList.new([label for _, label in SHARING_CHOICES]),
        )
        values = [value for value, _ in SHARING_CHOICES]
        sharing.set_selected(values.index(self._settings.personal_to_cloud))
        sharing.connect("notify::selected", self._on_sharing)
        privacy.add(sharing)
        page.add(privacy)
        page.connect("map", lambda _p: self._show_always_allow())
        return page

    def _show_always_allow(self) -> None:
        """As arceed keeps it: "Always allow" in the pill may have added one."""
        saved = set(load().always_allow)
        self._syncing = True
        for tool, row in self._allow_rows.items():
            row.set_active(tool in saved)
        self._syncing = False

    def _on_always_allow(self, row: Adw.SwitchRow, _param: Any, tool: str) -> None:
        if self._syncing:
            return
        allows = set(load().always_allow)
        if row.get_active():
            allows.add(tool)
        else:
            allows.discard(tool)
        save_always_allow(sorted(allows))
        self._settings.always_allow = sorted(allows)
        restart_daemon_when_idle(self._background, self._toast)

    def _on_sharing(self, row: Adw.ComboRow, _param: Any) -> None:
        value = SHARING_CHOICES[row.get_selected()][0]
        if value != self._settings.personal_to_cloud:
            self._settings.personal_to_cloud = value
            self._saved()

    def _on_ask_all(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self._syncing:
            return
        on = row.get_active()
        self._syncing = True
        for each in self._ask_rows.values():
            each.set_active(on)
        self._syncing = False
        self._settings.always_ask = sorted(self._ask_rows) if on else []
        self._saved()

    def _on_confirm_all(self, row: Adw.SwitchRow, _param: Any) -> None:
        self._settings.confirm_all = row.get_active()
        self._saved()

    def _on_always_ask(self, row: Adw.SwitchRow, _param: Any, tool: str) -> None:
        if self._syncing:
            return
        asks = set(self._settings.always_ask)
        if row.get_active():
            asks.add(tool)
        else:
            asks.discard(tool)
        self._settings.always_ask = sorted(asks)
        self._syncing = True
        self._ask_all.set_active(all(r.get_active() for r in self._ask_rows.values()))
        self._syncing = False
        self._saved()

    # --- Settings -------------------------------------------------------------------

    def _settings_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self._voice_group = VoiceGroup(
            self._background.run, self._toast, self._settings, self._saved
        )
        page.add(self._voice_group)

        data = Adw.PreferencesGroup(
            title="Your data",
            description="Your history, memory and voice samples stay on this computer.",
        )
        folder = Adw.ActionRow(title="Arcee's folder", subtitle=str(data_dir()))
        open_folder = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_folder.connect(
            "clicked", lambda _b: Gio.AppInfo.launch_default_for_uri(data_dir().as_uri(), None)
        )
        folder.add_suffix(open_folder)
        data.add(folder)
        clear = Adw.ActionRow(
            title="Delete all history",
            subtitle="Every request and what Arcee did; Memory is kept",
        )
        delete = Gtk.Button(label="Delete…", valign=Gtk.Align.CENTER)
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda _b: self._confirm_delete_all())
        clear.add_suffix(delete)
        data.add(clear)
        page.add(data)

        setup = Adw.PreferencesGroup(title="Setup")
        again = Adw.ActionRow(
            title="Run setup again", subtitle="Keys, voice, the browser and the top bar"
        )
        run = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
        run.connect("clicked", lambda _b: self.show_setup())
        again.add_suffix(run)
        setup.add(again)
        page.add(setup)

        self._checks = Adw.PreferencesGroup(
            title="Diagnostics", description="Arcee's parts: what each does, and how to fix it."
        )
        again = Gtk.Button(label="Check again", valign=Gtk.Align.CENTER)
        again.connect("clicked", lambda _b: self._run_checks())
        self._checks.set_header_suffix(again)
        page.add(self._checks)
        self._check_rows: list[Gtk.Widget] = []
        self._run_checks()
        return page

    def _run_checks(self) -> None:
        self._background.run(diagnostics.run_all(), self._show_checks)

    def _show_checks(self, checks: list[diagnostics.Check]) -> None:
        for row in self._check_rows:
            self._checks.remove(row)
        self._check_rows = []
        for check in checks:
            icon, style = (
                ("object-select-symbolic", "success")
                if check.ok
                else ("dialog-warning-symbolic", "warning")
                if check.advisory
                else ("dialog-error-symbolic", "error")
            )
            image = Gtk.Image(icon_name=icon)
            image.add_css_class(style)
            subtitle = check.detail + (f"\nFix: {check.fix}" if check.fix else "")
            row = Adw.ActionRow(title=check.name, subtitle=GLib.markup_escape_text(subtitle))
            row.add_prefix(image)
            if not check.ok and check.fix_action:
                fix = Gtk.Button(label="Fix", valign=Gtk.Align.CENTER)
                fix.connect("clicked", self._on_fix, check.fix_action)
                row.add_suffix(fix)
            self._checks.add(row)
            self._check_rows.append(row)

    def _on_fix(self, button: Gtk.Button, action: str) -> None:
        if action == "keys":
            self._stack.set_visible_child_name("models")
            return
        installers = {
            "shell_extension": install.shell_extension,
            "daemon": install.daemon,
            "browser_bridge": install.browser_bridge,
        }
        button.set_sensitive(False)

        async def run() -> install.Report:
            import asyncio

            report = await asyncio.to_thread(installers[action])
            # Installed before but not turned on: turning it on may be all that is left.
            if action == "shell_extension":
                await asyncio.to_thread(install.enable_shell_extension)
            return report

        def done(report: install.Report) -> None:
            if action == "browser_bridge":
                self._chrome_steps(report)
            else:
                self._toast(report.next[0] if report.next else "Done.")
            self._run_checks()

        self._background.run(run(), done, lambda exc: self._toast(f"Could not fix it: {exc}"))

    def _chrome_steps(self, report: install.Report) -> None:
        """Chrome loads an unpacked extension only by hand: show every step, not a toast
        with the first line, and put the folder on the clipboard for its file picker."""
        from arcee.browser.setup import EXTENSION_DIR

        self.get_clipboard().set(str(EXTENSION_DIR))
        dialog = Adw.AlertDialog(
            heading="Load Arcee in Chrome",
            body="\n".join(
                [
                    *report.lines,
                    "",
                    *report.next,
                    "",
                    "The folder is copied: in the picker press Ctrl+L and paste.",
                ]
            ),
        )
        dialog.add_response("close", "Close")
        dialog.add_response("open", "Open Chrome's extensions")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)

        def respond(_dialog: Any, response: str) -> None:
            if response == "open":
                subprocess.Popen(
                    ["google-chrome", "chrome://extensions"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

        dialog.connect("response", respond)
        dialog.present(self)


class ArceeApp(Adw.Application):
    def __init__(self, from_keyring: set[str]) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        # `arcee-app --page=history` opens on that page; a second launch reaches the first.
        self.add_main_option(
            "page", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING, "Page to open", "PAGE"
        )
        self.from_keyring = from_keyring
        about = Gio.SimpleAction(name="about")
        about.connect("activate", lambda *_: self._about())
        self.add_action(about)
        setup = Gio.SimpleAction(name="setup")
        setup.connect("activate", lambda *_: self._window().show_setup())
        self.add_action(setup)

    def _window(self) -> ArceeWindow:
        window = self.get_active_window()
        return window if isinstance(window, ArceeWindow) else ArceeWindow(self, self.from_keyring)

    def _about(self) -> None:
        from importlib.metadata import version

        Adw.AboutDialog(
            application_icon=APP_ID,
            application_name="Arcee",
            developer_name="Itesh Tomar",
            developers=["Itesh Tomar https://iteshxt.me"],
            version=version("arcee"),
            website="https://iteshxt.me",
            issue_url="https://github.com/iteshxt/arcee-agent/issues",
            comments="An agent for your Ubuntu desktop: ask in words or by voice, and it works "
            "in your apps, your Chrome and your files, asking before anything risky.",
            copyright="© 2026 Itesh Tomar",
        ).present(self.get_active_window())

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Run from a checkout the icon is not in the icon theme; art/ holds it by name.
        art = Path(__file__).resolve().parents[2] / "art"
        display = Gdk.Display.get_default()
        if art.is_dir() and display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(art))
        Gtk.Window.set_default_icon_name(APP_ID)

    def do_activate(self) -> None:
        window = self._window()
        window.present()
        if not window.settings.setup_done:  # first run
            window.show_setup()
        else:
            window.turn_on_extension_once()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        self.activate()
        if page := options.get("page"):
            self._window().show_page(page)
        return 0


def main() -> None:
    import faulthandler
    import logging

    from arcee.config import load_env

    # What went wrong, kept for a report: warnings and errors, and the stack of every
    # thread if Python itself crashes (GTK or a C library underneath).
    # Logging never stops the app: with no writable data folder it logs to stderr.
    logs = data_dir() / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        crash_log = open(logs / "app-crash.log", "a")  # noqa: SIM115 - open for life
        log_file: str | None = str(logs / "app.log")
    except OSError:
        crash_log, log_file = sys.stderr, None
    logging.basicConfig(
        filename=log_file,
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    def report(kind: Any, value: Any, trace: Any) -> None:
        logging.getLogger("arcee-app").error("uncaught", exc_info=(kind, value, trace))
        sys.__excepthook__(kind, value, trace)

    sys.excepthook = report
    faulthandler.enable(crash_log)
    ArceeApp(load_env()).run(sys.argv)
