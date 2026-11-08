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
from arcee.app.voice_picker import VoicePicker  # noqa: E402
from arcee.app.wake_training import WakeTrainingDialog  # noqa: E402
from arcee.settings import load, save_learn_wakes, save_listen, save_settings  # noqa: E402
from arcee.voice_local import MODELS, VoiceModel, installed  # noqa: E402
from arcee.voice_reply import SPEECH, fetch  # noqa: E402


async def set_wake_word(on: bool) -> None:
    """Listen for "hey Arcee" or stop, at once: arceed switches its listener and keeps
    the setting (D-Bus starts it to listen). When arceed cannot be reached, the setting
    alone is kept, for when it next starts."""
    from dbus_fast import BusType, Message, MessageFlag, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.errors import DBusError

    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            reply = await bus.call(
                Message(
                    destination="me.iteshxt.Arcee.Agent",
                    path="/me/iteshxt/Arcee/Agent",
                    interface="me.iteshxt.Arcee.Agent",
                    member="SetWakeWord",
                    signature="b",
                    body=[on],
                    # Off: if arceed is not running, nothing listens; do not start it.
                    flags=MessageFlag.NONE if on else MessageFlag.NO_AUTOSTART,
                )
            )
        finally:
            bus.disconnect()
        if reply is not None and reply.message_type is MessageType.METHOD_RETURN:
            return
    except OSError, DBusError:
        pass
    save_listen(on)


