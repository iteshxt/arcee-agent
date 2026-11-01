# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The Arcee app (phase 4): models and today's budget, history, permissions, diagnostics.

GTK 4 and libadwaita. Slow work (reading the history, running the checks) runs on a
background asyncio loop and comes back to GTK with GLib.idle_add, so the window never
freezes. Settings changes are saved to ~/.config/arcee/settings.json and arceed is
restarted when it is idle, so the next request uses them.
"""

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, Final, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from arcee.app import diagnostics  # noqa: E402
from arcee.context.store import Store, data_dir  # noqa: E402
from arcee.models.presets import GROQ_MODELS, read_budget  # noqa: E402
from arcee.settings import Core, load, save  # noqa: E402
from arcee.voice import SpeechUsage  # noqa: E402

APP_ID: Final = "me.iteshxt.Arcee"  # under the developer's own domain, iteshxt.me
BUDGET_REFRESH_S: Final = 5
HISTORY_LIMIT: Final = 50
# Automatic actions the user may make ask first, in their words. Deleting, sending,
# paying and the like ask whatever is set here.
ASKABLE: Final = (
    ("browser.click", "Clicking in Chrome"),
    ("browser.fill", "Typing into web pages"),
    ("ui.act", "Acting in app windows"),
    ("screen.click", "Clicking on the screen"),
    ("kbd.type", "Typing on the keyboard"),
    ("app.open", "Opening apps"),
    ("fs.write", "Writing files"),
    ("fs.move", "Moving and renaming files"),
    ("shell.run", "Running commands"),
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
    ) -> None:
        future = asyncio.run_coroutine_threadsafe(work, self._loop)

        def finish(f: Any) -> None:
            exc = f.exception()
            GLib.idle_add(lambda: (failed(exc) if exc else done(f.result())) and False)

        future.add_done_callback(finish)


def restart_daemon_when_idle(background: Background, told: Callable[[str], None]) -> None:
    """Ask arceed to quit if it is idle; D-Bus starts it again with the new settings."""

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
            return "Saved."
        finally:
            bus.disconnect()

    background.run(restart(), told, lambda exc: told("Saved."))


class ArceeWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Arcee", default_width=760, default_height=640)
        self._background = Background()
        self._settings = load()
        self._toasts = Adw.ToastOverlay()

        stack = Adw.ViewStack()
        stack.add_titled_with_icon(
            self._models_page(), "models", "Models", "applications-science-symbolic"
        )
        stack.add_titled_with_icon(
            self._history_page(), "history", "History", "document-open-recent-symbolic"
        )
        stack.add_titled_with_icon(
            self._permissions_page(), "permissions", "Permissions", "security-medium-symbolic"
        )
        stack.add_titled_with_icon(
            self._diagnostics_page(), "diagnostics", "Diagnostics", "emblem-system-symbolic"
        )

        header = Adw.HeaderBar()
        menu = Gio.Menu()
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

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def _saved(self) -> None:
        save(self._settings)
        restart_daemon_when_idle(self._background, self._toast)

    # --- Models ---------------------------------------------------------------------

    def _models_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        thinking = Adw.PreferencesGroup(
            title="Thinking", description="Which models read your requests and decide."
        )
        first: Gtk.CheckButton | None = None
        for key, title, about in CORE_CHOICES:
            check = Gtk.CheckButton(active=self._settings.core == key, group=first)
            first = first or check
            check.connect("toggled", self._on_core, key)
            row = Adw.ActionRow(title=title, subtitle=about, activatable_widget=check)
            row.add_prefix(check)
            thinking.add(row)
        page.add(thinking)

        self._budget = Adw.PreferencesGroup(
            title="Today", description="Groq's free allowance per model; it refills over the day."
        )
        self._budget_rows: dict[str, tuple[Adw.ActionRow, Gtk.LevelBar]] = {}
        for model in GROQ_MODELS:
            bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=140)
            row = Adw.ActionRow(title=model.split("/")[-1], subtitle="No use recorded yet")
            row.add_suffix(bar)
            self._budget.add(row)
            self._budget_rows[model] = (row, bar)
        page.add(self._budget)

        speech = Adw.PreferencesGroup(
            title="Speech",
            description="whisper-large-v3-turbo on Groq turns what you say to the panel's mic "
            "into a request, on its own key and allowance.",
        )
        self._speech_bar = Gtk.LevelBar(
            min_value=0, max_value=1, value=1, valign=Gtk.Align.CENTER, width_request=140
        )
        self._speech_row = Adw.ActionRow(title="whisper-large-v3-turbo", subtitle="No use yet")
        self._speech_row.add_suffix(self._speech_bar)
        speech.add(self._speech_row)
        page.add(speech)
        return page

    def _on_core(self, check: Gtk.CheckButton, key: str) -> None:
        if check.get_active() and self._settings.core != key:
            self._settings.core = cast(Core, key)
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
        bar = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        bar.pack_end(refresh)
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
        label = Gtk.Label(label=task.reply or task.status, wrap=True, xalign=0, selectable=True)
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
        view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False))
        self._history.push(Adw.NavigationPage(child=view, title=task.request[:60]))
        # A selectable label selects all its text when it first takes focus; undo that.
        GLib.idle_add(lambda: label.select_region(0, 0) and False)

    # --- Permissions ----------------------------------------------------------------

    def _permissions_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        everything = Adw.PreferencesGroup(
            description="Deleting, sending, paying, signing out and similar always ask first."
        )
        all_row = Adw.SwitchRow(
            title="Ask before every action",
            subtitle="Arcee asks you before it does anything, even reading.",
            active=self._settings.confirm_all,
        )
        all_row.connect("notify::active", self._on_confirm_all)
        everything.add(all_row)
        page.add(everything)

        always = Adw.PreferencesGroup(
            title="Always ask before", description="Actions that otherwise run without asking."
        )
        for tool, title in ASKABLE:
            row = Adw.SwitchRow(
                title=title, subtitle=tool, active=tool in self._settings.always_ask
            )
            row.connect("notify::active", self._on_always_ask, tool)
            always.add(row)
        page.add(always)
        return page

    def _on_confirm_all(self, row: Adw.SwitchRow, _param: Any) -> None:
        self._settings.confirm_all = row.get_active()
        self._saved()

    def _on_always_ask(self, row: Adw.SwitchRow, _param: Any, tool: str) -> None:
        asks = set(self._settings.always_ask)
        if row.get_active():
            asks.add(tool)
        else:
            asks.discard(tool)
        self._settings.always_ask = sorted(asks)
        self._saved()

    # --- Diagnostics ----------------------------------------------------------------

    def _diagnostics_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self._checks = Adw.PreferencesGroup(
            title="Arcee's parts", description="What each part does, and how to fix it."
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
            self._checks.add(row)
            self._check_rows.append(row)


class ArceeApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        about = Gio.SimpleAction(name="about")
        about.connect("activate", lambda *_: self._about())
        self.add_action(about)

    def _about(self) -> None:
        from importlib.metadata import version

        Adw.AboutDialog(
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

    def do_activate(self) -> None:
        window = self.get_active_window() or ArceeWindow(self)
        window.present()


def main() -> None:
    from arcee.config import load_env

    load_env()
    ArceeApp().run(None)
