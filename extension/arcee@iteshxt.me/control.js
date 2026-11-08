// While Arcee drives the screen it holds a control session: the screen edges glow,
// Arcee's pill at the top centre (cue.js) says what it is doing and offers Stop, and
// the screen and input methods work only inside it, so nothing reads or drives the
// desktop unseen (decision 0010). The glow is Arcee's sunrise orange. The user's own
// mouse or keyboard pauses the session at once: their input always goes through, the
// glow turns blue (the user's colour), and the pill offers Resume or Stop. Esc stops.
//
// GNOME gives an extension's event filter only input meant for the Shell's own UI, not
// for app windows, so over Chrome or Files the filter never saw the user (found with
// Esc, 2026-11-04). So the session also watches what GNOME knows whoever the input is
// for: where the pointer is, and how long since any input (its idle monitor). Input
// Arcee made is told apart by time: the bridge notes each event it injects (`injected`).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// A session the owner has not used for this long ends; Arcee begins a new one when
// it next needs the screen (a long confirmation, a crashed process).
const IDLE_LIMIT_S = 120;
// Pointer travel that counts as the user taking the mouse, not a nudge of the desk.
const MOVE_THRESHOLD_PX = 40;
// How often the session looks at the pointer and the idle time.
const WATCH_MS = 100;
// Input this soon after one of Arcee's own events counts as Arcee's.
const OWN_INPUT_MS = 250;
// Small moves stop adding up after this long with the pointer still.
const NUDGE_FORGET_MS = 1000;
const GLOW_PX = 28;
const USER_EVENTS = new Set([
    Clutter.EventType.BUTTON_PRESS,
    Clutter.EventType.KEY_PRESS,
    Clutter.EventType.SCROLL,
    Clutter.EventType.TOUCH_BEGIN,
]);

// GNOME's idle monitor for all input devices (null where this Shell has none).
function idleMonitor() {
    try {
        return global.backend.get_core_idle_monitor();
    } catch {
        return Meta.IdleMonitor.get_core?.() ?? null;
    }
}

export class ControlSession {
    constructor({onStop, onPause, onResume}) {
        this._onStop = onStop;
        this._onPause = onPause;
        this._onResume = onResume;
        this.state = 'off';
        this.owner = null;
        this._glows = [];
        // Told each change of state ('active', 'paused', 'off'): the pill shows it.
        this.onState = _state => {};
        // Whether `actor` is the pill's: pressing its buttons is not taking control.
        this.isOwnActor = _actor => false;
    }

