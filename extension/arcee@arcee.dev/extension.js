// Arcee shell bridge (spec sections 9 and 10). On Wayland only the compositor can
// raise a window, see the whole screen or synthesise input, so this extension
// exposes org.arcee.Shell on the session bus for Arcee: list and focus windows,
// take a screenshot, click, scroll and type (decision 0009). Screen and input work
// only inside a visible control session (control.js, decision 0010). Every input
// call is whole (press and release together), so nothing stays held between calls;
// the stop shortcut (Super+Shift+Escape) and the Stop pill release everything and
// tell Arcee to stop.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {ControlSession} from './control.js';
import {ArceeIndicator} from './panel.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');

const BUS_NAME = 'org.arcee.Shell';
const OBJECT_PATH = '/org/arcee/Shell';
const INTERFACE = `
<node>
  <interface name="org.arcee.Shell">
    <method name="ListWindows">
      <arg type="s" direction="out" name="windows_json"/>
    </method>
    <method name="FocusWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="focused"/>
    </method>
    <method name="FocusTitle">
      <arg type="s" direction="in" name="title"/>
      <arg type="b" direction="out" name="focused"/>
    </method>
    <method name="Screenshot">
      <arg type="s" direction="out" name="shot_json"/>
    </method>
    <method name="PointerClick">
      <arg type="d" direction="in" name="x"/>
      <arg type="d" direction="in" name="y"/>
      <arg type="u" direction="in" name="button"/>
      <arg type="u" direction="in" name="count"/>
    </method>
    <method name="Scroll">
      <arg type="d" direction="in" name="x"/>
      <arg type="d" direction="in" name="y"/>
      <arg type="i" direction="in" name="steps"/>
    </method>
    <method name="KeyCombo">
      <arg type="au" direction="in" name="keyvals"/>
    </method>
    <method name="TypeText">
      <arg type="s" direction="in" name="text"/>
    </method>
    <method name="ReleaseAll"/>
    <method name="BeginControl">
      <arg type="s" direction="in" name="reason"/>
      <arg type="s" direction="out" name="state"/>
    </method>
    <method name="EndControl"/>
    <method name="ControlState">
      <arg type="s" direction="out" name="state"/>
    </method>
    <signal name="Stopped"/>
    <signal name="Paused"/>
    <signal name="Resumed"/>
  </interface>
</node>`;

// Shortest gap between synthetic events, so apps see a press before its release.
const EVENT_GAP_US = 8000;
const MOVE_ATTEMPTS = 3;
const OVERVIEW_CLOSE_MS = 500;
const SCREENSHOT_HIDE_MS = 50;

function windows() {
    return global.get_window_actors()
        .map(actor => actor.meta_window)
        .filter(window => window && !window.is_skip_taskbar());
}

function activate(window) {
    // From the overview, a focused window would sit behind it: leave it first.
    if (Main.overview.visible)
        Main.overview.hide();
    window.activate(global.get_current_time());
}

function sleep(ms) {
    return new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
        resolve();
        return GLib.SOURCE_REMOVE;
    }));
}

class ShellBridge {
    constructor(emit) {
        this._emit = emit;
        this._control = new ControlSession({
            onStop: () => emit('Stopped'),
            onPause: () => emit('Paused'),
            onResume: () => emit('Resumed'),
        });
        // Made now, not on first use: a device made just before its first event is not
        // yet on the seat, and those events are lost (typed text vanished in tests).
        const seat = Clutter.get_default_backend().get_default_seat();
        this._pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._time = 0;
        // What is pressed right now, so a stop can release it.
        this._buttons = new Set();
        this._keys = new Set();
    }

    destroy() {
        this.ReleaseAll();
        this._control.end();
        this._pointer = null;
        this._keyboard = null;
    }

    ListWindows() {
        return JSON.stringify(windows().map(window => ({
            id: window.get_id(),
            title: window.get_title() ?? '',
            app_id: window.get_gtk_application_id() ?? window.get_wm_class() ?? '',
            focused: window.has_focus(),
        })));
    }

    FocusWindow(id) {
        const window = windows().find(w => w.get_id() === id);
        if (!window)
            return false;
        activate(window);
        return true;
    }

    FocusTitle(title) {
        // Exact title first, then the first window whose title contains it.
        const all = windows();
        const window = all.find(w => w.get_title() === title) ??
            all.find(w => (w.get_title() ?? '').includes(title));
        if (!window)
            return false;
        activate(window);
        return true;
    }

    BeginControlAsync([reason], invocation) {
        try {
            this._control.begin(invocation.get_sender(), reason);
            invocation.return_value(new GLib.Variant('(s)', [this._control.state]));
        } catch (error) {
            this._fail(invocation, error.message);
        }
    }

    EndControlAsync(params, invocation) {
        if (this._control.owner === invocation.get_sender())
            this._control.end();
        invocation.return_value(null);
    }

    ControlState() {
        return this._control.state;
    }

