# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""First-run setup: what Arcee is, then each part put in place with a button.

Shown the first time the app opens and again from its menu. Each step says what the
part does, shows whether it is already in place, and installs it on a click with the
same installers arcee-cli uses (arcee/install.py). Nothing needs sudo.
"""

import subprocess
from collections.abc import Callable
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from arcee import install, voice_local  # noqa: E402
from arcee.app import diagnostics  # noqa: E402
from arcee.app.voice_group import set_wake_word  # noqa: E402
from arcee.app.wake_training import WakeTrainer  # noqa: E402
from arcee.browser.setup import EXTENSION_DIR  # noqa: E402
from arcee.keys import KEYS, keyring  # noqa: E402

DEVELOPER: Final = "Itesh Tomar"
WEBSITE: Final = "https://iteshxt.me"
APP_ICON: Final = "me.iteshxt.Arcee"

# Run work off the GTK thread; the callback gets its result back on it.
Runner = Callable[..., object]  # (work, done[, failed]), as Background.run


class SetupDialog(Adw.Dialog):
    def __init__(
        self,
        run: Runner,
        on_done: Callable[[], None],
        settings: Any = None,
        saved: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(title="Set up Arcee", content_width=560, content_height=640)
        self._run = run
        self._on_done = on_done
        self._settings = settings
        self._saved = saved
        self._trainer: WakeTrainer | None = None
        self._nav = Adw.NavigationView()
        self.set_child(self._nav)
        self._steps = [
            self._welcome(),
            self._keys(),
            self._desktop_control(),
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
            self._voice(),
            self._finish(),
        ]
        self._nav.add(self._steps[0])
        self.connect("closed", lambda _d: self._trainer and self._trainer.cancel())

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
        if self._trainer is not None:
            self._trainer.cancel()  # moving on is skipping it
        if index + 1 < len(self._steps):
            self._nav.push(self._steps[index + 1])
        else:
            self._on_done()
            self.close()

    def _welcome(self) -> Any:
        status = Adw.StatusPage(
            icon_name=APP_ICON,
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

    def _desktop_control(self) -> Any:
        """The GNOME Shell extension, with exactly what is left to do for it."""
        status = Adw.StatusPage(
            icon_name="preferences-desktop-display-symbolic",
            title="Desktop control",
            description="Arcee's GNOME Shell extension puts Arcee in the top bar, lets it see "
            "the screen, click and type, and shows the glow and a Stop button while it works.",
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        state = Gtk.Label(
            label="Checking…", wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60
        )
        button = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        button.add_css_class("pill")
        box.append(state)
        box.append(button)
        status.set_child(box)
        action: list[str] = [""]

        def show(check: diagnostics.Check) -> None:
            button.set_sensitive(True)
            if check.ok:
                state.set_label("✓ On and working: Arcee's icon is in the top bar.")
                button.set_visible(False)
            elif check.detail == "installed but turned off":
                state.set_label("Installed, but turned off.")
                action[0] = "enable"
                button.set_label("Turn it on")
                button.set_visible(True)
            elif check.detail.startswith("installed, but GNOME loads"):
                state.set_label(
                    "Installed. GNOME starts new extensions only when you log in, so finish "
                    "setup, then log out and back in once.\nWhen you open Arcee again it "
                    "turns the extension on by itself."
                )
                button.set_visible(False)
            elif check.detail == "not installed":
                state.set_label("Not installed yet.")
                action[0] = "install"
                button.set_label("Install")
                button.set_visible(True)
            else:
                state.set_label(f"{check.detail}. {check.fix}".strip())
                button.set_visible(False)

        def clicked(_button: Any) -> None:
            button.set_sensitive(False)

            async def work() -> diagnostics.Check:
                import asyncio

                if action[0] == "enable":
                    await _threaded(install.enable_shell_extension)
                else:
                    await _threaded(install.shell_extension)
                # GNOME takes a moment to start it: look again for up to 3 s.
                check = await diagnostics.shell_extension()
                for _ in range(10):
                    if check.ok or check.detail != "starting":
                        break
                    await asyncio.sleep(0.3)
                    check = await diagnostics.shell_extension()
                return check

            self._run(work(), show)

        button.connect("clicked", clicked)
        self._run(diagnostics.shell_extension(), show)
        return self._page("Desktop control", status, 2)

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

    def _voice(self) -> Any:
        """ "Hey Arcee": turned on, then trained on the user's voice, right here."""
        status = Adw.StatusPage(
            icon_name="audio-input-microphone-symbolic",
            title="“Hey Arcee”",
            description="Say “Hey Arcee” and ask, hands free. A small model on this computer "
            "listens for it; only what you say after it goes to Groq to become text. "
            "Everyone says it a little differently, so Arcee learns how you do.",
        )
        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        off = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        state = Gtk.Label(
            label="Off. Turning it on downloads 7 MB of voice models from sherpa-onnx on "
            "GitHub. You can skip this and turn it on later in Arcee's settings.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=56,
        )
        state.add_css_class("dim-label")
        turn_on = Gtk.Button(label="Turn on “Hey Arcee”", halign=Gtk.Align.CENTER)
        turn_on.add_css_class("pill")
        turn_on.add_css_class("suggested-action")
        progress = Gtk.ProgressBar(visible=False, width_request=240, halign=Gtk.Align.CENTER)
        for child in (state, turn_on, progress):
            off.append(child)
        self._trainer = WakeTrainer(self._run, icon=False)
        stack.add_named(off, "off")
        stack.add_named(self._trainer, "train")
        status.set_child(stack)

        def training() -> None:
            # Room for the trainer: the page's icon small, its words short.
            status.add_css_class("compact")
            status.set_description(
                "Everyone says it a little differently, so Arcee learns how you do."
            )
            stack.set_visible_child_name("train")

        def listening() -> None:
            if self._settings is not None and not self._settings.listen:
                self._settings.listen = True
                self._run(set_wake_word(True), lambda _none: None)  # arceed listens now
            training()

        def downloaded(_none: None) -> None:
            progress.set_visible(False)
            listening()

        def failed(exc: BaseException) -> None:
            progress.set_visible(False)
            turn_on.set_sensitive(True)
            state.set_label(f"Could not download: {exc}")

        def fetch() -> None:
            turn_on.set_sensitive(False)
            progress.set_visible(True)

            def report(fraction: float) -> None:
                GLib.idle_add(lambda: progress.set_fraction(fraction) and False)

            self._run(voice_local.download_missing(report), downloaded, failed)

        def clicked(_button: Any) -> None:
            if not voice_local.missing():
                listening()
                return
            size = sum(m.size_mb for m in voice_local.missing())
            dialog = Adw.AlertDialog(
                heading=f"Download {size} MB?",
                body="The wake word and voice detection models, from sherpa-onnx's releases "
                "on GitHub, kept on this computer.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("download", "Download")
            dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, r: r == "download" and fetch())
            dialog.present(self)

        turn_on.connect("clicked", clicked)
        if self._settings is not None and self._settings.listen and not voice_local.missing():
            training()
        else:
            stack.set_visible_child_name("off")
        return self._page("Voice", status, 5)

    def _finish(self) -> Any:
        status = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title="Ready",
            description="One last step on a new install: log out and back in, so GNOME "
            "starts Arcee's extension. Then open Arcee once (it turns the extension on), "
            "click Arcee's icon in the top bar, type or press the mic, and ask.\n\n"
            "Diagnostics in this app shows every part, with a Fix button where one is "
            "missing. Super+Shift+Escape always stops Arcee.",
        )
        logout = Gtk.Button(label="Log out now", halign=Gtk.Align.CENTER)
        logout.add_css_class("pill")
        logout.set_tooltip_text("GNOME asks before it logs you out")

        def log_out(_button: Any) -> None:
            self._on_done()  # setup counts as finished either way
            subprocess.Popen(
                ["gnome-session-quit", "--logout"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        logout.connect("clicked", log_out)
        status.set_child(logout)
        return self._page("Ready", status, 6, last=True)


async def _threaded(work: Callable[[], Any]) -> Any:
    import asyncio

    return await asyncio.to_thread(work)
