"""The desktop's settings, one safe call each: sound, screen, appearance, notifications,
radios, Bluetooth, accessibility, power, default apps and updates.

Each was a shell command the model had to know and get right (wpctl's sink name, a
gsettings schema, nmcli's words), and each then needed a confirmation as a write. Here
each setting is a tool with plain arguments; the ones that cannot be undone from where
the user sits (Wi-Fi off cuts Arcee's own models; power off, restart; default apps) ask
first. No sudo anywhere: logind sets the brightness and powers off, as the Shell does.
"""

import json
import re
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, cast

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.tools.apps import application_dirs, best_match, installed_apps
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.commands import Commands, SystemCommands, cancel_later, later

SINK: Final = "@DEFAULT_AUDIO_SINK@"
MAX_VOLUME: Final = 150  # wpctl's limit, as GNOME's "over-amplification"
STEP: Final = 10  # "louder", "dimmer": this many points
BACKLIGHT: Final = Path("/sys/class/backlight")
DND_UNIT: Final = "arcee-dnd-end"
POWER_UNIT: Final = "arcee-power"
_INTERFACE: Final = "org.gnome.desktop.interface"


def _pct(fraction: float) -> int:
    return round(fraction * 100)


class _Settings:
    """Holds the command runner every settings tool shares."""

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()


async def _gsettings(commands: Commands, schema: str, key: str, value: str | None = None) -> str:
    argv = ["gsettings", "set" if value is not None else "get", schema, key]
    done = await commands.run([*argv, value] if value is not None else argv)
    if not done.ok:
        raise SettingsError(f"gsettings {key}: {done.why()}")
    return done.out.strip()


class SettingsError(Exception):
    pass


# --- settings.volume --------------------------------------------------------------------


class VolumeArgs(ToolArgs):
    action: Literal["get", "set", "up", "down", "mute", "unmute"] = Field(
        description="get, set (to percent), up or down (by percent, default 10), mute, unmute"
    )
    percent: int | None = Field(default=None, ge=0, le=MAX_VOLUME, description="0-150")


def parse_volume(text: str) -> tuple[int, bool]:
    """wpctl's "Volume: 0.78 [MUTED]" as (78, True)."""
    m = re.search(r"Volume:\s*([\d.]+)", text)
    if m is None:
        raise SettingsError(f"wpctl said: {text.strip()[:80]}")
    return _pct(float(m[1])), "MUTED" in text


class SettingsVolume(_Settings, Tool[VolumeArgs]):
    name = "settings.volume"
    description = "Get or change the sound volume of the speakers or headphones in use."
    usage = Usage(
        when='"Turn it up", "volume to 30%", "mute", "how loud is it?".',
        avoid="Pausing music (media.control); choosing headphones (settings.audio_output).",
        example='settings.volume(action="set", percent=30)',
    )
    args_model = VolumeArgs
    timeout_s = 10

    async def run(self, args: VolumeArgs, ctx: ToolContext) -> ToolResult:
        run = self._commands.run
        if args.action == "set":
            if args.percent is None:
                return ToolResult.fail("say the percent to set it to")
            done = await run(["wpctl", "set-volume", "-l", "1.5", SINK, f"{args.percent}%"])
        elif args.action in ("up", "down"):
            sign = "+" if args.action == "up" else "-"
            step = args.percent if args.percent is not None else STEP
            done = await run(["wpctl", "set-volume", "-l", "1.5", SINK, f"{step}%{sign}"])
        elif args.action in ("mute", "unmute"):
            done = await run(["wpctl", "set-mute", SINK, "1" if args.action == "mute" else "0"])
        else:
            done = None
        if done is not None and not done.ok:
            return ToolResult.fail(f"could not change the volume: {done.why()}")
        now = await run(["wpctl", "get-volume", SINK])
        if not now.ok:
            return ToolResult.fail(f"could not read the volume: {now.why()}")
        level, muted = parse_volume(now.out)
        said = f"Volume {level}%" + (" (muted)" if muted else "")
        return ToolResult.ok(said, data={"volume_percent": level, "muted": muted})


