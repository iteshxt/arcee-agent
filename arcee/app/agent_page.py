# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The app's Agent page: talk to Arcee from the app, as from the panel.

A ring of glowing particles in sunrise amber breathes while Arcee waits and pulses
while the mic listens or Arcee works: two loops rendered once (tools/make_art.py) and
played here, frame by frame. With no conversation the ring fills the page; once there
is one it shrinks into a slim bar above it (with what Arcee is doing, and New chat),
and the conversation takes the room: requests, steps, questions with their answers,
replies, always scrolled to the newest. At the bottom, one bar to type or speak: its
button is the mic, Send once there is text, Stop while Arcee works.
It talks to arceed over D-Bus on the GTK main loop, like the panel;
what the user says is run by arceed itself, so the panel and the app never both run it.
"""

import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from arcee.app.formatting import show_markdown  # noqa: E402

BUS_NAME: Final = "me.iteshxt.Arcee.Agent"
OBJECT_PATH: Final = "/me/iteshxt/Arcee/Agent"
# The ring's two loops, rendered once by tools/make_art.py. Drawing it live (1,100
# particles, up to 60 times a second) kept a fifth of a fast core busy and froze the
# app on slower machines; playing frames costs next to nothing.
ASSETS: Final = Path(__file__).parent / "assets"
# Transparent loops, one look for a dark window and one for a light one.
LOOPS: Final = {"idle": "wave-idle-{shade}.webp", "active": "wave-active-{shade}.webp"}
IDLE_TEXT: Final = "Ask me anything"
HERO_PX: Final = 280  # the ring with no conversation
RECENT_CHATS: Final = 4  # on the idle page; the Chats menu shows more
CHATS_MENU: Final = 12
# Tried by a click on the idle page: what Arcee does well, in the user's words.
SUGGESTIONS: Final = (
    "What's on my calendar today?",
    "Summarize my latest email",
    "What's the weather like?",
    "How much disk space is free?",
)
CORE_NAMES: Final = {"groq": "Groq", "local": "Local"}
BAR_PX: Final = 36  # and in the bar above one
CHAT_WIDTH: Final = 760
# Within this many pixels of the end, the conversation follows what is added.
STICK_PX: Final = 48
# The app's own touches: the mic in sunrise orange, red while it listens or works.
CSS: Final = b"""
.arcee-composer { background: alpha(currentColor, 0.07); border-radius: 26px;
  padding: 4px 4px 4px 18px; }
.arcee-composer entry, .arcee-composer entry:focus-within { background: none; border: none;
  box-shadow: none; outline: none; min-height: 40px; }
