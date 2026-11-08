"""The desktop's settings tools, against a fake command runner (no real desktop)."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import SETTINGS, filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.commands import Done
from arcee.tools.desktop_settings import (
    AccessibilityArgs,
    AppearanceArgs,
    BluetoothArgs,
    BluetoothDevices,
    BrightnessArgs,
    DefaultArgs,
    DndArgs,
    OutputArgs,
    PowerArgs,
    RadioArgs,
    SessionPower,
    SettingsAccessibility,
    SettingsAppearance,
    SettingsAudioOutput,
    SettingsBrightness,
    SettingsDefaultApp,
    SettingsDnd,
    SettingsRadio,
    SettingsVolume,
    UpdatesArgs,
    UpdatesCheck,
    VolumeArgs,
    audio_devices,
    parse_volume,
    upgradable,
)


class FakeCommands:
    """Answers each command by the longest matching prefix in `replies`; records them."""

    def __init__(self, replies: dict[str, str | Done] | None = None) -> None:
        self.replies = replies or {}
        self.ran: list[str] = []
        self.settings: dict[str, str] = {}

    async def run(
        self,
        argv: Sequence[str],
        *,
        limit_s: float = 15,
        stdin: bytes | None = None,
        quiet: bool = False,
    ) -> Done:
        line = " ".join(argv)
        self.ran.append(line)
        if argv[:2] == ["gsettings", "set"]:
            self.settings[f"{argv[2]} {argv[3]}"] = argv[4]
            return Done(0, "", "")
        if argv[:2] == ["gsettings", "get"]:
            key = f"{argv[2]} {argv[3]}"
            if key in self.settings:
                return Done(0, self.settings[key] + "\n", "")
        best = max((k for k in self.replies if line.startswith(k)), key=len, default=None)
        if best is None:
            return Done(0, "", "")
        reply = self.replies[best]
        return reply if isinstance(reply, Done) else Done(0, reply, "")

    def has(self, program: str) -> bool:
        return program != "flatpak"


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t", home=tmp_path)


async def test_volume_is_set_then_read_back(tmp_path: Path) -> None:
    c = FakeCommands({"wpctl get-volume": "Volume: 0.30\n"})
    result = await SettingsVolume(c).run(VolumeArgs(action="set", percent=30), ctx(tmp_path))
    assert result.data == {"volume_percent": 30, "muted": False}
    assert c.ran[0] == "wpctl set-volume -l 1.5 @DEFAULT_AUDIO_SINK@ 30%"


async def test_louder_goes_up_ten_points_and_mute_says_so(tmp_path: Path) -> None:
    c = FakeCommands({"wpctl get-volume": "Volume: 0.88 [MUTED]\n"})
    tool = SettingsVolume(c)
    await tool.run(VolumeArgs(action="up"), ctx(tmp_path))
    result = await tool.run(VolumeArgs(action="mute"), ctx(tmp_path))
    assert "wpctl set-volume -l 1.5 @DEFAULT_AUDIO_SINK@ 10%+" in c.ran
    assert "wpctl set-mute @DEFAULT_AUDIO_SINK@ 1" in c.ran
    assert result.summary == "Volume 88% (muted)"
    assert parse_volume("Volume: 1.25") == (125, False)


PW_DUMP = json.dumps(
    [
        {
            "id": 51,
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "node.name": "alsa_output.analog-stereo",
                    "node.description": "Built-in Audio Analog Stereo",
                }
            },
        },
        {
            "id": 44,
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "node.name": "alsa_output.hdmi-stereo",
                    "node.description": "HDA NVidia Digital Stereo (HDMI)",
                }
            },
        },
        {
            "id": 0,
            "type": "PipeWire:Interface:Metadata",
            "props": {"metadata.name": "default"},
            "metadata": [
                {"key": "default.audio.sink", "value": {"name": "alsa_output.hdmi-stereo"}}
            ],
        },
    ]
)


async def test_outputs_are_listed_with_the_one_in_use(tmp_path: Path) -> None:
    c = FakeCommands({"pw-dump": PW_DUMP})
    result = await SettingsAudioOutput(c).run(OutputArgs(), ctx(tmp_path))
    assert result.data == {
        "devices": ["Built-in Audio Analog Stereo", "HDA NVidia Digital Stereo (HDMI) (in use)"]
    }
    devices, default = audio_devices(PW_DUMP, "output")
    assert (len(devices), default) == (2, "alsa_output.hdmi-stereo")


async def test_an_output_is_chosen_by_part_of_its_name(tmp_path: Path) -> None:
    c = FakeCommands({"pw-dump": PW_DUMP})
    result = await SettingsAudioOutput(c).run(OutputArgs(device="built-in"), ctx(tmp_path))
    assert result.summary == "Sound output is now Built-in Audio Analog Stereo"
    assert c.ran[-1] == "wpctl set-default 51"
    missing = await SettingsAudioOutput(c).run(OutputArgs(device="tv"), ctx(tmp_path))
    assert missing.error is not None
    assert "no output matches “tv”" in missing.error


def backlight(tmp_path: Path, now: int, top: int) -> Path:
    device = tmp_path / "backlight" / "intel_backlight"
    device.mkdir(parents=True)
    (device / "type").write_text("raw\n")
    (device / "max_brightness").write_text(f"{top}\n")
    (device / "brightness").write_text(f"{now}\n")
    return tmp_path / "backlight"


async def test_brightness_is_set_through_logind_and_never_to_black(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    async def setter(device: str, value: int) -> None:
        calls.append((device, value))

    tool = SettingsBrightness(backlight(tmp_path, 960, 1920), setter)
    got = await tool.run(BrightnessArgs(action="get"), ctx(tmp_path))
    dimmer = await tool.run(BrightnessArgs(action="down", percent=60), ctx(tmp_path))
    assert got.data == {"brightness_percent": 50}
    assert dimmer.data == {"brightness_percent": 1}
    assert calls == [("intel_backlight", 19)]


async def test_no_backlight_says_it_cannot(tmp_path: Path) -> None:
    result = await SettingsBrightness(tmp_path / "none").run(
        BrightnessArgs(action="get"), ctx(tmp_path)
    )
    assert result.error == "this screen's brightness cannot be set from here"


async def test_dark_mode_and_night_light(tmp_path: Path) -> None:
    c = FakeCommands()
    result = await SettingsAppearance(c).run(
        AppearanceArgs(dark_mode=True, night_light=True), ctx(tmp_path)
    )
    assert c.settings["org.gnome.desktop.interface color-scheme"] == "'prefer-dark'"
    assert result.summary == "Dark mode, Night Light on"


async def test_a_wallpaper_must_be_a_file(tmp_path: Path) -> None:
    c = FakeCommands()
    tool = SettingsAppearance(c)
    missing = await tool.run(AppearanceArgs(wallpaper="nope.jpg"), ctx(tmp_path))
    (tmp_path / "sea.jpg").write_bytes(b"jpg")
    await tool.run(AppearanceArgs(wallpaper="sea.jpg"), ctx(tmp_path))
    assert missing.error == "nope.jpg is not a file"
    uri = f"'{(tmp_path / 'sea.jpg').as_uri()}'"
    assert c.settings["org.gnome.desktop.background picture-uri-dark"] == uri


async def test_do_not_disturb_for_an_hour_turns_itself_off(tmp_path: Path) -> None:
    c = FakeCommands()
    result = await SettingsDnd(c).run(DndArgs(on=True, minutes=60), ctx(tmp_path))
    assert c.settings["org.gnome.desktop.notifications show-banners"] == "false"
    timer = next(line for line in c.ran if line.startswith("systemd-run"))
    assert "--unit=arcee-dnd-end --on-active=3600s" in timer
    assert timer.endswith("gsettings set org.gnome.desktop.notifications show-banners true")
    assert result.summary == "Do Not Disturb is on for 60 minutes"


async def test_wifi_off_asks_first_and_bluetooth_does_not(tmp_path: Path) -> None:
    c = FakeCommands({"nmcli radio wifi": "enabled\n", "bluetoothctl show": "Powered: yes\n"})
    tool = SettingsRadio(c)
    assert tool.tier_for(RadioArgs(radio="wifi", on=False), Policy()) is Tier.CONFIRM
    assert tool.tier_for(RadioArgs(radio="bluetooth", on=False), Policy()) is Tier.AUTO
    result = await tool.run(RadioArgs(radio="bluetooth", on=True), ctx(tmp_path))
    assert "bluetoothctl power on" in c.ran
    assert result.summary == "Bluetooth is on"


PAIRED = "Device A8:E6:E8:B8:05:3E JBL Tune 770NC\nDevice 08:12:87:21:D3:29 Nord Buds\n"


async def test_a_paired_device_is_connected_by_name(tmp_path: Path) -> None:
    c = FakeCommands(
        {
            "bluetoothctl devices Paired": PAIRED,
            "bluetoothctl devices Connected": "",
            "bluetoothctl show": "Powered: no\n",
            "bluetoothctl connect": "Connection successful\n",
        }
    )
    result = await BluetoothDevices(c).run(
        BluetoothArgs(action="connect", device="jbl"), ctx(tmp_path)
    )
    assert "bluetoothctl power on" in c.ran  # it was off
    assert c.ran[-1] == "bluetoothctl connect A8:E6:E8:B8:05:3E"
    assert result.summary == "Connected to JBL Tune 770NC"


async def test_paired_devices_are_listed_with_the_connected_ones(tmp_path: Path) -> None:
    c = FakeCommands(
        {
            "bluetoothctl devices Paired": PAIRED,
            "bluetoothctl devices Connected": "Device 08:12:87:21:D3:29 Nord Buds\n",
        }
    )
    result = await BluetoothDevices(c).run(BluetoothArgs(), ctx(tmp_path))
    assert result.data == {"devices": ["JBL Tune 770NC", "Nord Buds (connected)"]}


async def test_bigger_text(tmp_path: Path) -> None:
    c = FakeCommands()
    result = await SettingsAccessibility(c).run(AccessibilityArgs(text_size=1.25), ctx(tmp_path))
    assert c.settings["org.gnome.desktop.interface text-scaling-factor"] == "1.25"
    assert result.data is not None
    assert result.data["text_size"] == 1.25


async def test_power_off_later_asks_and_is_a_timer(tmp_path: Path) -> None:
    c = FakeCommands()
    tool = SessionPower(c)
    args = PowerArgs(action="power_off", in_minutes=10)
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    assert tool.sentence(args) == (
        "Shut down the computer in 10 minutes? Unsaved work in open apps is lost."
    )
    assert tool.tier_for(PowerArgs(action="lock"), Policy()) is Tier.AUTO
    await tool.run(args, ctx(tmp_path))
    timer = next(line for line in c.ran if line.startswith("systemd-run"))
    assert "--unit=arcee-power --on-active=600s" in timer
    assert timer.endswith("systemctl poweroff")


async def test_cancelling_says_whether_anything_was_waiting(tmp_path: Path) -> None:
    c = FakeCommands({"systemctl --user stop": Done(5, "", "not loaded")})
    result = await SessionPower(c).run(PowerArgs(action="cancel"), ctx(tmp_path))
    assert result.summary == "Nothing was waiting to happen"


async def test_default_app_names_what_is_used(tmp_path: Path, monkeypatch: Any) -> None:
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "google-chrome.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Google Chrome\n"
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "none"))
    c = FakeCommands({"xdg-mime query default": "google-chrome.desktop\n"})
    tool = SettingsDefaultApp(c)
    args = DefaultArgs(what="browser", app="chrome")
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    result = await tool.run(args, ctx(tmp_path))
    assert "xdg-mime default google-chrome.desktop x-scheme-handler/https" in c.ran
    assert "xdg-settings set default-web-browser google-chrome.desktop" in c.ran
    assert result.summary == "browser: Google Chrome"


async def test_updates_are_counted_without_installing(tmp_path: Path) -> None:
    apt = (
        "Listing...\n"
        "base-files/resolute-updates 14ubuntu6.3 amd64 [upgradable from: 14ubuntu6.2]\n"
        "code/stable 1.145.2 amd64 [upgradable from: 1.145.0]\n"
    )
    c = FakeCommands({"apt list --upgradable": apt})
    result = await UpdatesCheck(c).run(UpdatesArgs(), ctx(tmp_path))
    assert result.summary == "2 updates waiting"
    assert upgradable(apt) == ["base-files", "code"]
    assert not any("sudo" in line or "upgrade " in line for line in c.ran)


@pytest.mark.parametrize(
    "request_",
    [
        "Turn the volume up",
        "Set volume to 30%",
        "Dim the screen",
        "Switch to dark mode",
        "Turn on do not disturb for an hour",
        "Turn off Wi-Fi",
        "Connect my headphones",
        "Make the text bigger",
        "Lock my computer",
        "Shut down in 10 minutes",
        "Make Chrome my default browser",
        "Are there updates?",
        "Play sound through my headphones",
    ],
)
def test_settings_requests_offer_the_settings_tools(request_: str) -> None:
    assert set(SETTINGS) <= set(filters_for([Message("user", request_)]))