# --- settings.audio_output --------------------------------------------------------------


class OutputArgs(ToolArgs):
    device: str = Field(
        default="",
        description="Part of the device's name (headphones, HDMI, built-in); empty to list them",
    )
    kind: Literal["output", "input"] = Field(
        default="output", description="output (speakers, headphones) or input (microphone)"
    )


def audio_devices(dump: str, kind: str) -> tuple[list[dict[str, Any]], str]:
    """From pw-dump's JSON: the sinks (or sources) as {id, name, description}, and the
    default one's name."""
    media_class = "Audio/Sink" if kind == "output" else "Audio/Source"
    key = "default.audio.sink" if kind == "output" else "default.audio.source"
    devices: list[dict[str, Any]] = []
    default = ""
    objects = cast(list[Any], json.loads(dump or "[]"))
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        node = cast(dict[str, Any], obj)
        info = cast(dict[str, Any], node.get("info") or {})
        props = cast(dict[str, Any], info.get("props") or {})
        if props.get("media.class") == media_class:
            devices.append(
                {
                    "id": node.get("id"),
                    "name": str(props.get("node.name", "")),
                    "description": str(props.get("node.description", "")),
                }
            )
        if node.get("type") == "PipeWire:Interface:Metadata":
            for entry in cast(list[Any], node.get("metadata") or []):
                item = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
                value = item.get("value")
                if item.get("key") == key and isinstance(value, dict):
                    default = str(cast(dict[str, Any], value).get("name", ""))
    return devices, default


class SettingsAudioOutput(_Settings, Tool[OutputArgs]):
    name = "settings.audio_output"
    description = "List the sound outputs (or microphones), or switch to one by name."
    usage = Usage(
        when='"Play sound through my headphones", "switch audio to the TV (HDMI)", "which '
        'speakers are on?", "use the built-in mic".',
        avoid="Connecting a Bluetooth device first: bluetooth.devices connects it.",
        example='settings.audio_output(device="HDMI")',
    )
    args_model = OutputArgs
    timeout_s = 10

    async def run(self, args: OutputArgs, ctx: ToolContext) -> ToolResult:
        dump = await self._commands.run(["pw-dump"])
        if not dump.ok:
            return ToolResult.fail(f"could not list the sound devices: {dump.why()}")
        try:
            devices, default = audio_devices(dump.out, args.kind)
        except ValueError as exc:
            return ToolResult.fail(f"pw-dump's list could not be read: {exc}")
        if not args.device:
            listed = [
                d["description"] + (" (in use)" if d["name"] == default else "") for d in devices
            ]
            return ToolResult.ok(f"{len(devices)} {args.kind}s", data={"devices": listed})
        want = args.device.lower()
        found = [d for d in devices if want in d["description"].lower() or want in d["name"]]
        if not found:
            names = [d["description"] for d in devices]
            return ToolResult.fail(f"no {args.kind} matches “{args.device}”; there are: {names}")
        chosen = found[0]
        done = await self._commands.run(["wpctl", "set-default", str(chosen["id"])])
        if not done.ok:
            return ToolResult.fail(f"could not switch: {done.why()}")
        return ToolResult.ok(
            f"Sound {args.kind} is now {chosen['description']}",
            data={"in_use": chosen["description"]},
        )


# --- settings.brightness ----------------------------------------------------------------


class BrightnessArgs(ToolArgs):
    action: Literal["get", "set", "up", "down"] = Field(description="get, set, up or down")
    percent: int | None = Field(default=None, ge=1, le=100, description="1-100")


def backlight(root: Path = BACKLIGHT) -> Path | None:
    """The screen's backlight: firmware first, then platform, then raw (as GNOME picks)."""
    if not root.is_dir():
        return None
    ranked: list[tuple[int, Path]] = []
    for device in root.iterdir():
        kind = (device / "type").read_text().strip() if (device / "type").exists() else "raw"
        order = {"firmware": 0, "platform": 1, "raw": 2}.get(kind, 3)
        ranked.append((order, device))
    return min(ranked)[1] if ranked else None


