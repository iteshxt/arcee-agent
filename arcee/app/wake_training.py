# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""Training "Hey Arcee" on the user's voice (wake_match), as phone assistants do.

Three times "Hey Arcee", twice "Arcee stop", then one ordinary sentence; each is heard
until the user
stops speaking, and the trainer moves on by itself. People say it differently (run
together, or "hey… Arcee" with a pause), so it is said the user's own way and matched
to that. arceed stops listening for the wake word meanwhile, and takes up what was
trained when it is done. What is kept is the sound of the samples, not the audio.

`WakeTrainer` is the widget, in first-run setup and in `WakeTrainingDialog` (Voice
settings, to train again).
"""

from collections.abc import Callable
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from arcee import voice_local, wake_match  # noqa: E402
from arcee.voice import VoiceError  # noqa: E402

Runner = Callable[..., Any]  # (work, done[, failed]) -> its future, as Background.run
SENTENCE: Final = "“Hey, what's the weather like today?”"
ABOUT: Final = (
    "You will say “Hey Arcee” three times, “Arcee stop” twice, then one ordinary "
    "sentence. Say them the way you naturally do, run together or with a pause, from "
    "where you usually sit. It takes about half a minute."
)


def is_trained() -> bool:
    return wake_match.trained_path().is_file()


class WakeTrainer(Gtk.Box):
    """The steps of training, with a Start (or Train again) button."""

    def __init__(
        self,
        run: Runner,
        on_trained: Callable[[], None] = lambda: None,
        icon: bool = True,  # off where the page shows its own
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._run = run
        self._on_trained = on_trained
        self._session: Any = None  # the running training, cancelled with `cancel()`

        self._icon = Gtk.Image(
            icon_name="audio-input-microphone-symbolic", pixel_size=56, visible=icon
        )
        self._heading = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._heading.add_css_class("title-2")
        self._text = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=52)
        self._text.add_css_class("dim-label")
        self._level = Gtk.LevelBar(
            min_value=0, max_value=1, visible=False, margin_start=40, margin_end=40
        )
        self._steps = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self._dots: list[Gtk.Image] = []
        for _ in range(wake_match.SAMPLES + wake_match.STOP_SAMPLES + 1):
            dot = Gtk.Image(icon_name="radio-symbolic", pixel_size=14)
            self._dots.append(dot)
            self._steps.append(dot)
        self._button = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=6)
        self._button.add_css_class("pill")
        self._button.connect("clicked", lambda _b: self.start())
        for child in (
            self._icon,
            self._heading,
            self._text,
            self._level,
            self._steps,
            self._button,
        ):
            self.append(child)
        if is_trained():
            self._show_trained()
        else:
            self._show_ready()

    # --- what the user sees ---------------------------------------------------------

    def _icon_style(self, style: str) -> None:
        for name in ("dim-label", "accent", "success"):
            self._icon.remove_css_class(name)
        self._icon.add_css_class(style)

    def _show_dots(self, step: int) -> None:
        for n, dot in enumerate(self._dots):
            name = "object-select-symbolic" if n < step else "radio-symbolic"
            dot.set_from_icon_name("radio-checked-symbolic" if n == step else name)

    def _show_ready(self) -> None:
        self._icon.set_from_icon_name("audio-input-microphone-symbolic")
        self._icon_style("dim-label")
        self._heading.set_label("Teach Arcee your voice")
        self._text.set_label(ABOUT)
        self._show_dots(-1)
        self._button.set_label("Start")
        self._button.add_css_class("suggested-action")
        self._button.set_visible(True)

    def _show_trained(self) -> None:
        self._icon.set_from_icon_name("object-select-symbolic")
        self._icon_style("success")
        self._heading.set_label("Arcee knows your voice")
        self._text.set_label(
            "Say “Hey Arcee” whenever you need it, and “Arcee stop” to stop it. If it "
            "misses you, or wakes when you did not call it, train it again: in a new room, "
            "with a new mic, or to say it another way."
        )
        self._show_dots(len(self._dots))
        self._button.set_label("Train again")
        self._button.remove_css_class("suggested-action")
        self._button.set_visible(True)

    # Told from the training, on the background thread.
    def _say(self, step: int, heading: str, text: str) -> None:
        def show() -> bool:
            self._heading.set_label(heading)
            self._text.set_label(text)
            self._show_dots(step)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(show)

    def _loudness(self, value: float) -> None:
        GLib.idle_add(lambda: self._level.set_value(min(1.0, value * 4)) and False)

    # --- training -------------------------------------------------------------------

    async def _train(self) -> wake_match.Training:
        vad = voice_local.make_vad()
        await wake_match.hold_wake(True)
        try:
            samples: list[list[float]] = []
            for n in range(wake_match.SAMPLES):
                self._say(
                    n,
                    "Say “Hey Arcee”",
                    f"{n + 1} of {wake_match.SAMPLES}, your own way, then stop.",
                )
                samples.append(await voice_local.record_phrase(self._loudness, vad=vad))
            stops: list[list[float]] = []
            for n in range(wake_match.STOP_SAMPLES):
                self._say(
                    wake_match.SAMPLES + n,
                    "Say “Arcee stop”",
                    f"{n + 1} of {wake_match.STOP_SAMPLES}: what you will say to stop a task.",
                )
                stops.append(await voice_local.record_phrase(self._loudness, vad=vad))
            self._say(
                wake_match.SAMPLES + wake_match.STOP_SAMPLES,
                "Now say something else",
                f"An ordinary sentence, such as {SENTENCE} This shows Arcee what is not its name.",
            )
            other = await voice_local.record_phrase(self._loudness, vad=vad)
            training = wake_match.train(samples, other, stops)
            wake_match.save(training.trained)
            return training
        finally:
            await wake_match.hold_wake(False)  # arceed takes up what was trained

    def start(self) -> None:
        if voice_local.missing():
            self._failed(VoiceError("the voice models are not downloaded yet"))
            return
        self._button.set_visible(False)
        self._level.set_visible(True)
        self._icon.set_from_icon_name("audio-input-microphone-symbolic")
        self._icon_style("accent")
        self._show_dots(0)
        self._session = self._run(self._train(), self._trained, self._failed)

    def cancel(self) -> None:
        if self._session is not None:
            self._session.cancel()  # its own `finally` lets arceed listen again
            self._session = None

    def _trained(self, _training: wake_match.Training) -> None:
        self._session = None
        self._level.set_visible(False)
        self._show_trained()
        self._on_trained()

    def _failed(self, error: BaseException) -> None:
        self._session = None
        self._level.set_visible(False)
        self._icon.set_from_icon_name("audio-input-microphone-symbolic")
        self._icon_style("dim-label")
        self._heading.set_label("Not trained")
        reason = str(error) if isinstance(error, VoiceError) else f"something went wrong ({error})"
        self._text.set_label(f"{reason[:1].upper()}{reason[1:]}.")
        self._show_dots(-1)
        self._button.set_label("Try again")
        self._button.add_css_class("suggested-action")
        self._button.set_visible(True)


class WakeTrainingDialog(Adw.Dialog):
    """Training (again) from the Voice settings."""

    def __init__(self, run: Runner, on_trained: Callable[[], None]) -> None:
        super().__init__(title="Train “Hey Arcee”", content_width=460)
        self._trainer = WakeTrainer(run, on_trained)
        self._trainer.set_margin_top(12)
        self._trainer.set_margin_bottom(24)
        self._trainer.set_margin_start(24)
        self._trainer.set_margin_end(24)
        view = Adw.ToolbarView(content=self._trainer)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.connect("closed", lambda _d: self._trainer.cancel())
