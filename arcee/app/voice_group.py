# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The app's Voice settings: the wake word, voice's first tier (decision 0015).

"Listen for 'Hey Arcee'" downloads the two small wake-word models the first time
(asked first, with their size); arceed then keeps the mic open, and only the keyword
spotter on this computer hears it until "hey Arcee". The request after it goes to
Groq's Whisper, as the mic button's does.
"""

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from arcee import voice_local, wake_match  # noqa: E402
from arcee.app.wake_training import WakeTrainingDialog  # noqa: E402
from arcee.voice_local import MODELS, VoiceModel, installed  # noqa: E402

Runner = Callable[..., Any]  # (work, done[, failed]), as Background.run


class VoiceGroup(Adw.PreferencesGroup):
    def __init__(
        self,
        run: Runner,
        toast: Callable[[str], None],
        settings: Any,
        saved: Callable[[], None],
    ) -> None:
        super().__init__(
            title="Voice",
            description="Say “Hey Arcee”, or press the mic. The wake word is heard on this "
            "computer; only what you say after it goes to Groq to become text.",
        )
        self._run = run
        self._toast = toast
        self._settings = settings
        self._saved = saved

        self._listen = Adw.SwitchRow(
            title="Listen for “Hey Arcee”",
            subtitle="The mic stays open, but a small model on this computer is all that "
            "hears it until you say “Hey Arcee”. “Arcee stop” stops a task.",
            active=settings.listen,
        )
        self._listen.connect("notify::active", self._on_listen)
        self._progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=100, visible=False)
        self._listen.add_suffix(self._progress)
        self.add(self._listen)

        # Training: the spotter's English model misses "Arcee" in some voices and mics.
        self._train = Adw.ActionRow(title="Your voice")
        self._forget = Gtk.Button(
            icon_name="user-trash-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Forget the training",
        )
        self._forget.add_css_class("flat")
        self._forget.connect("clicked", lambda _b: self._forget_training())
        self._train_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._train_button.connect("clicked", lambda _b: self._open_training())
        self._train.add_suffix(self._forget)
        self._train.add_suffix(self._train_button)
        self.add(self._train)
        self._show_training()

    def _show_training(self) -> None:
        trained = wake_match.trained_path().is_file()
        self._train.set_subtitle(
            "Trained: “Hey Arcee” is matched to how you say it."
            if trained
            else "Not trained. If “Hey Arcee” does not wake it, say it a few times so Arcee "
            "learns how you say it."
        )
        self._train_button.set_label("Train again…" if trained else "Train…")
        self._forget.set_visible(trained)
        self._train.set_sensitive(self._settings.listen)

    def _open_training(self) -> None:
        WakeTrainingDialog(self._run, self._show_training).present(self.get_root())

    def _forget_training(self) -> None:
        wake_match.forget()
        self._run(wake_match.hold_wake(False), lambda _none: None)  # arceed forgets it too
        self._show_training()
        self._toast("Forgot the training.")

    def _on_listen(self, row: Adw.SwitchRow, _param: Any) -> None:
        on = row.get_active()
        if on == self._settings.listen:
            return
        needed = [m for m in MODELS if not installed(m)]
        if on and needed:
            # On only once the models are here; until then the switch waits.
            row.set_active(False)
            self._confirm(needed)
            return
        self._settings.listen = on
        self._saved()  # arceed starts again with the listener on or off
        self._show_training()

    def _confirm(self, models: list[VoiceModel]) -> None:
        size = sum(m.size_mb for m in models)
        names = "\n".join(f"• {m.purpose} ({m.size_mb} MB)" for m in models)
        dialog = Adw.AlertDialog(
            heading=f"Download {size} MB?",
            body=f"From sherpa-onnx's releases on GitHub, kept on this computer:\n{names}",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "download" and self._fetch(models))
        dialog.present(self.get_root())

    def _fetch(self, models: list[VoiceModel]) -> None:
        self._listen.set_sensitive(False)
        self._progress.set_visible(True)
        total = sum(m.size_mb for m in models)

        async def fetch() -> None:
            done = 0
            for model in models:

                def progress(fraction: float, model: VoiceModel = model, done: int = done) -> None:
                    value = (done + fraction * model.size_mb) / total
                    GLib.idle_add(lambda: self._progress.set_fraction(value) and False)

                await voice_local.download(model, progress)
                done += model.size_mb

        def finished(_none: None) -> None:
            self._listen.set_sensitive(True)
            self._progress.set_visible(False)
            self._listen.set_active(True)  # now that the models are here
            if not wake_match.trained_path().is_file():
                self._open_training()  # as phones do when the assistant is first turned on

        def failed(error: BaseException) -> None:
            self._listen.set_sensitive(True)
            self._progress.set_visible(False)
            self._toast(f"Could not download: {error}")

        self._run(fetch(), finished, failed)