async def logind_brightness(device: str, value: int) -> None:
    """logind's Session.SetBrightness: the active session's user may, without root."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(
                destination="org.freedesktop.login1",
                path="/org/freedesktop/login1/session/auto",
                interface="org.freedesktop.login1.Session",
                member="SetBrightness",
                signature="ssu",
                body=["backlight", device, value],
            )
        )
        if reply.message_type is MessageType.ERROR:
            raise SettingsError(str(reply.body[0] if reply.body else reply.error_name))
    finally:
        bus.disconnect()


class SettingsBrightness(Tool[BrightnessArgs]):
    name = "settings.brightness"
    description = "Get or change the screen's brightness (laptop screens; not monitors)."
    usage = Usage(
        when='"Dim the screen", "brightness to 50%", "brighter".',
        example='settings.brightness(action="set", percent=50)',
    )
    args_model = BrightnessArgs
    timeout_s = 10

    def __init__(self, root: Path = BACKLIGHT, setter: Any = logind_brightness) -> None:
        self._root = root
        self._set = setter

    async def run(self, args: BrightnessArgs, ctx: ToolContext) -> ToolResult:
        device = backlight(self._root)
        if device is None:
            return ToolResult.fail("this screen's brightness cannot be set from here")
        top = int((device / "max_brightness").read_text())
        now = int((device / "brightness").read_text())
        percent = round(now * 100 / top)
        if args.action != "get":
            if args.action == "set":
                if args.percent is None:
                    return ToolResult.fail("say the percent to set it to")
                percent = args.percent
            else:
                step = args.percent if args.percent is not None else STEP
                percent += step if args.action == "up" else -step
            percent = min(100, max(1, percent))  # never fully dark
            try:
                await self._set(device.name, max(1, round(percent * top / 100)))
            except Exception as exc:
                return ToolResult.fail(f"could not set the brightness: {exc}")
        return ToolResult.ok(f"Brightness {percent}%", data={"brightness_percent": percent})


# --- settings.appearance ----------------------------------------------------------------


class AppearanceArgs(ToolArgs):
    dark_mode: bool | None = Field(default=None, description="true dark, false light")
    night_light: bool | None = Field(default=None, description="warmer colours at night")
    wallpaper: str = Field(default="", description="An image file to use as the background")


class SettingsAppearance(_Settings, Tool[AppearanceArgs]):
    name = "settings.appearance"
    description = "Dark or light mode, Night Light, and the wallpaper. No arguments: what is set."
    usage = Usage(
        when='"Switch to dark mode", "turn on night light", "set this photo as my wallpaper".',
        example="settings.appearance(dark_mode=true)",
    )
    args_model = AppearanceArgs
    timeout_s = 10

    async def run(self, args: AppearanceArgs, ctx: ToolContext) -> ToolResult:
        c = self._commands
        light = "org.gnome.settings-daemon.plugins.color"
        try:
            if args.dark_mode is not None:
                scheme = "'prefer-dark'" if args.dark_mode else "'default'"
                await _gsettings(c, _INTERFACE, "color-scheme", scheme)
            if args.night_light is not None:
                on = "true" if args.night_light else "false"
                await _gsettings(c, light, "night-light-enabled", on)
            if args.wallpaper:
                image = _resolve(args.wallpaper, ctx.home)
                if not image.is_file():
                    return ToolResult.fail(f"{args.wallpaper} is not a file")
                uri = f"'{image.as_uri()}'"
                for key in ("picture-uri", "picture-uri-dark"):
                    await _gsettings(c, "org.gnome.desktop.background", key, uri)
            dark = "dark" in await _gsettings(c, _INTERFACE, "color-scheme")
            night = await _gsettings(c, light, "night-light-enabled") == "true"
        except SettingsError as exc:
            return ToolResult.fail(str(exc))
        state = f"{'Dark' if dark else 'Light'} mode, Night Light {'on' if night else 'off'}"
        data: dict[str, Any] = {"dark_mode": dark, "night_light": night}
        if args.wallpaper:
            data["wallpaper"] = args.wallpaper
        return ToolResult.ok(state, data=data)


def _resolve(path: str, home: Path) -> Path:
    image = Path(path).expanduser()
    return image if image.is_absolute() else home / image


# --- settings.dnd -----------------------------------------------------------------------


class DndArgs(ToolArgs):
    on: bool = Field(description="true: no notification pop-ups; false: show them again")
    minutes: int | None = Field(
        default=None, ge=1, le=24 * 60, description="Turn it off again after this long"
    )


class SettingsDnd(_Settings, Tool[DndArgs]):
    name = "settings.dnd"
    description = "Do Not Disturb: hide notification pop-ups, for a while or until turned off."
    usage = Usage(
        when='"Turn on do not disturb", "silence notifications for an hour", "focus mode '
        'for 25 minutes" (with a timer: reminder.add).',
        example="settings.dnd(on=true, minutes=60)",
    )
    args_model = DndArgs
    timeout_s = 15

    async def run(self, args: DndArgs, ctx: ToolContext) -> ToolResult:
        schema = "org.gnome.desktop.notifications"
        try:
            await _gsettings(self._commands, schema, "show-banners", "false" if args.on else "true")
        except SettingsError as exc:
            return ToolResult.fail(str(exc))
        await cancel_later(self._commands, DND_UNIT)
        if not args.on:
            return ToolResult.ok("Do Not Disturb is off", data={"dnd": False})
        if args.minutes is None:
            return ToolResult.ok("Do Not Disturb is on", data={"dnd": True})
        argv = ["gsettings", "set", schema, "show-banners", "true"]
        done = await later(self._commands, DND_UNIT, args.minutes, argv)
        if not done.ok:
            return ToolResult.fail(f"Do Not Disturb is on, but not timed: {done.why()}")
        return ToolResult.ok(
            f"Do Not Disturb is on for {args.minutes} minutes",
            data={"dnd": True, "minutes": args.minutes},
        )


# --- settings.radio ---------------------------------------------------------------------


class RadioArgs(ToolArgs):
    radio: Literal["wifi", "bluetooth"]
    on: bool | None = Field(default=None, description="true on, false off; omit to ask")


class SettingsRadio(_Settings, Tool[RadioArgs]):
    name = "settings.radio"
    description = "Turn Wi-Fi or Bluetooth on or off, or say whether it is on."
    usage = Usage(
        when='"Turn off Wi-Fi", "turn on Bluetooth", "is Bluetooth on?".',
        avoid="Connecting a device: bluetooth.devices.",
        example='settings.radio(radio="bluetooth", on=true)',
    )
    args_model = RadioArgs
    timeout_s = 15

    def tier_for(self, args: RadioArgs, policy: Policy) -> Tier:
        # Wi-Fi off cuts Arcee's own cloud models: the user may not get it back by asking.
        return Tier.CONFIRM if args.radio == "wifi" and args.on is False else Tier.AUTO

    def sentence(self, args: RadioArgs) -> str:
        return "Turn Wi-Fi off? Arcee's cloud models stop working until it is back on."

    async def run(self, args: RadioArgs, ctx: ToolContext) -> ToolResult:
        run = self._commands.run
        if args.radio == "wifi":
            if args.on is not None:
                done = await run(["nmcli", "radio", "wifi", "on" if args.on else "off"])
                if not done.ok:
                    return ToolResult.fail(f"could not switch Wi-Fi: {done.why()}")
            state = await run(["nmcli", "radio", "wifi"])
            on = state.out.strip() == "enabled"
        else:
            if args.on is not None:
                done = await run(["bluetoothctl", "power", "on" if args.on else "off"])
                if not done.ok or "Failed" in done.out:
                    return ToolResult.fail(f"could not switch Bluetooth: {done.why()}")
            state = await run(["bluetoothctl", "show"])
            if "No default controller" in state.out + state.err:
                return ToolResult.fail("this computer has no Bluetooth adapter")
            on = bool(re.search(r"Powered:\s*yes", state.out))
        name = "Wi-Fi" if args.radio == "wifi" else "Bluetooth"
        return ToolResult.ok(f"{name} is {'on' if on else 'off'}", data={args.radio: on})


# --- bluetooth.devices ------------------------------------------------------------------


class BluetoothArgs(ToolArgs):
    action: Literal["list", "connect", "disconnect"] = "list"
    device: str = Field(default="", description="Part of the paired device's name")


def paired(text: str) -> list[tuple[str, str]]:
    """bluetoothctl's "Device A8:E6:E8:B8:05:3E JBL Tune 770NC" lines as (address, name)."""
    return re.findall(r"^Device ([0-9A-F:]{17}) (.+)$", text, re.MULTILINE)


