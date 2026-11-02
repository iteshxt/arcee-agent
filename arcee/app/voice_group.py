# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The app's Voice settings (decision 0015): where speech becomes text, and the wake word.

Speech becomes text on this computer once its model is downloaded (asked first, with
its size), or on Groq. "Listen for 'Hey Arcee'" downloads the small wake-word models
the first time; arceed then keeps the mic open, and only the keyword spotter hears it.
"""

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from arcee import voice_local  # noqa: E402
from arcee.voice_local import SPEECH, SPOTTER, VAD, VoiceModel, installed  # noqa: E402

Runner = Callable[..., None]  # (work, done[, failed]), as Background.run


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
            description="Talk to Arcee from the mic button, or by saying “Hey Arcee”. "
            "On this computer nothing you say leaves it.",
        )
        self._run = run
        self._toast = toast
        self._settings = settings
        self._saved = saved

        self._local = Adw.ActionRow(title="Speech to text on this computer")
        self._local_check = Gtk.CheckButton(active=settings.speech == "local")
        self._local.add_prefix(self._local_check)
        self._local.set_activatable_widget(self._local_check)
        self._download = Gtk.Button(label="Download…", valign=Gtk.Align.CENTER)
        self._download.connect("clicked", lambda _b: self._confirm([SPEECH], self._show))
        self._progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=120, visible=False)
        self._local.add_suffix(self._progress)
        self._local.add_suffix(self._download)
        self.add(self._local)

        groq = Adw.ActionRow(
            title="Speech to text on Groq",
            subtitle="Whisper large: more accurate with names; your voice goes to Groq",
        )
        self._groq_check = Gtk.CheckButton(
            group=self._local_check, active=settings.speech == "groq"
        )
        groq.add_prefix(self._groq_check)
        groq.set_activatable_widget(self._groq_check)
        self.add(groq)
        self._local_check.connect("toggled", self._on_speech)

        self._listen = Adw.SwitchRow(
            title="Listen for “Hey Arcee”",
            subtitle="The mic stays open, but only a small model on this computer hears it "
            "until you say “Hey Arcee”. “Arcee stop” stops a task.",
            active=settings.listen,
        )
        self._listen.connect("notify::active", self._on_listen)
        self.add(self._listen)
        self._show()

    def _show(self) -> None:
        have = installed(SPEECH)
        self._local.set_subtitle(
            f"Moonshine, about half a second a request · "
            f"{'downloaded' if have else 'not downloaded'} ({SPEECH.size_mb} MB)"
        )
        self._download.set_visible(not have)
        self._progress.set_visible(False)

    # --- choices ----------------------------------------------------------------------

    def _on_speech(self, check: Gtk.CheckButton) -> None:
        speech = "local" if check.get_active() else "groq"
        if speech == self._settings.speech:
            return
        self._settings.speech = speech
        self._saved()
        if speech == "local" and not installed(SPEECH):
            self._confirm([SPEECH], self._show)

    def _on_listen(self, row: Adw.SwitchRow, _param: Any) -> None:
        on = row.get_active()
        if on == self._settings.listen:
            return
        needed = [m for m in (SPOTTER, VAD) if not installed(m)]
        if on and needed:
            # Turned on only once the models are here; until then the switch waits.
            row.set_active(False)
            self._confirm(needed, lambda: self._listen.set_active(True))
            return
        self._settings.listen = on
        self._saved()  # arceed starts again with the listener on or off

    # --- downloading ------------------------------------------------------------------

    def _confirm(self, models: list[VoiceModel], then: Callable[[], None]) -> None:
        size = sum(m.size_mb for m in models)
        names = "\n".join(f"• {m.purpose} ({m.size_mb} MB)" for m in models)
        dialog = Adw.AlertDialog(
            heading=f"Download {size} MB?",
            body=f"From sherpa-onnx's releases on GitHub, kept on this computer:\n{names}",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "download" and self._fetch(models, then))
        dialog.present(self.get_root())

    def _fetch(self, models: list[VoiceModel], then: Callable[[], None]) -> None:
        self._download.set_sensitive(False)
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
            self._download.set_sensitive(True)
            self._show()
            self._toast("Voice models downloaded")
            self._saved()  # arceed loads them when it starts again
            then()

        def failed(error: BaseException) -> None:
            self._download.set_sensitive(True)
            self._show()
            self._toast(f"Could not download: {error}")

        self._run(fetch(), finished, failed)
