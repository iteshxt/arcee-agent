# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""The app's Agent page: talk to Arcee from the app, as from the panel.

A circle of glowing particles in sunrise colours breathes while Arcee waits, pulses
with the voice while the mic listens, and swirls while it works. Below it: the mic,
a button to type instead, and the conversation (requests, steps, questions with their
answers, replies). It talks to arceed over D-Bus on the GTK main loop, like the panel;
what the user says is run by arceed itself, so the panel and the app never both run it.
"""

import json
import math
import random
import re
import time
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

BUS_NAME: Final = "me.iteshxt.Arcee.Agent"
OBJECT_PATH: Final = "/me/iteshxt/Arcee/Agent"
# Sunrise: deep orange at the inside of the ring to gold at the outside.
SUN_INNER: Final = (1.0, 0.42, 0.05)
SUN_OUTER: Final = (1.0, 0.80, 0.30)
RIBBONS: Final = 5
POINTS: Final = 220
WAVE_SIZE: Final = 300
# The app's own touches: the mic in sunrise orange, red while it listens.
CSS: Final = b"""
.arcee-mic { background: #ff7a1a; color: white; min-width: 64px; min-height: 64px; }
.arcee-mic:hover { background: #ff8f3d; }
.arcee-mic.listening, .arcee-mic.listening:hover { background: #d93a2b; color: white; }
.arcee-you { background: alpha(#ff7a1a, 0.16); border-radius: 14px; padding: 8px 12px; }
"""


class SunWave(Gtk.DrawingArea):
    """The ring of particles. `state` is idle, listening or working; `level` the voice."""

    def __init__(self) -> None:
        super().__init__(content_width=WAVE_SIZE, content_height=WAVE_SIZE, halign=Gtk.Align.CENTER)
        self.state = "idle"
        self._level = 0.0
        self._target = 0.0
        self._start = time.monotonic()
        # Each ribbon wobbles in its own way; each particle sits a little off the line.
        self._ribbons = [
            (random.uniform(0, math.tau), random.uniform(2, 5), random.uniform(0.3, 0.9))
            for _ in range(RIBBONS)
        ]
        self._jitter = [[random.gauss(0, 1) for _ in range(POINTS)] for _ in range(RIBBONS)]
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)

    def set_level(self, level: float) -> None:
        # Speech sits around 0.02-0.2 RMS; stretch it so quiet talk still moves the ring.
        self._target = min(1.0, math.sqrt(max(level, 0.0)) * 2.5)

    def _tick(self, _widget: Any, _clock: Any) -> bool:
        target = self._target if self.state == "listening" else 0.0
        self._level += (target - self._level) * 0.25
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _area: Any, cr: Any, width: int, height: int) -> None:
        t = time.monotonic() - self._start
        cx, cy = width / 2, height / 2
        base = min(width, height) * 0.32
        # A dark stage, so the light reads in a light or dark theme alike.
        cr.set_source_rgb(0.04, 0.03, 0.03)
        cr.arc(cx, cy, min(width, height) / 2, 0, math.tau)
        cr.fill()
        speed = {"idle": 0.25, "listening": 0.6, "working": 1.4}[self.state]
        breathe = 0.04 * math.sin(t * 1.3)
        energy = 0.06 + breathe + self._level * 0.2 + (0.06 if self.state == "working" else 0)
        for r, (phase, waves, drift) in enumerate(self._ribbons):
            fraction = r / max(RIBBONS - 1, 1)
            red, green, blue = (
                SUN_INNER[i] + (SUN_OUTER[i] - SUN_INNER[i]) * fraction for i in range(3)
            )
            for i in range(POINTS):
                angle = math.tau * i / POINTS
                wobble = math.sin(angle * waves + phase + t * speed * (1 + drift))
                wobble += 0.5 * math.sin(angle * (waves + 3) - t * speed * drift)
                radius = base * (1 + energy * wobble + 0.012 * self._jitter[r][i])
                x = cx + radius * math.cos(angle + t * 0.05 * speed)
                y = cy + radius * math.sin(angle + t * 0.05 * speed)
                alpha = 0.35 + 0.5 * (0.5 + 0.5 * wobble)
                cr.set_source_rgba(red, green, blue, alpha)
                cr.rectangle(x, y, 1.8, 1.8)
                cr.fill()
        # A soft core that brightens with the voice.
        glow = 0.12 + self._level * 0.3
        cr.set_source_rgba(*SUN_INNER, glow)
        cr.arc(cx, cy, base * 0.14 * (1 + 0.4 * self._level), 0, math.tau)
        cr.fill()


class AgentPage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        css = Gtk.CssProvider()
        css.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.set_margin_top(18)
        self.set_margin_bottom(12)
        self._listening = False
        self.wave = SunWave()
        self.append(self.wave)

        self._status = Gtk.Label(label="Ask me anything", halign=Gtk.Align.CENTER)
        self._status.add_css_class("dim-label")
        self.append(self._status)

        controls = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER)
        self._mic = Gtk.Button(icon_name="audio-input-microphone-symbolic", tooltip_text="Speak")
        self._mic.add_css_class("circular")
        self._mic.add_css_class("arcee-mic")
        self._mic.connect("clicked", lambda _b: self._toggle_listening())
        typing = Gtk.ToggleButton(icon_name="input-keyboard-symbolic", tooltip_text="Type")
        typing.add_css_class("circular")
        typing.set_size_request(48, 48)
        typing.set_valign(Gtk.Align.CENTER)
        controls.append(typing)
        controls.append(self._mic)
        self.append(controls)

        self._entry = Gtk.Entry(placeholder_text="Ask Arcee…", hexpand=True)
        self._entry.connect("activate", lambda _e: self._send())
        send = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Send")
        send.add_css_class("circular")
        send.connect("clicked", lambda _b: self._send())
        row = Gtk.Box(spacing=8)
        row.append(self._entry)
        row.append(send)
        self._typing = Gtk.Revealer(child=Adw.Clamp(child=row, maximum_size=520))
        typing.connect("toggled", self._on_typing)
        self.append(self._typing)

        self._log = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._scroll = Gtk.ScrolledWindow(
            child=Adw.Clamp(child=self._log, maximum_size=560, margin_start=12, margin_end=12),
            vexpand=True,
        )
        self.append(self._scroll)

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

    # --- talking to arceed ----------------------------------------------------------

    def _call(self, method: str, args: Any = None) -> None:
        def done(proxy: Any, result: Any) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as exc:
                self._add(re.sub(r"^GDBus\.Error:[\w.\-]+:\s*", "", exc.message), "dim-label")

        self._agent.call(method, args, Gio.DBusCallFlags.NONE, -1, None, done)

    def _send(self) -> None:
        text = self._entry.get_text().strip()
        if text:
            self._entry.set_text("")
            self._call("Run", GLib.Variant("(s)", (text,)))

    def _toggle_listening(self) -> None:
        self._call("StopListening" if self._listening else "StartListening")

    def _on_typing(self, button: Gtk.ToggleButton) -> None:
        self._typing.set_reveal_child(button.get_active())
        if button.get_active():
            self._entry.grab_focus()

    def _on_signal(self, _proxy: Any, _sender: str, signal: str, params: Any) -> None:
        value: Any = params.unpack()[0] if params.n_children() else ""
        match signal:
            case "Listening":
                self._listening = bool(value)
                self.wave.state = "listening" if value else "idle"
                self._status.set_label("Listening…" if value else "Ask me anything")
                if value:
                    self._mic.add_css_class("listening")
                else:
                    self._mic.remove_css_class("listening")
            case "Level":
                self.wave.set_level(float(value))
            case "Heard":
                heard = json.loads(value)
                if heard.get("error"):
                    self._add(heard["error"], "dim-label")
            case "Started":
                self.wave.state = "working"
                self._status.set_label("Working…")
                self._add(value, "arcee-you", halign=Gtk.Align.END)
            case "Progress":
                self._add(value, "dim-label")
            case "Question":
                self._ask(json.loads(value))
            case "Finished":
                done = json.loads(value)
                self.wave.state = "idle"
                self._status.set_label("Ask me anything")
                reply = done.get("reply") or (
                    "Stopped." if done.get("status") == "stopped" else done.get("status", "")
                )
                self._add(reply, "body")
            case _:
                pass

    # --- the conversation -------------------------------------------------------------

    def _add(self, text: str, style: str, halign: Gtk.Align = Gtk.Align.START) -> None:
        label = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True, halign=halign)
        label.add_css_class(style)
        if style == "arcee-you":
            label.set_margin_start(48)
        self._log.append(label)
        GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        adjustment = self._scroll.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return GLib.SOURCE_REMOVE

    def _ask(self, question: dict[str, Any]) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        for side in ("start", "end", "top", "bottom"):
            getattr(card, f"set_margin_{side}")(4)
        text = Gtk.Label(label=question.get("text", ""), wrap=True, xalign=0)
        text.add_css_class("heading")
        card.append(text)
        for target in question.get("preview", []):
            card.append(Gtk.Label(label=target, xalign=0, wrap=True))
        buttons = Gtk.Box(spacing=8)

        def answer(value: str) -> None:
            buttons.set_sensitive(False)
            self._call("Answer", GLib.Variant("(ss)", (question["id"], value)))

        if question.get("kind") == "confirm":
            for label, value, style in (("Allow", "yes", "suggested-action"), ("Deny", "no", "")):
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
        self._log.append(card)
        GLib.idle_add(self._scroll_to_end)
