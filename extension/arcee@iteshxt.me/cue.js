// The listening cue: when Arcee hears "hey Arcee" (or the mic is pressed), its ring
// appears at the top centre of the screen, pulsing with the voice, and goes once Arcee
// has the words. The ring is the app's active loop, as frames (icons/cue/NN.png, from
// tools/make_art.py): GNOME 50's shell has no sprite-sheet animation, so the frames
// are shown one after another, and only while the cue is on screen.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Graphene from 'gi://Graphene';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const SIZE = 96;
// The ring with its disc (.arcee-cue's 8 px padding): fixed, so it centres before its
// first frame has loaded (measured then, it was 16 px wide and sat off centre).
const BOX = SIZE + 16;
const FRAME_MS = 95; // the loop's own pace
const FADE_MS = 160;
const GAP_PX = 12;
// Once the words are in (or nothing came), the cue goes; and never stays longer than
// this, whatever happened to the request.
const LONGEST_MS = 30000;

export class ListeningCue {
    constructor(path) {
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
        this._icon = new St.Icon({icon_size: SIZE, gicon: this._frames[0] ?? null});
        this._ring = new St.Bin({
            style_class: 'arcee-cue', child: this._icon, opacity: 0, visible: false,
            width: BOX, height: BOX,
            pivot_point: new Graphene.Point({x: 0.5, y: 0.5}),
        });
        Main.layoutManager.addTopChrome(this._ring);
    }

    show() {
        if (!this._frames.length)
            return;
        const monitor = Main.layoutManager.primaryMonitor;
        this._ring.set_position(
            Math.round(monitor.x + (monitor.width - BOX) / 2),
            monitor.y + Main.panel.height + GAP_PX);
        this._ring.remove_all_transitions();
        this._ring.visible = true;
        this._ring.ease({opacity: 255, duration: FADE_MS, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        if (!this._ticker) {
            this._ticker = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_MS, () => {
                this._index = (this._index + 1) % this._frames.length;
                this._icon.gicon = this._frames[this._index];
                return GLib.SOURCE_CONTINUE;
            });
        }
        if (this._limit)
            GLib.source_remove(this._limit);
        this._limit = GLib.timeout_add(GLib.PRIORITY_DEFAULT, LONGEST_MS, () => {
            this._limit = 0;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    // How loud the voice is (0..1): the ring swells a little with it.
    level(loudness) {
        if (!this._ring.visible)
            return;
        const scale = 1 + Math.min(1, Math.sqrt(loudness) * 2.5) * 0.12;
        this._ring.ease({scale_x: scale, scale_y: scale, duration: 80, mode: Clutter.AnimationMode.LINEAR});
    }

    hide() {
        if (!this._ring.visible)
            return;
        if (this._limit) {
            GLib.source_remove(this._limit);
            this._limit = 0;
        }
        this._ring.remove_all_transitions();
        this._ring.ease({
            opacity: 0, scale_x: 1, scale_y: 1, duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => {
                this._ring.visible = false;
                if (this._ticker) {
                    GLib.source_remove(this._ticker);
                    this._ticker = 0;
                }
            },
        });
    }

    destroy() {
        for (const id of [this._ticker, this._limit]) {
            if (id)
                GLib.source_remove(id);
        }
        this._ticker = this._limit = 0;
        Main.layoutManager.removeChrome(this._ring);
        this._ring.destroy();
    }
}
