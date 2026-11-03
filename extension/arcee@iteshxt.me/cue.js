// The voice cue: when Arcee hears "hey Arcee" (or the mic is pressed), its ring appears
// at the top centre of the screen, pulsing with the voice. Beside it, what the user
// said and what Arcee is doing about it, step by step, then its reply for a moment: a
// spoken request can be followed without opening the panel. The ring is the app's
// active loop, as frames (icons/cue/NN.png, from tools/make_art.py): GNOME 50's shell
// has no sprite-sheet animation, so the frames are shown one after another, and only
// while the cue is on screen.
//
// It takes no clicks, so Arcee's own clicks near the top of the screen go through to
// the window under it, and it hides for Arcee's screenshots (`setShown`).

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

function shorten(text, length = TEXT_CHARS) {
    const line = (text ?? '').replace(/\s+/g, ' ').trim();
    return line.length > length ? `${line.slice(0, length - 1)}…` : line;
}

export class ListeningCue {
    // `onPlaced(bottom)` hears where the cue ends on screen (0 once it is gone), so the
    // control session's pill can sit below it.
    constructor(path, onPlaced = () => {}) {
        this._onPlaced = onPlaced;
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
        this._box = new St.BoxLayout({
            style_class: 'arcee-cue', reactive: false, opacity: 0, visible: false,
        });
        this._box.add_child(this._ring);
        this._box.add_child(this._text);
        // Over the windows, but not chrome: no input region, so clicks pass through.
        Main.layoutManager.uiGroup.add_child(this._box);
    }

    get visible() {
        return this._box.visible;
    }

    // Woken (or the mic pressed): the ring, listening.
    show() {
        if (!this._frames.length)
            return;
        this._doing.clutter_text.line_wrap = false; // one line, until the reply
        this._doing.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._doing.width = -1;
        this._set('', 'Listening…');
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
        this._expire(WAIT_LISTENING_MS);
    }

    // The voice has stopped; Whisper has the audio.
    transcribing() {
        if (this.visible)
            this._set('', 'Getting your words…');
    }

    // What the user said.
    heard(text) {
        if (!this.visible)
            return;
        this._set(`“${shorten(text)}”`, 'Starting…');
        this._expire(WAIT_HEARD_MS);
    }

    // What Arcee is doing about it now.
    doing(text) {
        if (!this.visible || !text)
            return;
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
                this._onPlaced(0);
            },
        });
    }

    // Hidden for the moment of a screenshot: Arcee reads the user's screen, not its own.
    setShown(shown) {
        this._shown = shown;
        this._box.opacity = shown && this.visible ? 255 : 0;
    }

    destroy() {
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
        const [, height] = this._box.get_preferred_height(width);
        const y = monitor.y + Main.panel.height + GAP_PX;
        this._box.set_position(Math.round(monitor.x + (monitor.width - width) / 2), y);
        this._onPlaced(y + height);
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
