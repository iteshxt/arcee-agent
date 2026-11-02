# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""First-run setup: what Arcee is, then each part put in place with a button.

Shown the first time the app opens and again from its menu. Each step says what the
part does, shows whether it is already in place, and installs it on a click with the
same installers arcee-cli uses (arcee/install.py). Nothing needs sudo.
"""

from collections.abc import Callable
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from arcee import install  # noqa: E402
from arcee.app import diagnostics  # noqa: E402
from arcee.browser.setup import EXTENSION_DIR  # noqa: E402
from arcee.keys import KEYS, keyring  # noqa: E402

DEVELOPER: Final = "Itesh Tomar"
WEBSITE: Final = "https://iteshxt.me"

# Run work off the GTK thread; the callback gets its result back on it.
Runner = Callable[..., None]  # (work, done[, failed]), as Background.run


class SetupDialog(Adw.Dialog):
    def __init__(self, run: Runner, on_done: Callable[[], None]) -> None:
        super().__init__(title="Set up Arcee", content_width=560, content_height=620)
        self._run = run
        self._on_done = on_done
        self._nav = Adw.NavigationView()
        self.set_child(self._nav)
        self._steps = [
            self._welcome(),
            self._keys(),
            self._part(
                2,
                "Desktop control",
                "preferences-desktop-display-symbolic",
                "A GNOME Shell extension lets Arcee see the screen, click and type, and show "
                "the glow and Stop button while it works. GNOME loads it at your next login.",
                diagnostics.shell_extension,
                install.shell_extension,
            ),
            self._part(
                3,
                "Background service",
                "system-run-symbolic",
                "arceed runs your requests from the panel, so no terminal is needed. It starts "
                "when you first ask something and uses no resources while idle.",
                diagnostics.daemon,
                install.daemon,
            ),
            self._chrome(),
            self._finish(),
        ]
        self._nav.add(self._steps[0])

    # --- pages --------------------------------------------------------------------

    def _page(self, title: str, status: Adw.StatusPage, index: int, last: bool = False) -> Any:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        status.set_vexpand(True)
        box.append(status)
        next_button = Gtk.Button(
            label="Finish" if last else "Next", halign=Gtk.Align.CENTER, margin_bottom=24
        )
        next_button.add_css_class("pill")
        next_button.add_css_class("suggested-action")
        next_button.connect("clicked", lambda _b: self._next(index))
        box.append(next_button)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        return Adw.NavigationPage(child=view, title=title)

    def _next(self, index: int) -> None:
        if index + 1 < len(self._steps):
            self._nav.push(self._steps[index + 1])
        else:
            self._on_done()
            self.close()

    def _welcome(self) -> Any:
        status = Adw.StatusPage(
            icon_name="starred-symbolic",
            title="Welcome to Arcee",
            description=(
                "Arcee is an agent for your desktop. Ask in words or by voice from the panel "
                "at the top of the screen, and it works in your apps, your Chrome and your "
                "files, asking you first before anything risky.\n\n"
                f'Made by <a href="{WEBSITE}">{DEVELOPER}</a>.'
            ),
        )
        return self._page("Welcome", status, 0)

    def _keys(self) -> Any:
        status = Adw.StatusPage(
            icon_name="dialog-password-symbolic",
            title="Groq keys",
            description="Arcee thinks with models on Groq. A free key is enough; a second "
            "one for speech keeps the mic's allowance apart. Both stay in your keyring.",
        )
        group = Adw.PreferencesGroup()
        for key in KEYS:
            row = Adw.PasswordEntryRow(title=key.title, show_apply_button=True)
            row.add_suffix(
                Gtk.LinkButton(uri=key.get_url, label="Get a key", valign=Gtk.Align.CENTER)
            )
            row.connect("apply", self._save_key, key.env)
            group.add(row)
        status.set_child(Adw.Clamp(child=group, maximum_size=440))
        return self._page("Keys", status, 1)

    def _save_key(self, row: Any, env: str) -> None:
        ring = keyring()
        value = row.get_text().strip()
        if ring is None or not value:
            return
        row.set_sensitive(False)

        def saved(_none: None) -> None:
            row.set_text("")
            row.set_title(f"{row.get_title()} ✓ saved")
            row.set_sensitive(True)

        def failed(exc: BaseException) -> None:
            row.set_title(f"Could not save: {exc}")
            row.set_sensitive(True)

        # Off the GTK thread: a new keyring first asks for its password (see window.py).
        self._run(_threaded(lambda: ring.store(env, value)), saved, failed)

    def _part(
        self,
        index: int,
        title: str,
        icon: str,
        about: str,
        check: Callable[[], Any],
        installer: Callable[[], install.Report],
    ) -> Any:
        status = Adw.StatusPage(icon_name=icon, title=title, description=about)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        state = Gtk.Label(label="Checking…", wrap=True, justify=Gtk.Justification.CENTER)
        button = Gtk.Button(label="Install", halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        box.append(state)
        box.append(button)
        status.set_child(box)

        def show_check(result: diagnostics.Check) -> None:
            state.set_label("Already in place." if result.ok else "Not installed yet.")
            button.set_label("Install again" if result.ok else "Install")

        def installed(report: install.Report) -> None:
            state.set_label("\n".join(report.next) or "Done.")
            button.set_label("Install again")
            button.set_sensitive(True)

        def clicked(_button: Any) -> None:
            button.set_sensitive(False)
            state.set_label("Installing…")
            self._run(_threaded(installer), installed)

        button.connect("clicked", clicked)
        self._run(check(), show_check)
        return self._page(title, status, index)

    def _chrome(self) -> Any:
        status = Adw.StatusPage(
            icon_name="web-browser-symbolic",
            title="Your Chrome",
            description="Arcee works in your own signed-in Chrome through a small extension. "
            "Register it with Chrome here, then load it once: on chrome://extensions turn on "
            "Developer mode, choose Load unpacked, and pick the folder this opens.",
        )
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        register = Gtk.Button(label="Register with Chrome")
        folder = Gtk.Button(label="Show the extension's folder")
        for button in (register, folder):
            button.add_css_class("pill")
            box.append(button)
        state = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, margin_top=12)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(box)
        column.append(state)
        status.set_child(column)
        register.connect(
            "clicked",
            lambda _b: self._run(
                _threaded(install.browser_bridge),
                lambda report: state.set_label("Registered. Now load the extension once."),
            ),
        )
        folder.connect(
            "clicked",
            lambda _b: Gio.AppInfo.launch_default_for_uri(EXTENSION_DIR.as_uri(), None),
        )
        return self._page("Chrome", status, 4)

    def _finish(self) -> Any:
        status = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title="Ready",
            description="If you installed desktop control, log out and back in once. Then "
            "click Arcee's icon in the top bar, type or press the mic, and ask.\n\n"
            "Diagnostics in this app shows every part, with a Fix button where one is "
            "missing. Super+Shift+Escape always stops Arcee.",
        )
        return self._page("Ready", status, 5, last=True)


async def _threaded(work: Callable[[], Any]) -> Any:
    import asyncio

    return await asyncio.to_thread(work)
