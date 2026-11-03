// Arcee's pill at the top centre: when Arcee hears "hey Arcee" (or the mic is pressed),
// its ring appears, pulsing with the voice. Beside it, what the user said and what
// Arcee is doing about it, step by step, then its reply for a moment: a spoken request
// can be followed without opening the panel. While a request runs it has a Stop button;
// while Arcee holds the screen (control.js) it shows too, typed requests included, and
// offers Resume once the user takes over. Esc, while it shows, stops everything. The ring is the app's
// active loop, as frames (icons/cue/NN.png, from tools/make_art.py): GNOME 50's shell
// has no sprite-sheet animation, so the frames are shown one after another, and only
// while the cue is on screen.
//
// Only its buttons take clicks, and only the user's (not Arcee's own virtual pointer):
// Arcee's clicks near the top of the screen go through to the window under it. It
// hides for Arcee's screenshots (`setShown`).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Graphene from 'gi://Graphene';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// Small, at the user's word: noticeable, out of the way. The ring is about the size of
// the top bar's own icons, and the cue a slim pill just below the bar.
const SIZE = 30;
// The ring's own box, fixed, so the cue centres before its first frame has loaded
// (measured then, it was 16 px wide and sat off centre).
const RING_BOX = SIZE;
const FRAME_MS = 95; // the loop's own pace
const FADE_MS = 160;
const GAP_PX = 6;
const TEXT_CHARS = 60; // a line
const REPLY_WIDTH_PX = 380; // the reply wraps to two lines at this width
// How long the cue waits for what comes next before it goes by itself: for the words
// after the wake word, for the request to start, for its next step.
const WAIT_LISTENING_MS = 30000;
const WAIT_HEARD_MS = 10000;
const WAIT_STEP_MS = 180000;
// The reply stays this long, and longer for a longer one.
const REPLY_MS = 5000;
const REPLY_MS_PER_CHAR = 45;
const REPLY_LONGEST_MS = 12000;
const ERROR_MS = 3500;
const STOPPED_MS = 1200;

function shorten(text, length = TEXT_CHARS) {
    const line = (text ?? '').replace(/\s+/g, ' ').trim();
    return line.length > length ? `${line.slice(0, length - 1)}…` : line;
}

// Whether `event` came from the user's own mouse or keyboard, not Arcee's virtual ones
// (which have no device node).
function fromUser(event) {
    return event?.get_source_device()?.get_device_node() != null;
}