    begin(owner, reason) {
        if (this.state !== 'off' && this.owner !== owner)
            throw new Error('another program holds control of the screen');
        this.touch();
        if (this.state !== 'off')
            return;
        this.owner = owner;
        this.state = 'active';
        this._showGlow();
        this._restyle();
        this._filter = Clutter.Event.add_filter(global.stage, (event, actor) =>
            this._onEvent(event, actor));
        this._startWatching();
        // The owner exiting, even by a crash, ends its session.
        this._watch = Gio.bus_watch_name(
            Gio.BusType.SESSION, owner, Gio.BusNameWatcherFlags.NONE, null, () => this.end());
        this._idle = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
            if (this.state === 'active' && GLib.get_monotonic_time() - this._used > IDLE_LIMIT_S * 1e6)
                this.end();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // Raises unless `owner` may act now.
    check(owner) {
        if (this.state === 'off' || this.owner !== owner)
            throw new Error('Arcee has not taken control of the screen (BeginControl)');
        if (this.state === 'paused')
            throw new Error('paused: the user took control');
        this.touch();
    }

    touch() {
        this._used = GLib.get_monotonic_time();
    }

    pause() {
        if (this.state !== 'active')
            return;
        this.state = 'paused';
        this._restyle();
        this._onPause();
    }

    resume() {
        if (this.state !== 'paused')
            return;
        this.state = 'active';
        this._moved = 0;
        this._sinceNow();  // the click on Resume is not the user taking control again
        this.touch();
        this._restyle();
        this._onResume();
    }

    stop() {
        const wasOn = this.state !== 'off';
        this.end();
        if (wasOn)
            this._onStop();
    }

    end() {
        if (this.state === 'off')
            return;
        this.state = 'off';
        this.owner = null;
        Clutter.Event.remove_filter(this._filter);
        Gio.bus_unwatch_name(this._watch);
        GLib.source_remove(this._idle);
        GLib.source_remove(this._watcher);
        this._filter = this._watch = this._idle = this._watcher = null;
        for (const glow of this._glows)
            glow.destroy();
        this._glows = [];
        this.onState('off');
    }

    // Hidden for the moment of a screenshot: the glow over the screen edges spoiled
    // OCR of the top bar.
    setShown(shown) {
        for (const glow of this._glows)
            glow.visible = shown;
    }

    // Arcee injected an event just now (the bridge, for each one).
    injected() {
        this._injectedAt = GLib.get_monotonic_time();
    }

    _startWatching() {
        this._monitor = idleMonitor();
        this._sinceNow();
        this._watcher = GLib.timeout_add(GLib.PRIORITY_DEFAULT, WATCH_MS, () => {
            this._watchInput();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // From now: input before this moment (typing the request, a click on Resume) is not
    // the user taking control.
    _sinceNow() {
        this._injectedAt = GLib.get_monotonic_time();
        const [x, y] = global.get_pointer();
        this._pointerAt = [x, y];
        this._moved = 0;
        this._motionAt = 0;
    }

    _watchInput() {
        const [x, y] = global.get_pointer();
        const step = Math.hypot(x - this._pointerAt[0], y - this._pointerAt[1]);
        this._pointerAt = [x, y];
        if (this.state !== 'active')
            return;
        const now = GLib.get_monotonic_time();
        const ownAgoMs = (now - this._injectedAt) / 1000;
        if (ownAgoMs < OWN_INPUT_MS) {  // Arcee is acting: the pointer is going where it sent it
            this._moved = 0;
            return;
        }
        if (step > 0) {  // the user's mouse: a nudge of the desk is not taking control
            this._motionAt = now;
            this._moved += step;
            if (this._moved > MOVE_THRESHOLD_PX)
                this.pause();
            return;
        }
        if (now - this._motionAt > NUDGE_FORGET_MS * 1000)
            this._moved = 0;
        // Input since Arcee's last event, the pointer still: a key, a click, a scroll. Not
        // the movement just seen, which reset the idle time too (a nudge paused).
        const idleMs = this._monitor ? Number(this._monitor.get_idletime()) : Infinity;
        const inputAt = now - idleMs * 1000;
        if (idleMs < 2 * WATCH_MS && idleMs + WATCH_MS < ownAgoMs && inputAt > this._motionAt + 20_000)
            this.pause();
    }

    _onEvent(event, actor) {
        if (this.state !== 'active')
            return Clutter.EVENT_PROPAGATE;
        // Arcee's own events come from virtual devices, which have no device node.
        const device = event.get_source_device();
        if (!device || device.get_device_node() === null)
            return Clutter.EVENT_PROPAGATE;
        if (actor && this.isOwnActor(actor))
            return Clutter.EVENT_PROPAGATE;
        if (event.type() === Clutter.EventType.KEY_PRESS && event.get_key_symbol() === Clutter.KEY_Escape) {
            this.stop(); // Esc stops Arcee, and goes no further
            return Clutter.EVENT_STOP;
        }
        if (USER_EVENTS.has(event.type())) {
            this.pause();
        } else if (event.type() === Clutter.EventType.MOTION) {
            const [x, y] = event.get_coords();
            if (this._last)
                this._moved = (this._moved ?? 0) + Math.hypot(x - this._last[0], y - this._last[1]);
            this._last = [x, y];
            if (this._moved > MOVE_THRESHOLD_PX)
                this.pause();
        }
        // The user's input always reaches their apps.
        return Clutter.EVENT_PROPAGATE;
    }

    _showGlow() {
        // Four gradient strips per monitor, fading inwards (St draws no inset shadow).
        const g = GLOW_PX;
        for (const m of Main.layoutManager.monitors) {
            for (const [edge, x, y, width, height] of [
                ['top', m.x, m.y, m.width, g],
                ['bottom', m.x, m.y + m.height - g, m.width, g],
                ['left', m.x, m.y, g, m.height],
                ['right', m.x + m.width - g, m.y, g, m.height],
            ]) {
                const glow = new St.Widget({
                    style_class: `arcee-glow arcee-glow-${edge}`, reactive: false, x, y, width, height,
                });
                Main.layoutManager.uiGroup.add_child(glow);
                this._glows.push(glow);
            }
        }
    }

    _restyle() {
        const paused = this.state === 'paused';
        for (const glow of this._glows) {
            if (paused)
                glow.add_style_class_name('paused');
            else
                glow.remove_style_class_name('paused');
        }
        this.onState(this.state);
    }
}
