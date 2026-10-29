// Arcee shell bridge (spec section 10). On Wayland only the compositor can raise a
// window, so this extension exposes org.arcee.Shell on the session bus for the
// Arcee daemon: list windows, focus one. It grows the indicator, confirmation
// bubble and kill switch in phase 4.

import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

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
  </interface>
</node>`;

function windows() {
    return global.get_window_actors()
        .map(actor => actor.meta_window)
        .filter(window => window && !window.is_skip_taskbar());
}

class ShellBridge {
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
        window.activate(global.get_current_time());
        return true;
    }

    FocusTitle(title) {
        // Exact title first, then the first window whose title contains it.
        const all = windows();
        const window = all.find(w => w.get_title() === title) ??
            all.find(w => (w.get_title() ?? '').includes(title));
        if (!window)
            return false;
        window.activate(global.get_current_time());
        return true;
    }
}

export default class ArceeExtension extends Extension {
    enable() {
        this._bridge = new ShellBridge();
        this._dbus = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this._bridge);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        Gio.bus_unown_name(this._owner);
        this._dbus.unexport();
        this._dbus = null;
        this._bridge = null;
    }
}