export class ListeningCue {
    // `onStop` for its Stop button and Esc, `onResume` for Resume.
    constructor(path, {onStop = () => {}, onResume = () => {}} = {}) {
        this._onStop = onStop;
        this._onResume = onResume;
        this._phase = ''; // 'listening', 'transcribing', 'working', 'done'
        this._step = '';
        this._control = 'off';
        this._filter = null;
        const folder = Gio.File.new_for_path(`${path}/icons/cue`);
        this._frames = [];
        for (let i = 0; ; i++) {
            const file = folder.get_child(`${String(i).padStart(2, '0')}.png`);
            if (!file.query_exists(null))
                break;
            this._frames.push(new Gio.FileIcon({file}));
        }
        this._index = 0;
        this._ticker = 0;
        this._limit = 0;
        this._shown = true;
        this._icon = new St.Icon({icon_size: SIZE, gicon: this._frames[0] ?? null});
        this._ring = new St.Bin({
            child: this._icon, width: RING_BOX, height: RING_BOX,
            y_align: Clutter.ActorAlign.CENTER,
            pivot_point: new Graphene.Point({x: 0.5, y: 0.5}),
        });
        this._said = new St.Label({style_class: 'arcee-cue-said'});
        this._doing = new St.Label({style_class: 'arcee-cue-doing'});
        for (const label of [this._said, this._doing])
            label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._text = new St.BoxLayout({
            style_class: 'arcee-cue-text', orientation: Clutter.Orientation.VERTICAL,
            y_align: Clutter.ActorAlign.CENTER, visible: false,
        });
        this._text.add_child(this._said);
        this._text.add_child(this._doing);
        this._resume = new St.Button({
            style_class: 'arcee-cue-button', label: 'Resume', visible: false,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._stop = new St.Button({
            style_class: 'arcee-cue-button arcee-cue-stop', visible: false,
            y_align: Clutter.ActorAlign.CENTER, accessible_name: 'Stop Arcee',
            child: new St.Icon({icon_name: 'media-playback-stop-symbolic', icon_size: 12}),
        });
        this._resume.connect('clicked', () => fromUser(Clutter.get_current_event()) && this._onResume());
        this._stop.connect('clicked', () => fromUser(Clutter.get_current_event()) && this._onStop());
        this._box = new St.BoxLayout({
            style_class: 'arcee-cue', reactive: false, opacity: 0, visible: false,
        });
        for (const child of [this._ring, this._text, this._resume, this._stop])
            this._box.add_child(child);
        // Over the windows, but not chrome: no input region, so clicks pass through.
        Main.layoutManager.uiGroup.add_child(this._box);
    }

    get visible() {
        return this._box.visible;
    }

    // Whether `actor` is part of the pill (its buttons).
    contains(actor) {
        return this._box.contains(actor);
    }

    // Woken (or the mic pressed): the ring, listening.
    show() {
        this._open('', 'Listening…');
        this._phase = 'listening';
        this._expire(WAIT_LISTENING_MS);
    }

    // Shown for a request already running (Arcee took the screen for a typed one).
    follow(request, doing) {
        if (this.visible)
            return;
        this._open(request ? `“${shorten(request)}”` : '', doing || 'Working…');
        this._phase = 'working';
        this.working(true);
        this._expire(WAIT_STEP_MS);
    }

    _open(said, doing) {
        if (!this._frames.length)
            return;
        this._doing.clutter_text.line_wrap = false; // one line, until the reply
        this._doing.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._doing.width = -1;
        this._step = '';
        this._set(said, doing);
        this._box.remove_all_transitions();
        this._box.visible = true;
        this._box.ease({opacity: 255, duration: FADE_MS, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        if (!this._ticker) {
            this._ticker = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_MS, () => {
                this._index = (this._index + 1) % this._frames.length;
                this._icon.gicon = this._frames[this._index];
                return GLib.SOURCE_CONTINUE;
            });
        }
        // Esc, while the pill shows, stops Arcee (and goes no further).
        this._filter ??= Clutter.Event.add_filter(global.stage, event => {
            if (event.type() !== Clutter.EventType.KEY_PRESS ||
                event.get_key_symbol() !== Clutter.KEY_Escape || !fromUser(event))
                return Clutter.EVENT_PROPAGATE;
            this._onStop();
            return Clutter.EVENT_STOP;
        });
    }

    // The voice has stopped; Whisper has the audio.
    transcribing() {
        if (this.visible && this._phase === 'listening') {
            this._phase = 'transcribing';
            this._set('', 'Getting your words…');
        }
    }

    // A request is running (Stop shown) or not.
    working(on) {
        this._stop.visible = on;
        if (on)
            this._phase = 'working';
        this._place();
    }

    // Arcee's control of the screen: 'active', 'paused' (the user took over) or 'off'.
    control(state) {
        this._control = state;
        const paused = state === 'paused';
        if (paused)
            this._box.add_style_class_name('paused');
        else
            this._box.remove_style_class_name('paused');
        this._resume.visible = paused;
        // Only while the request runs: once done, the pill keeps its reply.
        if (this.visible && this._phase === 'working')
            this._set(null, paused ? 'Paused: you have control' : this._step || 'Working…');
        else
            this._place();
    }

    // Stopped by the user (Esc, Stop): said briefly, then gone.
    stopped() {
        if (!this.visible)
            return;
        if (this._phase === 'done') {
            this.hide(); // only a reply showing: Esc just puts it away
            return;
        }
        this.working(false);
        this._phase = 'done';
        this._set(null, 'Stopped');
        this._expire(STOPPED_MS);
    }

    // What the user said.
    heard(text) {
        if (!this.visible)
            return;
        this._phase = 'working';
        this._set(`“${shorten(text)}”`, 'Starting…');
        this._expire(WAIT_HEARD_MS);
    }

    // What Arcee is doing about it now.
    doing(text) {
        if (!this.visible || !text)
            return;
        this._step = text;
        if (this._control !== 'paused')
            this._set(null, text);
        this._expire(WAIT_STEP_MS);
    }

    // Whether `reply` fits on the cue whole.
    fits(reply) {
        return (reply ?? '').replace(/\s+/g, ' ').trim().length <= 2 * TEXT_CHARS;
    }

    // Done: the reply, for long enough to read it.
    finish(reply) {
        if (!this.visible)
            return;
        this.working(false);
        this._phase = 'done';
        const text = shorten(reply, 2 * TEXT_CHARS);
        // A reply longer than a line wraps to a second one, at a fixed width.
        const wraps = text.length > TEXT_CHARS;
        this._doing.clutter_text.line_wrap = wraps;
        // Clutter keeps an ellipsized label to one line; the text is already cut short.
        this._doing.clutter_text.ellipsize = wraps ? Pango.EllipsizeMode.NONE : Pango.EllipsizeMode.END;
        this._doing.width = wraps ? REPLY_WIDTH_PX : -1;
        this._set(null, text);
        this._ring.ease({scale_x: 1, scale_y: 1, duration: 80, mode: Clutter.AnimationMode.LINEAR});
        this._expire(Math.min(REPLY_LONGEST_MS, REPLY_MS + text.length * REPLY_MS_PER_CHAR));
    }

    fail(text) {
        if (!this.visible)
            return;
        this.working(false);
        this._phase = 'done';
        this._set(null, text);
        this._expire(ERROR_MS);
    }

    // How loud the voice is (0..1): the ring swells a little with it.
    level(loudness) {
        if (!this.visible)
            return;
        const scale = 1 + Math.min(1, Math.sqrt(loudness) * 2.5) * 0.12;
        this._ring.ease({scale_x: scale, scale_y: scale, duration: 80, mode: Clutter.AnimationMode.LINEAR});
    }

    hide() {
        if (this._filter) {
            Clutter.Event.remove_filter(this._filter);
            this._filter = null;
        }
        if (!this.visible)
            return;
        this._clearLimit();
        this._box.remove_all_transitions();
        this._box.ease({
            opacity: 0, duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => {
                this._box.visible = false;
                this._ring.set_scale(1, 1);
                if (this._ticker) {
                    GLib.source_remove(this._ticker);
                    this._ticker = 0;
                }
                this._stop.visible = this._resume.visible = false;
                this._phase = '';
            },
        });
    }

    // Hidden for the moment of a screenshot: Arcee reads the user's screen, not its own.
    setShown(shown) {
        this._shown = shown;
        this._box.opacity = shown && this.visible ? 255 : 0;
    }

    destroy() {
        if (this._filter)
            Clutter.Event.remove_filter(this._filter);
        this._filter = null;
        this._clearLimit();
        if (this._ticker)
            GLib.source_remove(this._ticker);
        this._ticker = 0;
        this._box.destroy();
    }

    // `said` null keeps what is there.
    _set(said, doing) {
        if (said !== null) {
            this._said.text = said;
            this._said.visible = Boolean(said);
        }
        const wraps = this._doing.clutter_text.line_wrap;
        this._doing.text = shorten(doing, wraps ? 2 * TEXT_CHARS : TEXT_CHARS);
        this._text.visible = Boolean(this._said.text || this._doing.text);
        this._place();
    }

    // Centred below the top bar of the primary monitor, at its new width.
    _place() {
        const monitor = Main.layoutManager.primaryMonitor;
        const [, width] = this._box.get_preferred_width(-1);
        const y = monitor.y + Main.panel.height + GAP_PX;
        this._box.set_position(Math.round(monitor.x + (monitor.width - width) / 2), y);
    }

    _expire(ms) {
        this._clearLimit();
        this._limit = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._limit = 0;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearLimit() {
        if (this._limit) {
            GLib.source_remove(this._limit);
            this._limit = 0;
        }
    }
}