class BluetoothDevices(_Settings, Tool[BluetoothArgs]):
    name = "bluetooth.devices"
    description = "The paired Bluetooth devices, and connect or disconnect one by name."
    usage = Usage(
        when='"Connect my headphones", "disconnect the buds", "which Bluetooth devices do I '
        'have?".',
        avoid="Pairing a new device: that needs the device in pairing mode and Settings.",
        example='bluetooth.devices(action="connect", device="JBL")',
    )
    args_model = BluetoothArgs
    timeout_s = 30

    async def run(self, args: BluetoothArgs, ctx: ToolContext) -> ToolResult:
        run = self._commands.run
        listing = await run(["bluetoothctl", "devices", "Paired"])
        devices = paired(listing.out)
        connected = {
            address
            for address, _ in paired((await run(["bluetoothctl", "devices", "Connected"])).out)
        }
        if args.action == "list" or not args.device:
            names = [n + (" (connected)" if a in connected else "") for a, n in devices]
            return ToolResult.ok(f"{len(devices)} paired devices", data={"devices": names})
        want = args.device.lower()
        found = [(a, n) for a, n in devices if want in n.lower()]
        if not found:
            names = [n for _, n in devices]
            return ToolResult.fail(f"no paired device matches “{args.device}”; paired: {names}")
        address, name = found[0]
        power = await run(["bluetoothctl", "show"])
        if args.action == "connect" and not re.search(r"Powered:\s*yes", power.out):
            await run(["bluetoothctl", "power", "on"])
        done = await run(["bluetoothctl", args.action, address], limit_s=25)
        if not done.ok or "Failed" in done.out:
            return ToolResult.fail(
                f"could not {args.action} {name}: {done.why()} (is it on and nearby?)"
            )
        verb = "Connected to" if args.action == "connect" else "Disconnected"
        return ToolResult.ok(f"{verb} {name}", data={args.action + "ed": name})