async def _agent(member: str, signature: str, body: list[Any]) -> None:
    """Call arceed, starting it if needed; nothing when the session bus is not there."""
    from dbus_fast import BusType, Message
    from dbus_fast.aio import MessageBus
    from dbus_fast.errors import DBusError

    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except OSError, DBusError:
        return
    try:
        await bus.call(
            Message(
                destination="me.iteshxt.Arcee.Agent",
                path="/me/iteshxt/Arcee/Agent",
                interface="me.iteshxt.Arcee.Agent",
                member=member,
                signature=signature,
                body=body,
            )
        )
    finally:
        bus.disconnect()


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

        self._learn = Adw.SwitchRow(
            title="Learn from your wakes",
            subtitle="A “Hey Arcee” you go on to use, if it is as close to your training "
            "as your samples are to each other, becomes a sample too. Off by default: a "
            "false wake can teach it ordinary speech.",
            active=settings.learn_wakes,
        )
        self._learn.connect("notify::active", self._on_learn)
        self.add(self._learn)

        # Spoken replies (decision 0019): made on this computer, downloaded when turned on.
        self._aloud = Adw.SwitchRow(
            title="Answer aloud",
            subtitle="When you ask by voice, Arcee says its answer and its questions too, "
            "in a voice made on this computer. Closing the pill stops it.",
            active=settings.speak_replies and installed(SPEECH),
        )
        self._aloud.connect("notify::active", self._on_aloud)
        self._aloud_progress = Gtk.ProgressBar(
            valign=Gtk.Align.CENTER, width_request=100, visible=False
        )
        self._aloud.add_suffix(self._aloud_progress)
        self.add(self._aloud)
        # The voices, as their own group under this one (the window adds it).
        self.picker = VoicePicker(settings.voice, self._on_voice)
        self._show_training()

    def refresh(self) -> None:
        """Show the settings as they are now (first-run setup may have changed them)."""
        saved = load()
        self._settings.listen = saved.listen  # the panel may have switched it
        self._settings.learn_wakes = saved.learn_wakes
        self._listen.set_active(self._settings.listen)
        self._learn.set_active(self._settings.learn_wakes)
        self._settings.speak_replies = saved.speak_replies
        self._settings.voice = saved.voice
        self._aloud.set_active(saved.speak_replies and installed(SPEECH))
        self.picker.show(saved.voice)
        self._show_training()

    def _show_training(self) -> None:
        trained = wake_match.is_trained()
        self._train.set_subtitle(
            "Trained: “Hey Arcee” is matched to how you say it."
            if trained
            else "Not trained. If “Hey Arcee” does not wake it, say it a few times so Arcee "
            "learns how you say it."
        )
        self._train_button.set_label("Train again…" if trained else "Train…")
        if not trained:
            self._train_button.add_css_class("suggested-action")
        else:
            self._train_button.remove_css_class("suggested-action")
        self._forget.set_visible(trained)
        self._train.set_sensitive(self._settings.listen)
        self._learn.set_sensitive(self._settings.listen and trained)  # learns your samples
        self.picker.set_sensitive(installed(SPEECH))  # "read it out loud" needs it too

    def _open_training(self) -> None:
        WakeTrainingDialog(self._run, self._show_training).present(self.get_root())

    def _forget_training(self) -> None:
        wake_match.forget()
        self._run(wake_match.hold_wake(False), lambda _none: None)  # arceed forgets it too
        self._show_training()
        self._toast("Forgot the training.")

    def _on_learn(self, row: Adw.SwitchRow, _param: Any) -> None:
        on = row.get_active()
        if on == self._settings.learn_wakes:
            return
        self._settings.learn_wakes = on
        save_learn_wakes(on)
        # arceed reads the setting as it learns; reloading the wake word now brings in
        # the samples it learned before (on) or stops matching against them (off).
        self._run(wake_match.hold_wake(False), lambda _none: None)

    def _on_aloud(self, row: Adw.SwitchRow, _param: Any) -> None:
        on = row.get_active()
        if on and not installed(SPEECH):
            row.set_active(False)  # on once the voice is here
            self._confirm_voice()
            return
        if on != self._settings.speak_replies:
            self._settings.speak_replies = on
            save_settings(speak_replies=on)
        self.picker.set_sensitive(installed(SPEECH))

    def _on_voice(self, name: str) -> None:
        if name != self._settings.voice:
            self._settings.voice = name
            save_settings(voice=name)
        self._preview()

    def _preview(self) -> None:
        if installed(SPEECH):
            self._run(_agent("PreviewVoice", "s", [self._settings.voice]), lambda _none: None)

    def _confirm_voice(self) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Download {SPEECH.size_mb} MB?",
            body="Arcee's voice (Kitten TTS, from Hugging Face), kept on this computer: "
            "answers are said here, nothing is sent anywhere to be said.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "download" and self._fetch_voice())
        dialog.present(self.get_root())

    def _fetch_voice(self) -> None:
        self._aloud.set_sensitive(False)
        self._aloud_progress.set_visible(True)

        def progress(fraction: float) -> None:
            GLib.idle_add(lambda: self._aloud_progress.set_fraction(fraction) and False)

        def finished(_none: None) -> None:
            self._aloud.set_sensitive(True)
            self._aloud_progress.set_visible(False)
            self._aloud.set_active(True)  # now that the voice is here
            self._preview()

        def failed(error: BaseException) -> None:
            self._aloud.set_sensitive(True)
            self._aloud_progress.set_visible(False)
            self._toast(f"Could not download: {error}")

        self._run(fetch(progress), finished, failed)

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
        self._run(set_wake_word(on), lambda _none: None)  # at once, no restart
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
        dialog.connect("response", lambda _d, r: r == "download" and self._fetch())
        dialog.present(self.get_root())

    def _fetch(self) -> None:
        self._listen.set_sensitive(False)
        self._progress.set_visible(True)

        def progress(fraction: float) -> None:
            GLib.idle_add(lambda: self._progress.set_fraction(fraction) and False)

        def finished(_none: None) -> None:
            self._listen.set_sensitive(True)
            self._progress.set_visible(False)
            self._listen.set_active(True)  # now that the models are here
            if not wake_match.is_trained():
                self._open_training()  # as phones do when the assistant is first turned on

        def failed(error: BaseException) -> None:
            self._listen.set_sensitive(True)
            self._progress.set_visible(False)
            self._toast(f"Could not download: {error}")

        self._run(voice_local.download_missing(progress), finished, failed)