    async ScreenshotAsync(params, invocation) {
        if (!this._allowed(invocation))
            return;
        try {
            // The runtime directory is the user's own (0700); the file is 0600.
            const dir = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'arcee']);
            GLib.mkdir_with_parents(dir, 0o700);
            const path = GLib.build_filenamev([dir, 'screen.png']);
            const stream = Gio.File.new_for_path(path).replace(
                null, false, Gio.FileCreateFlags.PRIVATE | Gio.FileCreateFlags.REPLACE_DESTINATION,
                null);
            this._control.setShown(false);
            try {
                await sleep(SCREENSHOT_HIDE_MS); // let the stage redraw without them
                await new Shell.Screenshot().screenshot(false, stream);
            } finally {
                this._control.setShown(true);
            }
            stream.close(null);
            const shot = {
                path, width: global.stage.width, height: global.stage.height,
                // OCR reads the top bar apart from the rest of the screen.
                top_bar: Main.panel.visible ? Main.layoutManager.primaryMonitor.y + Main.panel.height : 0,
            };
            invocation.return_value(new GLib.Variant('(s)', [JSON.stringify(shot)]));
        } catch (error) {
            invocation.return_error_literal(
                Gio.IOErrorEnum, Gio.IOErrorEnum.FAILED, `screenshot failed: ${error.message}`);
        }
    }

    async PointerClickAsync([x, y, button, count], invocation) {
        if (!this._allowed(invocation))
            return;
        const pointer = this._pointer;
        if (!await this._moveTo(x, y)) {
            this._fail(invocation, `the pointer could not reach (${x}, ${y})`);
            return;
        }
        for (let i = 0; i < count; i++) {
            this._buttons.add(button);
            pointer.notify_button(this._now(), button, Clutter.ButtonState.PRESSED);
            pointer.notify_button(this._now(), button, Clutter.ButtonState.RELEASED);
            this._buttons.delete(button);
        }
        invocation.return_value(null);
    }

    async ScrollAsync([x, y, steps], invocation) {
        if (!this._allowed(invocation))
            return;
        const pointer = this._pointer;
        if (!await this._moveTo(x, y)) {
            this._fail(invocation, `the pointer could not reach (${x}, ${y})`);
            return;
        }
        const direction = steps < 0 ? Clutter.ScrollDirection.UP : Clutter.ScrollDirection.DOWN;
        for (let i = 0; i < Math.abs(steps); i++)
            pointer.notify_discrete_scroll(this._now(), direction, Clutter.ScrollSource.WHEEL);
        invocation.return_value(null);
    }

    KeyComboAsync([keyvals], invocation) {
        if (!this._allowed(invocation))
            return;
        // Press in order (modifiers first), release in reverse.
        const keyboard = this._keyboard;
        for (const keyval of keyvals) {
            this._keys.add(keyval);
            keyboard.notify_keyval(this._now(), keyval, Clutter.KeyState.PRESSED);
        }
        for (const keyval of [...keyvals].reverse()) {
            keyboard.notify_keyval(this._now(), keyval, Clutter.KeyState.RELEASED);
            this._keys.delete(keyval);
        }
        invocation.return_value(null);
    }

    TypeTextAsync([text], invocation) {
        if (!this._allowed(invocation))
            return;
        for (const character of text) {
            const keyval = Clutter.unicode_to_keysym(character.codePointAt(0));
            this._keyboard.notify_keyval(this._now(), keyval, Clutter.KeyState.PRESSED);
            this._keyboard.notify_keyval(this._now(), keyval, Clutter.KeyState.RELEASED);
        }
        invocation.return_value(null);
    }

    ReleaseAll() {
        for (const button of this._buttons)
            this._pointer?.notify_button(this._now(), button, Clutter.ButtonState.RELEASED);
        for (const keyval of this._keys)
            this._keyboard?.notify_keyval(this._now(), keyval, Clutter.KeyState.RELEASED);
        this._buttons.clear();
        this._keys.clear();
    }

    stop() {
        // The shortcut stops a task whether or not it holds the screen.
        this.ReleaseAll();
        if (this._control.state !== 'off')
            this._control.stop();
        else
            this._emit('Stopped');
    }

    // Whether the caller holds an active control session; answers the call if not.
    _allowed(invocation) {
        try {
            this._control.check(invocation.get_sender());
            return true;
        } catch (error) {
            this._fail(invocation, error.message);
            return false;
        }
    }

    async _moveTo(x, y) {
        // A move from the top-left corner presses on the hot corner's barriers: it can
        // lose one axis ((706, 500) landed at (0, 500)) and open the overview. So move,
        // look where the pointer is and try again, and close an overview it opened.
        const overview = Main.overview.visible;
        for (let attempt = 0; attempt < MOVE_ATTEMPTS; attempt++) {
            this._pointer.notify_absolute_motion(this._now(), x, y);
            await sleep(30); // the input thread applies it; the window sees it arrive
            const [px, py] = global.get_pointer();
            if (Math.abs(px - x) > 1 || Math.abs(py - y) > 1)
                continue;
            if (!overview && Main.overview.visible) {
                Main.overview.hide();
                await sleep(OVERVIEW_CLOSE_MS);
            }
            return true;
        }
        return false;
    }

    _fail(invocation, message) {
        invocation.return_error_literal(Gio.IOErrorEnum, Gio.IOErrorEnum.FAILED, message);
    }

    _now() {
        // Strictly increasing, so a press and its release never share a timestamp.
        this._time = Math.max(GLib.get_monotonic_time(), this._time + EVENT_GAP_US);
        return this._time;
    }
}

export default class ArceeExtension extends Extension {
    enable() {
        this._bridge = new ShellBridge(signal => this._dbus?.emit_signal(signal, null));
        this._dbus = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this._bridge);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null, null);
        Main.wm.addKeybinding(
            'stop-agent', this.getSettings(), Meta.KeyBindingFlags.NONE, Shell.ActionMode.ALL,
            () => this._bridge.stop());
        this._indicator = new ArceeIndicator(this.path);
        Main.panel.addToStatusArea('arcee', this._indicator);
    }

    disable() {
        this._indicator.destroy();
        this._indicator = null;
        Main.wm.removeKeybinding('stop-agent');
        Gio.bus_unown_name(this._owner);
        this._dbus.unexport();
        this._dbus = null;
        this._bridge.destroy();
        this._bridge = null;
    }
}
