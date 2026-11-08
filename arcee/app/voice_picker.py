# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""Settings → Arcee's voice: the eight voices as cards, each with its name and how it
sounds (decision 0019). Choosing one says a sentence in it, through arceed."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from arcee.voice_reply import VOICES, Voice, voice_id  # noqa: E402

CSS = """
.voice-card {
  padding: 12px 14px;
  border-radius: 14px;
  background: var(--card-bg-color);
  box-shadow: 0 0 0 1px alpha(currentColor, 0.08), 0 1px 3px alpha(black, 0.10);
  transition: box-shadow 150ms, background 150ms;
}
.voice-card:hover { background: alpha(var(--accent-bg-color), 0.06); }
.voice-card.chosen {
  background: alpha(var(--accent-bg-color), 0.12);
  box-shadow: 0 0 0 2px var(--accent-bg-color);
}
.voice-card .voice-name { font-weight: 700; font-size: 1.05em; }
.voice-card .voice-about { font-weight: 400; opacity: 0.7; font-size: 0.9em; }
.voice-card .voice-playing { color: var(--accent-color); }
"""

_styled = False


def _style() -> None:
    global _styled
    display = Gdk.Display.get_default()
    if _styled or display is None:
        return
    css = Gtk.CssProvider()
    css.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _styled = True


AVATARS = Path(__file__).parent / "assets" / "voices"


def avatar(voice: Voice, size: int) -> Any:
    """The voice's portrait (packaging/voice_avatars.py draws them), drawn at twice the
    size for sharp HiDPI screens; its initial when the picture cannot be read."""
    widget = Adw.Avatar(size=size, text=voice.name, show_initials=True)
    try:
        from gi.repository import GdkPixbuf

        path = AVATARS / f"{voice.name.lower()}.svg"
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size * 2, size * 2)
        widget.set_custom_image(Gdk.Texture.new_for_pixbuf(pixbuf))
    except Exception:  # no SVG loader: the initial stays
        pass
    return widget


class VoiceCard(Gtk.Button):
    def __init__(self, voice: Voice, on_pick: Callable[[Voice], None]) -> None:
        super().__init__(css_classes=["voice-card", "flat"], hexpand=True)
        self.voice = voice
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{voice.name}: {voice.about}"])
        box = Gtk.Box(spacing=12)
        box.append(avatar(voice, 48))
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        words.append(Gtk.Label(label=voice.name, xalign=0, css_classes=["voice-name"]))
        words.append(
            Gtk.Label(
                label=voice.about,
                xalign=0,
                wrap=True,
                css_classes=["voice-about"],
            )
        )
        words.set_hexpand(True)
        box.append(words)
        self._mark = Gtk.Image(
            icon_name="audio-volume-high-symbolic",
            css_classes=["voice-playing"],
            tooltip_text="Hear it",
        )
        box.append(self._mark)
        self.set_child(box)
        self.connect("clicked", lambda _b: on_pick(voice))
        self.set_chosen(False)

    def set_chosen(self, chosen: bool) -> None:
        if chosen:
            self.add_css_class("chosen")
            self._mark.set_from_icon_name("object-select-symbolic")
        else:
            self.remove_css_class("chosen")
            self._mark.set_from_icon_name("audio-volume-high-symbolic")
        self._mark.set_opacity(1.0 if chosen else 0.45)


class VoicePicker(Adw.PreferencesGroup):
    """The voices as a grid of cards; `on_pick(name)` when one is chosen (or chosen
    again, to hear it once more)."""

    def __init__(self, chosen: str, on_pick: Callable[[str], None]) -> None:
        super().__init__(
            title="Arcee's voice",
            description="Heard when Arcee answers aloud, and when you ask it to read "
            "something out loud. Pick one to hear it.",
        )
        _style()
        self._on_pick = on_pick
        grid = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=2,
            max_children_per_line=2,
            row_spacing=10,
            column_spacing=10,
        )
        self._cards: list[VoiceCard] = []
        for voice in VOICES:
            card = VoiceCard(voice, self._picked)
            self._cards.append(card)
            grid.append(card)
        for child in _children(grid):
            child.set_focusable(False)  # the card itself takes focus
        self.add(grid)
        self.show(chosen)

    def show(self, chosen: str) -> None:
        wanted = VOICES[voice_id(chosen)].name
        for card in self._cards:
            card.set_chosen(card.voice.name == wanted)

    def _picked(self, voice: Voice) -> None:
        self.show(voice.name)
        self._on_pick(voice.name)


def _children(widget: Any) -> list[Any]:
    out, child = [], widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out
