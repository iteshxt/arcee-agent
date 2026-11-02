# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""Setting up local models when the user picks Local (Ollama) and they are not ready.

One row per part: Ollama itself, then each model. Each row says where it stands and
offers the one step left, asked first: installing Ollama opens a terminal with its
official command (it needs the user's password); a model downloads through Ollama
with a progress bar. The dialog looks again every few seconds, so finishing a step in
the terminal shows up here.
"""

import shutil
import subprocess
from collections.abc import Callable
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from arcee import local_models  # noqa: E402
from arcee.local_models import HELPFUL, INSTALL_COMMAND, NEEDED, LocalStatus  # noqa: E402

RECHECK_S: Final = 3
ABOUT: Final = {
    "lfm3-omni": "Arcee's local model: reads your requests, sees and hears. Needed.",
    "lfm3": "The same size with step-by-step thinking, for harder requests. Helpful.",
}
Runner = Callable[..., None]  # (work, done[, failed]), as Background.run


class LocalSetupDialog(Adw.Dialog):
    def __init__(self, run: Runner, on_ready: Callable[[], None], on_cancel: Callable[[], None]):
        super().__init__(title="Local models", content_width=560)
        self._run = run
        self._on_ready = on_ready
        self._on_cancel = on_cancel
        self._chosen = False
        self._pulling: set[str] = set()

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description="Local models run on this computer through Ollama: nothing you ask "
            "or show leaves it, and there is no daily limit. They are slower and less able "
            "than the Groq models."
        )
        self._ollama = Adw.ActionRow(title="Ollama", subtitle="Checking…")
        self._ollama_button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self._ollama_button.connect("clicked", lambda _b: self._fix_ollama())
        self._ollama.add_suffix(self._ollama_button)
        group.add(self._ollama)
        self._models: dict[str, tuple[Adw.ActionRow, Gtk.Button, Gtk.ProgressBar]] = {}
        for model in (*NEEDED, *HELPFUL):
            row = Adw.ActionRow(title=model, subtitle=ABOUT.get(model, ""))
            bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=120, visible=False)
            button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
            button.connect("clicked", lambda _b, m=model: self._confirm_pull(m))
            row.add_suffix(bar)
            row.add_suffix(button)
            group.add(row)
            self._models[model] = (row, button, bar)
        page.add(group)

        self._use = Gtk.Button(label="Use local models", sensitive=False, halign=Gtk.Align.CENTER)
        self._use.add_css_class("pill")
        self._use.add_css_class("suggested-action")
        self._use.connect("clicked", lambda _b: self._choose())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_bottom=18)
        box.append(page)
        box.append(self._use)
        page.set_vexpand(True)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.set_content_height(520)

        self.connect("closed", lambda _d: self._closed())
        self._timer = GLib.timeout_add_seconds(RECHECK_S, self._recheck)
        self._recheck()

    # --- looking --------------------------------------------------------------------

    def _recheck(self) -> bool:
        self._run(local_models.status(), self._show)
        return GLib.SOURCE_CONTINUE

    def _show(self, status: LocalStatus) -> None:
        if not status.installed:
            self._ollama.set_subtitle("Not installed. It installs with one command.")
            self._ollama_button.set_label("Install…")
            self._ollama_button.set_visible(True)
        elif not status.running:
            self._ollama.set_subtitle("Installed, but not running.")
            self._ollama_button.set_label("Start it")
            self._ollama_button.set_visible(True)
        else:
            self._ollama.set_subtitle("✓ Running")
            self._ollama_button.set_visible(False)
        for model, (row, button, bar) in self._models.items():
            if model in self._pulling:
                continue
            bar.set_visible(False)
            if status.has(model):
                row.set_subtitle(f"✓ Downloaded. {ABOUT.get(model, '')}")
                button.set_visible(False)
            else:
                row.set_subtitle(ABOUT.get(model, ""))
                button.set_label("Download…")
                button.set_visible(status.running)
        self._use.set_sensitive(status.ready)

    # --- steps ------------------------------------------------------------------------

    def _fix_ollama(self) -> None:
        if self._ollama_button.get_label() == "Start it":
            # systemd asks for the password itself (polkit), outside this window.
            subprocess.Popen(["systemctl", "start", "ollama"], start_new_session=True)
            return
        dialog = Adw.AlertDialog(
            heading="Install Ollama?",
            body="Ollama's official installer runs in a terminal and asks for your password:\n\n"
            f"{INSTALL_COMMAND}\n\nIt downloads Ollama from ollama.com (about 1 GB with its "
            "GPU support) and starts it as a service. This window notices when it is done.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Open a terminal and install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "install" and _in_terminal(INSTALL_COMMAND))
        dialog.present(self)

    def _confirm_pull(self, model: str) -> None:
        def ask(size: int | None) -> None:
            dialog = Adw.AlertDialog(
                heading=f"Download {model}?",
                body=f"Ollama downloads {model} ({local_models.gigabytes(size)}) from "
                "ollama.com and keeps it on this computer.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("download", "Download")
            dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, r: r == "download" and self._pull(model))
            dialog.present(self)

        self._run(local_models.download_size(model), ask, lambda _exc: ask(None))

    def _pull(self, model: str) -> None:
        row, button, bar = self._models[model]
        self._pulling.add(model)
        button.set_visible(False)
        bar.set_visible(True)

        def progress(status: str, fraction: float | None) -> None:
            def show() -> bool:
                row.set_subtitle(status.capitalize())
                if fraction is None:
                    bar.pulse()
                else:
                    bar.set_fraction(fraction)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(show)  # called from the background thread

        def finished(_none: None) -> None:
            self._pulling.discard(model)
            self._recheck()

        def failed(exc: BaseException) -> None:
            self._pulling.discard(model)
            row.set_subtitle(f"Could not download: {exc}")
            bar.set_visible(False)
            button.set_visible(True)

        self._run(local_models.pull(model, progress), finished, failed)

    def _choose(self) -> None:
        self._chosen = True
        self._on_ready()
        self.close()

    def _closed(self) -> None:
        GLib.source_remove(self._timer)
        if not self._chosen:
            self._on_cancel()


def _in_terminal(command: str) -> bool:
    """Run `command` in the user's terminal, which stays open to show how it went."""
    script = f"{command}; echo; read -r -p 'Done. Press Enter to close. ' _"
    for terminal in (["xdg-terminal-exec"], ["ptyxis", "--"], ["gnome-terminal", "--"]):
        if shutil.which(terminal[0]):
            subprocess.Popen([*terminal, "bash", "-c", script], start_new_session=True)
            return True
    return False