.arcee-mic { background: #ff7a1a; color: white; min-width: 44px; min-height: 44px; }
.arcee-mic:hover { background: #ff8f3d; }
.arcee-mic.listening, .arcee-mic.listening:hover,
.arcee-mic.working, .arcee-mic.working:hover { background: #d93a2b; color: white; }
.arcee-you { background: alpha(#ff7a1a, 0.16); border-radius: 16px; padding: 8px 14px; }
.arcee-step { font-size: 0.88em; opacity: 0.6; }
.arcee-note { font-size: 0.92em; opacity: 0.75; font-style: italic; }
.arcee-question { padding: 12px 14px; }
.arcee-bar { border-bottom: 1px solid alpha(currentColor, 0.08); padding: 6px 12px; }
.arcee-chip { background: alpha(currentColor, 0.07); border-radius: 999px; padding: 3px 10px;
  font-size: 0.88em; }
.arcee-chip.on { background: alpha(#ff7a1a, 0.18); }
.arcee-suggestion { border-radius: 999px; padding: 6px 14px; }
.arcee-meta { font-size: 0.8em; opacity: 0.5; }
.arcee-greeting { font-size: 1.6em; font-weight: 600; }
"""


class SunWave(Gtk.Picture):
    """The ring. `state` is idle, listening or working; `level` the voice's loudness.

    Each loop's frames are decoded once into textures and shown in turn, only while
    the ring is on screen; the voice plays the active loop faster. The loops are
    transparent, drawn for the window's light or dark style, and switch with it.
    """

    def __init__(self, size: int = HERO_PX) -> None:
        super().__init__(
            can_shrink=True,
            content_fit=Gtk.ContentFit.CONTAIN,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            vexpand=False,
        )
        self._size = size
        self.state = "idle"
        self._level = 0.0
        self._loops: dict[str, tuple[list[Gdk.Texture], int]] = {}
        self._style = Adw.StyleManager.get_default()
        self._style.connect("notify::dark", lambda *_: self._restyle())
        self._index = 0
        self._timer = 0
        # The first frame now, so GTK sizes the ring by it (not by an empty picture).
        frames, _delay = self._loop()
        self.set_paintable(frames[0])
        self.connect("map", lambda _w: self._next())
        self.connect("unmap", lambda _w: self._stop())

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        # Exactly `size` square: a picture asks for its frames' own 300 px otherwise.
        return self._size, self._size, -1, -1

    def set_size(self, size: int) -> None:
        self._size = size
        self.queue_resize()

    def set_level(self, level: float) -> None:
        # Speech sits around 0.02-0.2 RMS; stretch it so quiet talk still speeds the ring.
        self._level = min(1.0, math.sqrt(max(level, 0.0)) * 2.5)

    def _loop(self) -> tuple[list[Gdk.Texture], int]:
        name = "idle" if self.state == "idle" else "active"
        if name not in self._loops:  # the active loop loads the first time it plays
            shade = "dark" if self._style.get_dark() else "light"
            self._loops[name] = _decode(ASSETS / LOOPS[name].format(shade=shade))
        return self._loops[name]

    def _restyle(self) -> None:
        """The window turned light or dark: the other look, loaded as it is needed."""
        self._loops.clear()
        frames, _delay = self._loop()
        self.set_paintable(frames[self._index % len(frames)])

    def _next(self) -> bool:
        frames, delay = self._loop()
        self._index = (self._index + 1) % len(frames)
        self.set_paintable(frames[self._index])
        if self.state == "listening":
            delay = round(delay / (1 + 1.5 * self._level))
        self._timer = GLib.timeout_add(delay, self._next)
        return GLib.SOURCE_REMOVE

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0


def _decode(path: Path) -> tuple[list[Gdk.Texture], int]:
    """An animation's frames as textures (with their alpha), and its frame delay in ms."""
    from PIL import Image, ImageSequence

    with Image.open(path) as image:
        delay = int(image.info.get("duration", 60))
        frames = []
        for frame in ImageSequence.Iterator(image):
            rgba = frame.convert("RGBA")
            frames.append(
                Gdk.MemoryTexture.new(
                    rgba.width,
                    rgba.height,
                    Gdk.MemoryFormat.R8G8B8A8,
                    GLib.Bytes.new(rgba.tobytes()),
                    rgba.width * 4,
                )
            )
    return frames, delay


def _greeting() -> str:
    hour = time.localtime().tm_hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening"
    real = GLib.get_real_name()
    name = (real if real and real != "Unknown" else GLib.get_user_name()).split()[0]
    return f"Good {part}, {name.capitalize()}"


def _meta(done: dict[str, Any]) -> str:
    """The line under a reply: which models, where, how long, how many tokens, how fast."""
    parts: list[str] = []
    if models := done.get("models"):
        core = CORE_NAMES.get(done.get("core", ""), done.get("core", ""))
        parts.append(" → ".join(models) + (f" · {core}" if core else ""))
    if steps := done.get("steps"):
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    if seconds := done.get("seconds"):
        parts.append(f"{seconds:.1f} s")
    if tokens := done.get("tokens_out"):
        parts.append(f"{done.get('tokens_in', 0):,} in · {tokens:,} out")
    if speed := done.get("tokens_per_s"):
        parts.append(f"{speed:,} tok/s")
    return "  ·  ".join(parts)


def _off_main(read: Callable[[Any], Awaitable[Any]], then: Callable[[Any], None]) -> None:
    """`read(store)` on History in a thread; `then(result)` back on the GTK loop."""
    import asyncio
    import threading

    from arcee.context.store import Store, data_dir

    def work() -> None:
        async def go() -> Any:
            store = Store(data_dir())
            try:
                return await read(store)
            finally:
                store.close()

        try:
            result = asyncio.run(go())
        except Exception:  # History unreadable: the page simply shows nothing
            return
        GLib.idle_add(lambda: then(result) and False)

    threading.Thread(target=work, daemon=True).start()


class AgentPage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        css = Gtk.CssProvider()
        css.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._listening = False
        self._working = False
        self._stick = True  # following the newest message
        self._following = False  # a jump to the end is due on the next frame
        # Questions waiting for an answer, by id: answered anywhere, they close here too.
        self._open_questions: dict[str, Gtk.Widget] = {}
        self.wave = SunWave()
        self._status = Gtk.Label(
            label=IDLE_TEXT, ellipsize=Pango.EllipsizeMode.END, max_width_chars=60
        )
        self._status.add_css_class("dim-label")

        # The bar above a conversation: the ring, small, what Arcee is doing, New chat.
        self._bar_ring = Gtk.Box(valign=Gtk.Align.CENTER)
        new = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="New chat"))
        new.add_css_class("flat")
        new.set_tooltip_text("Start a new conversation")
        new.connect("clicked", lambda _b: self._new_conversation())
        self._new = new
        self._bar_status = Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)
        self._bar_model = Gtk.Label(valign=Gtk.Align.CENTER)
        self._bar_model.add_css_class("arcee-chip")
        self._chats_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._chats_list.add_css_class("boxed-list")
        self._chats_list.connect("row-activated", self._on_chat_row)
        chats = Gtk.MenuButton(
            child=Adw.ButtonContent(icon_name="document-open-recent-symbolic", label="Chats"),
            popover=Gtk.Popover(
                child=Gtk.ScrolledWindow(
                    child=self._chats_list,
                    propagate_natural_height=True,
                    max_content_height=420,
                    min_content_width=340,
                    hscrollbar_policy=Gtk.PolicyType.NEVER,
                )
            ),
            tooltip_text="Earlier conversations",
        )
        chats.add_css_class("flat")
        chats.get_popover().connect(
            "show", lambda _p: self._load_chats(self._chats_list, CHATS_MENU)
        )
        bar = Gtk.Box(spacing=10)
        bar.add_css_class("arcee-bar")
        bar.append(self._bar_ring)
        bar.append(self._bar_status)
        bar.append(self._bar_model)
        bar.append(chats)
        bar.append(new)
        self._bar = Gtk.Revealer(child=bar, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.append(self._bar)

        # No conversation: the ring, large, a greeting, what Arcee runs on, things to try,
        # and the latest conversations to take up again.
        self._hero_ring = Gtk.Box(halign=Gtk.Align.CENTER)
        self._hero_status = Gtk.Box(halign=Gtk.Align.CENTER)
        greeting = Gtk.Label(label=_greeting(), halign=Gtk.Align.CENTER)
        greeting.add_css_class("arcee-greeting")
        self._greeting = greeting
        self._chips = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        tries = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            halign=Gtk.Align.CENTER,
            max_children_per_line=4,
            column_spacing=8,
            row_spacing=8,
            homogeneous=False,
        )
        for text in SUGGESTIONS:
            button = Gtk.Button(label=text)
            button.add_css_class("arcee-suggestion")
            button.connect("clicked", lambda _b, t=text: self._run_text(t))
            tries.append(button)
        self._recent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._recent_list.add_css_class("boxed-list")
        self._recent_list.connect("row-activated", self._on_chat_row)
        recent_title = Gtk.Label(label="Recent chats", xalign=0)
        recent_title.add_css_class("heading")
        self._recent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self._recent.append(recent_title)
        self._recent.append(self._recent_list)
        hero_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            valign=Gtk.Align.CENTER,
            vexpand=True,
        )
        hero_box.set_margin_top(12)
        hero_box.set_margin_bottom(12)
        hero_box.append(self._hero_ring)
        hero_box.append(greeting)
        hero_box.append(self._hero_status)
        hero_box.append(self._chips)
        hero_box.append(tries)
        recent_holder = Adw.Clamp(child=self._recent, maximum_size=560, margin_top=10)
        hero_box.append(recent_holder)
        hero = Gtk.ScrolledWindow(
            child=Adw.Clamp(
                child=hero_box, maximum_size=CHAT_WIDTH, margin_start=16, margin_end=16
            ),
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vexpand=True,
        )

        self._log = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._log.set_margin_top(16)
        self._log.set_margin_bottom(16)
        self._scroll = Gtk.ScrolledWindow(
            child=Adw.Clamp(
                child=self._log, maximum_size=CHAT_WIDTH, margin_start=16, margin_end=16
            ),
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        adjustment = self._scroll.get_vadjustment()
        adjustment.connect("notify::upper", lambda a, _p: self._follow(a))
        adjustment.connect("notify::page-size", lambda a, _p: self._follow(a))
        adjustment.connect("value-changed", self._on_scrolled)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._on_wheel)
        self._scroll.add_controller(wheel)
        grab = Gtk.GestureClick()  # pressing the scrollbar: the user is moving it
        grab.connect("pressed", lambda *_: setattr(self, "_stick", False))
        self._scroll.get_vscrollbar().add_controller(grab)
        self._views = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._views.add_named(hero, "hero")
        self._views.add_named(self._scroll, "chat")
        self.append(self._views)

        # One bar to type or speak, always at the bottom.
        self._entry = Gtk.Entry(placeholder_text="Ask Arcee…", hexpand=True)
        self._entry.connect("activate", lambda _e: self._send())
        self._entry.connect("changed", lambda _e: self._show_primary())
        self._mic = Gtk.Button(valign=Gtk.Align.CENTER)
        self._mic.add_css_class("circular")
        self._mic.add_css_class("arcee-mic")
        self._mic.connect("clicked", lambda _b: self._on_primary())
        composer = Gtk.Box(spacing=6)
        composer.add_css_class("arcee-composer")
        composer.append(self._entry)
        composer.append(self._mic)
        self.append(
            Adw.Clamp(
                child=composer,
                maximum_size=CHAT_WIDTH,
                margin_start=16,
                margin_end=16,
                margin_top=8,
                margin_bottom=16,
            )
        )
        self._show_primary()

        self._agent = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION,
            None,
            BUS_NAME,
            OBJECT_PATH,
            BUS_NAME,
            None,
        )
        self._agent.connect("g-signal", self._on_signal)
        self._agent.call("Status", None, Gio.DBusCallFlags.NONE, -1, None, self._on_status)
        self._show_hero(True)

    # --- layout -------------------------------------------------------------------------

    def _show_hero(self, hero: bool) -> None:
        """The ring large with no conversation, small in the bar above one."""
        ring, line = (
            (self._hero_ring, self._hero_status) if hero else (self._bar_ring, self._bar_status)
        )
        for widget, home in ((self.wave, ring), (self._status, line)):
            if widget.get_parent() is not home:
                if (parent := widget.get_parent()) is not None:
                    parent.remove(widget)  # type: ignore[attr-defined]
                home.append(widget)
        self.wave.set_size(HERO_PX if hero else BAR_PX)
        self._status.set_halign(Gtk.Align.CENTER if hero else Gtk.Align.START)
        self._bar.set_reveal_child(not hero)
        self._views.set_visible_child_name("hero" if hero else "chat")
        if hero:
            self._refresh_hero()

    def _refresh_hero(self) -> None:
        """The greeting, what Arcee runs on and the latest chats, as they are now."""
        self._greeting.set_label(_greeting())
        self._agent.call("Info", None, Gio.DBusCallFlags.NONE, -1, None, self._on_info)
        self._load_chats(self._recent_list, RECENT_CHATS, self._recent)

    def _on_info(self, proxy: Any, result: Any) -> None:
        try:
            info = json.loads(proxy.call_finish(result).unpack()[0])
        except GLib.Error:  # arceed is not running: what the settings say
            from arcee.settings import load

            settings = load()
            info = {"core": settings.core, "models": [], "wake_word": settings.listen}
        core = CORE_NAMES.get(info.get("core", ""), info.get("core", "") or "Arcee")
        models = info.get("models") or []
        model = f"{core} · {models[0]}" if models else core
        self._bar_model.set_label(model)
        self._bar_model.set_tooltip_text(
            "Models, first to last: " + ", ".join(models) if models else None
        )
        while (child := self._chips.get_first_child()) is not None:
            self._chips.remove(child)
        wake = bool(info.get("wake_word"))
        for text, on, tip in (
            (model, False, "The model that thinks for Arcee (Models to change it)"),
            ("“Hey Arcee” on" if wake else "“Hey Arcee” off", wake, "Settings → Voice"),
        ):
            chip = Gtk.Label(label=text, tooltip_text=tip)
            chip.add_css_class("arcee-chip")
            if on:
                chip.add_css_class("on")
            self._chips.append(chip)
        self._status.set_label(
            "Type below, or say “Hey Arcee”"
            if wake and not self._working
            else self._status.get_label()
        )

    def _load_chats(
        self, listbox: Gtk.ListBox, limit: int, holder: Gtk.Widget | None = None
    ) -> None:
        """The latest conversations from History into `listbox` (read off the main loop)."""

        def show(chats: list[Any]) -> None:
            listbox.remove_all()
            for chat in chats:
                when = time.strftime("%d %b %H:%M", time.localtime(chat.last))
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(chat.title[:80]),
                    subtitle=f"{chat.requests} request{'s' if chat.requests != 1 else ''} · {when}",
                    activatable=True,
                )
                row.chat_id = chat.id  # type: ignore[attr-defined]
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                listbox.append(row)
            if holder is not None:
                holder.set_visible(bool(chats))
            elif not chats:
                listbox.append(Adw.ActionRow(title="No earlier conversations yet"))

        _off_main(lambda store: store.conversations(limit), show)

    def _on_chat_row(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        chat_id = getattr(row, "chat_id", None)
        if chat_id is None or self._working:
            return
        if (popover := row.get_ancestor(Gtk.Popover)) is not None:
            popover.popdown()  # type: ignore[attr-defined]
        self._call("ResumeConversation", GLib.Variant("(s)", (chat_id,)))

        def show(rows: list[Any]) -> None:
            self._clear_log()
            for task, did in rows:
                self._add(task.request, "arcee-you", halign=Gtk.Align.END)
                for line in did[-4:]:
                    self._add("✓ " + line.split(": ", 1)[-1], "arcee-step")
                self._add(task.reply or task.status, "body")
                self._add_reply_line(task.reply or "", "")
            self._add("Earlier conversation: go on where it left off.", "arcee-meta")

        _off_main(lambda store: store.conversation(chat_id), show)

    def _run_text(self, text: str) -> None:
        self._call("Run", GLib.Variant("(s)", (text,)))

    def _show_primary(self) -> None:
        """The bar's one button: Stop while Arcee works, Send with text typed, else the mic."""
        if self._working:
            icon, tip = "media-playback-stop-symbolic", "Stop"
        elif self._entry.get_text().strip():
            icon, tip = "go-up-symbolic", "Send"
        elif self._listening:
            icon, tip = "media-playback-stop-symbolic", "Finish speaking"
        else:
            icon, tip = "audio-input-microphone-symbolic", "Speak"
        self._mic.set_icon_name(icon)
        self._mic.set_tooltip_text(tip)

    def _on_primary(self) -> None:
        if self._working:
            self._call("Cancel")
        elif self._entry.get_text().strip():
            self._send()
        else:
            self._call("StopListening" if self._listening else "StartListening")

    def _follow(self, adjustment: Gtk.Adjustment) -> None:
        # After this layout pass, not in it: a value set while GTK is still measuring
        # the wrapped text was put back where it was.
        if self._stick and not self._following:
            self._following = True
            self._scroll.add_tick_callback(self._to_end)

    def _to_end(self, _widget: Gtk.Widget, _clock: Any) -> bool:
        self._following = False
        if self._stick:
            adjustment = self._scroll.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        return GLib.SOURCE_REMOVE

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        # Back at the end, by any means: follow again. Only the user's own scrolling up
        # stops it (`_on_wheel`, the scrollbar): as wrapped text is measured GTK moves
        # the value back and forth by itself.
        if adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - STICK_PX:
            self._stick = True

    def _on_wheel(self, _controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        if dy < 0:
            self._stick = False
        return False  # the scrolled window scrolls as usual

    # --- talking to arceed ----------------------------------------------------------

    def _call(self, method: str, args: Any = None, quiet: bool = False) -> None:
        """Call arceed; a failure is said in the conversation unless `quiet` (calls the
        user did not make: with no arceed, New chat put an error in the empty page)."""

        def done(proxy: Any, result: Any) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as exc:
                if not quiet:
                    self._add(re.sub(r"^GDBus\.Error:[\w.\-]+:\s*", "", exc.message), "arcee-note")

        self._agent.call(method, args, Gio.DBusCallFlags.NONE, -1, None, done)

    def _send(self) -> None:
        text = self._entry.get_text().strip()
        if text:
            self._entry.set_text("")
            self._call("Run", GLib.Variant("(s)", (text,)))

    def _on_status(self, proxy: Any, result: Any) -> None:
        # Opened while Arcee works on something: show that, with Stop.
        try:
            status = json.loads(proxy.call_finish(result).unpack()[0])
        except GLib.Error:
            return  # arceed is not running yet; it starts on the first request
        if status.get("state") == "running":
            self._set_working(True, status.get("step") or "Working…")
            self._add(status.get("request", ""), "arcee-you", halign=Gtk.Align.END)

    def _set_working(self, working: bool, status: str) -> None:
        self._working = working
        self.wave.state = "working" if working else "idle"
        self._status.set_label(status)
        self._new.set_sensitive(not working)
        self._show_primary()
        if working:
            self._mic.add_css_class("working")
        else:
            self._mic.remove_css_class("working")

    def _clear_log(self) -> None:
        while (child := self._log.get_first_child()) is not None:
            self._log.remove(child)
        self._open_questions.clear()

    def _new_conversation(self) -> None:
        self._clear_log()
        self._call("EndConversation", quiet=True)  # arceed forgets the turns too
        self._show_hero(True)
        self._entry.grab_focus()

    def _on_signal(self, _proxy: Any, _sender: str, signal: str, params: Any) -> None:
        value: Any = params.unpack()[0] if params.n_children() else ""
        match signal:
            case "Listening":
                self._listening = bool(value)
                # While Arcee works the ring and the line belong to the work.
                if not self._working:
                    self.wave.state = "listening" if value else "idle"
                    self._status.set_label("Listening…" if value else IDLE_TEXT)
                if value:
                    self._mic.add_css_class("listening")
                else:
                    self._mic.remove_css_class("listening")
                self._show_primary()
                self._hint_answer(bool(value))
            case "Level":
                self.wave.set_level(float(value))
            case "Heard":
                heard = json.loads(value)
                if heard.get("error"):
                    self._add(heard["error"], "arcee-note")
                elif "answer" in heard:  # a question answered by voice
                    self._add(heard.get("text", ""), "arcee-you", halign=Gtk.Align.END)
            case "Answered":
                if (buttons := self._open_questions.pop(str(value), None)) is not None:
                    buttons.set_sensitive(False)
            case "Started":
                self._set_working(True, "Working…")
                self._add(value, "arcee-you", halign=Gtk.Align.END)
            case "Progress":
                # What was done, not the call itself ("→ app_open" said nothing).
                if not str(value).startswith(("→", "↑")):
                    self._add(value, "arcee-step")
            case "Step":
                if self._working:
                    self._status.set_label(value)
            case "Question":
                self._ask(json.loads(value))
            case "Finished":
                done = json.loads(value)
                self._set_working(False, IDLE_TEXT)
                # A question left unanswered can no longer be answered.
                for card in self._open_questions.values():
                    card.set_sensitive(False)
                self._open_questions.clear()
                reply = done.get("reply") or (
                    "Stopped." if done.get("status") == "stopped" else done.get("status", "")
                )
                self._add(reply, "body")
                self._add_reply_line(done.get("reply") or "", _meta(done))
            case _:
                pass

    # --- the conversation -------------------------------------------------------------

    def _add(self, text: str, style: str, halign: Gtk.Align = Gtk.Align.FILL) -> None:
        if not text:
            return
        label = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            xalign=0,
            selectable=True,
            halign=halign,
        )
        if style == "body":
            show_markdown(label, text)  # Arcee's replies come in Markdown
        else:
            label.set_text(text)  # what the user said, errors and notes, as they are
        label.add_css_class(style)
        if style == "arcee-you":
            label.set_margin_start(96)  # a bubble on the right, never the full width
        self._append(label)

    def _add_reply_line(self, reply: str, meta: str) -> None:
        """Under a reply: its meta line, and Read aloud (decision 0019)."""
        line = Gtk.Box(spacing=6)
        if meta:
            label = Gtk.Label(label=meta, xalign=0, wrap=True, hexpand=True)
            label.add_css_class("arcee-meta")
            line.append(label)
        else:
            line.append(Gtk.Box(hexpand=True))
        if reply.strip():
            line.append(self._read_aloud_button(reply))
        if meta or reply.strip():
            self._append(line)

    def _read_aloud_button(self, reply: str) -> Gtk.Button:
        button = Gtk.Button(
            icon_name="audio-volume-high-symbolic",
            tooltip_text="Read aloud",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        saying: dict[str, int] = {}  # the timer that turns it back, while it says it

        def back() -> bool:
            saying.pop("timer", None)
            button.set_icon_name("audio-volume-high-symbolic")
            button.set_tooltip_text("Read aloud")
            return False

        def said(proxy: Any, result: Any) -> None:
            try:
                (why,) = proxy.call_finish(result).unpack()
            except GLib.Error as exc:
                why = re.sub(r"^GDBus\.Error:[\w.\-]+:\s*", "", exc.message)
            if why:
                back()
                self._add(why, "arcee-note")

        def clicked(_b: Gtk.Button) -> None:
            if "timer" in saying:  # saying it: stop
                GLib.source_remove(saying["timer"])
                back()
                self._call("Hush", quiet=True)
                return
            button.set_icon_name("media-playback-stop-symbolic")
            button.set_tooltip_text("Stop reading")
            seconds = 2 + len(reply.split()) / 2.6  # about 155 words a minute
            saying["timer"] = GLib.timeout_add_seconds(int(seconds), back)
            self._agent.call(
                "Say", GLib.Variant("(s)", (reply,)), Gio.DBusCallFlags.NONE, -1, None, said
            )

        button.connect("clicked", clicked)
        return button

    def _append(self, widget: Gtk.Widget) -> None:
        self._show_hero(False)
        self._stick = True  # what the user just did, or Arcee's answer to it: show it
        self._log.append(widget)
        # A selectable label selects its text when it first takes focus; undo that.
        if isinstance(widget, Gtk.Label):
            GLib.idle_add(lambda: widget.select_region(0, 0) and False)

    def _hint_answer(self, listening: bool) -> None:
        """Under the newest open question: what to say while the mic listens for it."""
        for card in self._open_questions.values():
            hint = getattr(card, "hint", None)
            if hint is not None:
                hint.set_visible(listening)

    def _ask(self, question: dict[str, Any]) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("arcee-question")
        text = Gtk.Label(
            label=question.get("text", ""), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0
        )
        text.add_css_class("heading")
        card.append(text)
        for target in question.get("preview", []):
            line = Gtk.Label(label=target, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            line.add_css_class("dim-label")
            card.append(line)
        buttons = Gtk.Box(spacing=8)

        def answer(value: str) -> None:
            card.set_sensitive(False)
            self._open_questions.pop(question["id"], None)
            self._call("Answer", GLib.Variant("(ss)", (question["id"], value)))

        confirm = question.get("kind") == "confirm"
        if confirm:
            choices = [("Allow", "yes", "suggested-action"), ("Deny", "no", "")]
            if question.get("allowable"):  # routine, and undoable: may stop asking
                choices.insert(1, ("Always allow", "always", ""))
            for label, value, style in choices:
                button = Gtk.Button(label=label)
                if style:
                    button.add_css_class(style)
                button.connect("clicked", lambda _b, v=value: answer(v))
                buttons.append(button)
        elif question.get("options"):
            for option in question["options"]:
                button = Gtk.Button(label=option)
                button.connect("clicked", lambda _b, v=option: answer(v))
                buttons.append(button)
        else:
            entry = Gtk.Entry(placeholder_text="Your answer", hexpand=True)
            entry.connect("activate", lambda e: answer(e.get_text()))
            buttons.append(entry)
        card.append(buttons)
        # While the mic listens for the answer (a spoken request): what to say.
        saying = (
            "Listening… say “allow” or “deny”, or click"
            if confirm
            else "Listening… say your choice, or click"
            if question.get("options")
            else "Listening… say your answer"
        )
        hint = Gtk.Label(label=saying, xalign=0, visible=self._listening)
        hint.add_css_class("arcee-note")
        card.append(hint)
        card.hint = hint  # type: ignore[attr-defined]
        self._open_questions[question["id"]] = card
        self._append(card)