# --- settings.accessibility -------------------------------------------------------------


class AccessibilityArgs(ToolArgs):
    text_size: float | None = Field(
        default=None, ge=0.5, le=3.0, description="Text scale: 1 normal, 1.25 large, 1.5 larger"
    )
    zoom: bool | None = Field(default=None, description="The screen magnifier on or off")
    high_contrast: bool | None = None


class SettingsAccessibility(_Settings, Tool[AccessibilityArgs]):
    name = "settings.accessibility"
    description = "Bigger or smaller text everywhere, the screen magnifier, high contrast."
    usage = Usage(
        when='"Make the text bigger", "zoom in on the screen", "text back to normal".',
        avoid="Zooming one web page or document: kbd.key ctrl+plus in it.",
        example="settings.accessibility(text_size=1.25)",
    )
    args_model = AccessibilityArgs
    timeout_s = 10

    async def run(self, args: AccessibilityArgs, ctx: ToolContext) -> ToolResult:
        c = self._commands
        magnifier = ("org.gnome.desktop.a11y.applications", "screen-magnifier-enabled")
        contrast = ("org.gnome.desktop.a11y.interface", "high-contrast")
        try:
            if args.text_size is not None:
                await _gsettings(c, _INTERFACE, "text-scaling-factor", f"{args.text_size:g}")
            if args.zoom is not None:
                await _gsettings(c, *magnifier, "true" if args.zoom else "false")
            if args.high_contrast is not None:
                await _gsettings(c, *contrast, "true" if args.high_contrast else "false")
            size = float(await _gsettings(c, _INTERFACE, "text-scaling-factor"))
            zoom = await _gsettings(c, *magnifier) == "true"
            high = await _gsettings(c, *contrast) == "true"
        except SettingsError as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(
            f"Text size {size:g}, magnifier {'on' if zoom else 'off'}",
            data={"text_size": size, "zoom": zoom, "high_contrast": high},
        )


