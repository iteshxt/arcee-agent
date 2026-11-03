# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""Training "Hey Arcee" on the user's voice (wake_match), as phone assistants do.

Three times "Hey Arcee", then one ordinary sentence; each is heard until the user
stops speaking, and the dialog moves on by itself. arceed stops listening for the wake
word meanwhile, and takes up what was trained when it is done. What is kept is the
sound of the samples, not the audio.
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


class WakeTrainingDialog(Adw.Dialog):
    def __init__(self, run: Runner, on_trained: Callable[[], None]) -> None:
        super().__init__(title="Train “Hey Arcee”", content_width=440)
        self._run = run
        self._on_trained = on_trained
        self._session: Any = None  # the running training, cancelled if the dialog closes

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=12,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        self._icon = Gtk.Image(icon_name="audio-input-microphone-symbolic", pixel_size=56)
        self._icon.add_css_class("dim-label")
        self._heading = Gtk.Label(
            label="Teach Arcee your voice", wrap=True, justify=Gtk.Justification.CENTER
        )
        self._heading.add_css_class("title-2")
        self._text = Gtk.Label(
            label="You will say “Hey Arcee” three times, then one ordinary sentence. Speak as "
            "you normally would, from where you usually sit. It takes about half a minute.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        self._text.add_css_class("dim-label")
        self._level = Gtk.LevelBar(
            min_value=0, max_value=1, visible=False, margin_start=40, margin_end=40
        )
        self._steps = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self._dots: list[Gtk.Image] = []
        for _ in range(wake_match.SAMPLES + 1):
            dot = Gtk.Image(icon_name="radio-symbolic", pixel_size=14)
            self._dots.append(dot)
            self._steps.append(dot)
        self._button = Gtk.Button(label="Start", halign=Gtk.Align.CENTER, margin_top=6)
        self._button.add_css_class("pill")
        self._button.add_css_class("suggested-action")
        self._button.connect("clicked", lambda _b: self._clicked())
        for child in (
            self._icon,
            self._heading,
            self._text,
            self._level,
            self._steps,
            self._button,
        ):
            box.append(child)

        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.connect("closed", lambda _d: self._closed())
        self._done = False

    # --- the steps, told from the training (on the background thread) -----------------

    def _say(self, step: int, heading: str, text: str) -> None:
        def show() -> bool:
            self._heading.set_label(heading)
            self._text.set_label(text)
            for n, dot in enumerate(self._dots):
                dot.set_from_icon_name("object-select-symbolic" if n < step else "radio-symbolic")
                if n == step:
                    dot.set_from_icon_name("radio-checked-symbolic")
            return GLib.SOURCE_REMOVE

        GLib.idle_add(show)

    def _loudness(self, value: float) -> None:
        GLib.idle_add(lambda: self._level.set_value(min(1.0, value * 4)) and False)

    async def _train(self) -> wake_match.Training:
        vad = voice_local.make_vad()
        await wake_match.hold_wake(True)
        try:
            samples: list[list[float]] = []
            for n in range(wake_match.SAMPLES):
                self._say(n, "Say “Hey Arcee”", f"{n + 1} of {wake_match.SAMPLES}")
                samples.append(await voice_local.record_phrase(self._loudness, vad=vad))
            self._say(
                wake_match.SAMPLES,
                "Now say something else",
                f"An ordinary sentence, such as {SENTENCE} This shows Arcee what is not its name.",
            )
            other = await voice_local.record_phrase(self._loudness, vad=vad)
            training = wake_match.train(samples, other)
            wake_match.save(training.trained)
            return training
        finally:
            await wake_match.hold_wake(False)  # arceed takes up what was trained

    # --- the dialog ------------------------------------------------------------------

    def _clicked(self) -> None:
        if self._done:
            self.close()
            return
        self._button.set_visible(False)
        self._level.set_visible(True)
        self._icon.remove_css_class("dim-label")
        self._icon.add_css_class("accent")
        self._session = self._run(self._train(), self._trained, self._failed)

    def _trained(self, _training: wake_match.Training) -> None:
        self._done = True
        self._session = None
        self._level.set_visible(False)
        self._icon.set_from_icon_name("object-select-symbolic")
        self._icon.remove_css_class("accent")
        self._icon.add_css_class("success")
        for dot in self._dots:
            dot.set_from_icon_name("object-select-symbolic")
        self._heading.set_label("Arcee knows your voice")
        self._text.set_label(
            "Say “Hey Arcee” whenever you need it. If it misses you, or wakes when you did "
            "not call it, train it again from here."
        )
        self._button.set_label("Done")
        self._button.set_visible(True)
        self._on_trained()

    def _failed(self, error: BaseException) -> None:
        self._session = None
        self._level.set_visible(False)
        self._icon.remove_css_class("accent")
        self._icon.add_css_class("dim-label")
        self._heading.set_label("Not trained")
        reason = str(error) if isinstance(error, VoiceError) else f"something went wrong ({error})"
        self._text.set_label(f"{reason[:1].upper()}{reason[1:]}.")
        for dot in self._dots:
            dot.set_from_icon_name("radio-symbolic")
        self._button.set_label("Try again")
        self._button.set_visible(True)

    def _closed(self) -> None:
        if self._session is not None:
            self._session.cancel()  # its own `finally` lets arceed listen again