# --- session.power ----------------------------------------------------------------------


class PowerArgs(ToolArgs):
    action: Literal["lock", "suspend", "restart", "power_off", "log_out", "cancel"] = Field(
        description="lock, suspend (sleep), restart, power_off, log_out, or cancel a delayed one"
    )
    in_minutes: int | None = Field(default=None, ge=1, le=24 * 60, description="Delay")


_POWER: Final = {
    "lock": ["loginctl", "lock-session"],
    "suspend": ["systemctl", "suspend"],
    "restart": ["systemctl", "reboot"],
    "power_off": ["systemctl", "poweroff"],
    "log_out": ["gnome-session-quit", "--logout", "--no-prompt"],
}
_POWER_WORDS: Final = {
    "lock": "Lock the screen",
    "suspend": "Put the computer to sleep",
    "restart": "Restart the computer",
    "power_off": "Shut down the computer",
    "log_out": "Log out",
}


class SessionPower(_Settings, Tool[PowerArgs]):
    name = "session.power"
    description = "Lock the screen, sleep, restart, shut down or log out, now or after a delay."
    usage = Usage(
        when='"Lock my computer", "shut down in 10 minutes", "restart", "cancel the shutdown".',
        mistakes="Restart, shut down and log out close every app: unsaved work is lost.",
        example='session.power(action="power_off", in_minutes=10)',
    )
    args_model = PowerArgs
    timeout_s = 15

    def tier_for(self, args: PowerArgs, policy: Policy) -> Tier:
        return Tier.AUTO if args.action in ("lock", "cancel") else Tier.CONFIRM

    def allowable(self, args: PowerArgs) -> bool:
        return args.action in ("lock", "cancel", "suspend")

    def sentence(self, args: PowerArgs) -> str:
        when = f" in {args.in_minutes} minutes" if args.in_minutes else " now"
        lost = " Unsaved work in open apps is lost." if args.action != "suspend" else ""
        return f"{_POWER_WORDS.get(args.action, args.action)}{when}?{lost}"

    async def run(self, args: PowerArgs, ctx: ToolContext) -> ToolResult:
        if args.action == "cancel":
            if await cancel_later(self._commands, POWER_UNIT):
                return ToolResult.ok("Cancelled", data={"cancelled": True})
            return ToolResult.ok("Nothing was waiting to happen", data={"cancelled": False})
        argv = _POWER[args.action]
        if args.in_minutes:
            done = await later(self._commands, POWER_UNIT, args.in_minutes, argv)
            what = f"{_POWER_WORDS[args.action]} in {args.in_minutes} minutes"
        else:
            done = await self._commands.run(argv)
            what = _POWER_WORDS[args.action]
        if not done.ok:
            return ToolResult.fail(f"could not {args.action.replace('_', ' ')}: {done.why()}")
        return ToolResult.ok(
            f"{what}: done", data={"action": args.action, "in_minutes": args.in_minutes}
        )


# --- settings.default_app ---------------------------------------------------------------


class DefaultArgs(ToolArgs):
    what: str = Field(
        description='"browser", "email", or a file type: "pdf", "image", "video", "music", '
        '"text", or a MIME type (application/pdf)'
    )
    app: str = Field(default="", description="The app to use; empty to say which is used now")


_MIME: Final = {
    "browser": ["x-scheme-handler/http", "x-scheme-handler/https", "text/html"],
    "email": ["x-scheme-handler/mailto"],
    "pdf": ["application/pdf"],
    "image": ["image/jpeg", "image/png", "image/webp", "image/gif"],
    "video": ["video/mp4", "video/x-matroska", "video/webm"],
    "music": ["audio/mpeg", "audio/flac", "audio/ogg", "audio/x-wav"],
    "text": ["text/plain"],
}


class SettingsDefaultApp(_Settings, Tool[DefaultArgs]):
    name = "settings.default_app"
    description = "Which app opens a kind of file or link, and change it."
    usage = Usage(
        when='"Make Chrome my default browser", "open PDFs with Okular", "what opens my videos?".',
        example='settings.default_app(what="browser", app="Google Chrome")',
    )
    args_model = DefaultArgs
    timeout_s = 15

    def tier_for(self, args: DefaultArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.app else Tier.AUTO

    def sentence(self, args: DefaultArgs) -> str:
        return f"Open every {args.what} with {args.app} from now on?"

    async def run(self, args: DefaultArgs, ctx: ToolContext) -> ToolResult:
        what = args.what.lower().strip()
        types = _MIME.get(what) or ([what] if "/" in what else None)
        if types is None:
            return ToolResult.fail(
                f"say browser, email, {', '.join(list(_MIME)[2:])} or a MIME type"
            )
        run = self._commands.run
        if args.app:
            app = best_match(installed_apps(application_dirs()), args.app)
            if app is None:
                return ToolResult.fail(f"no installed app matches “{args.app}”")
            for mime in types:
                done = await run(["xdg-mime", "default", app.id, mime])
                if not done.ok:
                    return ToolResult.fail(f"could not set it: {done.why()}")
            if what == "browser":
                await run(["xdg-settings", "set", "default-web-browser", app.id])
        current = (await run(["xdg-mime", "query", "default", types[0]])).out.strip()
        apps = {a.id: a.name for a in installed_apps(application_dirs())}
        name = apps.get(current, current or "nothing")
        return ToolResult.ok(f"{what}: {name}", data={"what": what, "app": name, "id": current})


# --- updates.check ----------------------------------------------------------------------


class UpdatesArgs(ToolArgs):
    open_software: bool = Field(
        default=False, description="Also open Software on its Updates page to install them"
    )


def upgradable(text: str) -> list[str]:
    """apt's "code/stable 1.145.2 amd64 [upgradable from: 1.145.0]" lines as names."""
    return [line.split("/", 1)[0] for line in text.splitlines() if "[upgradable from" in line]


class UpdatesCheck(_Settings, Tool[UpdatesArgs]):
    name = "updates.check"
    description = "Which software updates are waiting (apt and Flatpak), without installing."
    usage = Usage(
        when='"Are there updates?", "update my system" (open_software=true: the user '
        "installs them in Software, with their password).",
        avoid="apt upgrade or sudo in shell.run: Arcee never uses sudo.",
        example="updates.check(open_software=true)",
    )
    args_model = UpdatesArgs
    read_only: ClassVar[bool] = True
    timeout_s = 60

    async def run(self, args: UpdatesArgs, ctx: ToolContext) -> ToolResult:
        run = self._commands.run
        apt = await run(["apt", "list", "--upgradable"], limit_s=45)
        packages = upgradable(apt.out)
        flatpaks: list[str] = []
        if self._commands.has("flatpak"):
            fp = await run(["flatpak", "remote-ls", "--updates", "--columns=name"], limit_s=45)
            flatpaks = [line.strip() for line in fp.out.splitlines() if line.strip()]
        data: dict[str, Any] = {
            "apt": len(packages),
            "apt_packages": packages[:40],
            "flatpak": len(flatpaks),
            "flatpak_apps": flatpaks[:20],
        }
        if args.open_software:
            await run(["systemd-run", "--user", "--collect", "gnome-software", "--mode=updates"])
            data["opened"] = "Software, Updates page"
        total = len(packages) + len(flatpaks)
        return ToolResult.ok(f"{total} updates waiting" if total else "Up to date", data=data)


def settings_tools(commands: Commands | None = None) -> list[Tool[Any]]:
    c = commands or SystemCommands()
    return [
        SettingsVolume(c),
        SettingsAudioOutput(c),
        SettingsBrightness(),
        SettingsAppearance(c),
        SettingsDnd(c),
        SettingsRadio(c),
        BluetoothDevices(c),
        SettingsAccessibility(c),
        SessionPower(c),
        SettingsDefaultApp(c),
        UpdatesCheck(c),
    ]
