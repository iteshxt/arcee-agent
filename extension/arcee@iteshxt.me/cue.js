// Arcee's pill at the top centre: when Arcee hears "hey Arcee" (or the mic is pressed),
// its ring appears, pulsing with the voice. Beside it, what the user said and what
// Arcee is doing about it, step by step, for as long as the request runs; then Arcee's
// reply, the pill growing to hold it. The pill is the conversation: it stays, with the
// last reply, until the user closes it (Esc, or a click anywhere), and "hey Arcee"
// while it shows goes on with the same conversation (arceed gives the next request
// the turns before it; closing the pill ends it). A spoken request can be followed
// without opening the panel.
// While a request runs it has a Stop button; while Arcee holds the screen (control.js)
// it shows too, typed requests included, and offers Resume once the user takes over.
// Esc, while a request runs, stops everything.
//
// The ring is the app's active loop, as frames (icons/cue/NN.png, from
// tools/make_art.py): GNOME 50's shell has no sprite-sheet animation, so the frames are
// shown one after another, and only while the pill is on screen.
//
// Only its buttons take clicks, and only the user's (not Arcee's own virtual pointer):
// Arcee's clicks near the top of the screen go through to the window under it. It
// hides for Arcee's screenshots (`setShown`).
//
// Esc is a shortcut while it shows (grab_accelerator): GNOME hands keys and clicks
// meant for an app window to the app before any extension's event filter sees them,
// so a filter only heard Esc over the Shell's own UI. A finished pill also closes when
// the user goes to another window (a click inside a window cannot be seen at all).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Graphene from 'gi://Graphene';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {plain, toPango} from './markdown.js';

// Small, at the user's word: noticeable, out of the way (35 px: 15% up from 30 px, at
// their word again). The ring's box is fixed, so the pill centres before its first
// frame has loaded (measured then, it was 16 px wide and sat off centre).
const SIZE = 35;
const FRAME_MS = 95; // the loop's own pace
const FADE_MS = 160;
const GAP_PX = 6;
const TEXT_CHARS = 64; // a line of what was said, or of a step
const REPLY_CHARS = 700; // the rest is in the panel and the app
const REPLY_WIDTH_PX = 560; // a reply wraps at this width
// How long the pill waits for what comes next before it goes by itself: for the words
// after the wake word, for the request to start. While a request runs it stays (until
// it ends, or nothing has been heard of it for SILENT_RUN_MS: arceed went away).
const WAIT_LISTENING_MS = 30000;
const WAIT_HEARD_MS = 60000;
const SILENT_RUN_MS = 15 * 60000;
const ERROR_MS = 3500;
const STOPPED_MS = 1200;

function oneLine(text) {
    return (text ?? '').replace(/\s+/g, ' ').trim();
}

function shorten(text, length = TEXT_CHARS) {
    const line = oneLine(text);
    return line.length > length ? `${line.slice(0, length - 1)}…` : line;
}

// Whether `event` came from the user's own mouse or keyboard, not Arcee's virtual ones
// (which have no device node).
function fromUser(event) {
    return event?.get_source_device()?.get_device_node() != null;
}

const HINT = 'Say “Hey Arcee” to go on  ·  Esc to close';
const PRESSES = new Set([Clutter.EventType.BUTTON_PRESS, Clutter.EventType.TOUCH_BEGIN]);
// A focus change this soon after the reply is Arcee's own doing (an app it opened).
const SETTLE_MS = 1500;

export class ListeningCue {
    // `onStop` for its Stop button and Esc while something runs, `onResume` for Resume,
    // `onDismiss` when the user closes the pill (the conversation ends).
    // `onAnswer(id, answer)` when the user clicks an answer to Arcee's question.
    constructor(path, {
        onStop = () => {}, onResume = () => {}, onDismiss = () => {}, onAnswer = () => {},
        typedJustNow = () => false,
    } = {}) {
        // Whether Arcee itself just pressed keys: its own Esc must not stop it.
        this._typedJustNow = typedJustNow;
        this._escAction = 0;
        this._escName = '';
        this._escSignal = 0;
        this._focusSignal = 0;
        this._doneAt = 0;
        this._onStop = onStop;
        this._onAnswer = onAnswer;
        this._questionId = null;
        this._onResume = onResume;
        this._onDismiss = onDismiss;
        // A request has run since the pill opened: it stays, as the conversation, with
        // the last reply to come back to.
        this._conversation = false;
        this._lastSaid = '';
        this._lastReply = '';
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
        this._icon = new St.Icon({icon_size: SIZE, gicon: this._frames[0] ?? null});
        this._ring = new St.Bin({
            child: this._icon, width: SIZE, height: SIZE,
            y_align: Clutter.ActorAlign.CENTER,
            pivot_point: new Graphene.Point({x: 0.5, y: 0.5}),
        });
        this._said = new St.Label({style_class: 'arcee-cue-said'});
        this._doing = new St.Label({style_class: 'arcee-cue-doing'});
        for (const label of [this._said, this._doing])
            label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        // The reply has a label of its own, wrapping from the start: one switched to
        // wrapping later kept its one-line height and spilled out of the pill.
        this._reply = new St.Label({style_class: 'arcee-cue-reply', visible: false});
        this._reply.clutter_text.line_wrap = true;
        this._reply.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        this._reply.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        this._text = new St.BoxLayout({
            style_class: 'arcee-cue-text', orientation: Clutter.Orientation.VERTICAL,
            y_align: Clutter.ActorAlign.CENTER, visible: false,
        });
        this._hint = new St.Label({style_class: 'arcee-cue-hint', visible: false, text: HINT});
        // Arcee's question's answers, as buttons under it.
        this._answers = new St.BoxLayout({style_class: 'arcee-cue-answers', visible: false});
        for (const child of [this._said, this._doing, this._reply, this._answers, this._hint])
            this._text.add_child(child);
        this._resume = new St.Button({
            style_class: 'arcee-cue-button', label: 'Resume', visible: false,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._stop = new St.Button({
            style_class: 'arcee-cue-button arcee-cue-stop', visible: false,
            y_align: Clutter.ActorAlign.CENTER, accessible_name: 'Stop Arcee',
            child: new St.Icon({icon_name: 'media-playback-stop-symbolic', icon_size: 14}),
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
        this._expire(WAIT_LISTENING_MS, () => this._settle());
    }

    // Shown for a request already running (Arcee took the screen for a typed one).
    follow(request, doing) {
        if (this.visible && this._phase !== 'done')
            return;
        this._open(request ? `“${shorten(request)}”` : '', doing || 'Working…');
        this.working(true);
    }

    // The voice has stopped; Whisper has the audio.
    transcribing() {
        if (this.visible && this._phase === 'listening') {
            this._phase = 'transcribing';
            this._set('', 'Getting your words…');
        }
    }

    // What the user said: the request starts.
    heard(text) {
        if (!this.visible)
            return;
        this._phase = 'working';
        this._set(`“${shorten(text)}”`, 'Starting…');
        this._expire(WAIT_HEARD_MS, () => this._settle());
    }

    // A request is running (Stop shown, and the pill stays) or not.
    working(on) {
        this._stop.visible = on;
        if (on) {
            this._phase = 'working';
            this._conversation = true;
            this._expire(SILENT_RUN_MS, () => this._settle());
        }
        this._place();
    }

    // What Arcee is doing about it now.
    doing(text) {
        // Not over a reply, nor over a question waiting for its answer.
        if (!this.visible || !text || this._phase === 'done' || this._phase === 'asking')
            return;
        this._step = text;
        if (this._control !== 'paused')
            this._set(null, text);
        this._expire(SILENT_RUN_MS, () => this._settle());
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

    // Done: Arcee's reply, the pill as big as it needs. It stays (the conversation goes
    // on) until the user closes it. `request` is shown above it when the pill was not up.
    reply(text, request = '') {
        if (!this._frames.length)
            return;
        if (!this.visible)
            this._open(request ? `“${shorten(request)}”` : '', '');
        // The models write Markdown: shown formatted, its line breaks (lists) kept.
        let words = (text ?? '').trim();
        if (words.length > REPLY_CHARS)
            words = `${words.slice(0, REPLY_CHARS - 1)}… (the rest is in Arcee)`;
        this._conversation = true;
        this._lastSaid = this._said.text;
        this._lastReply = words;
        this._showReply(words);
    }

    _showReply(words) {
        this._phase = 'done';
        this._doneAt = GLib.get_monotonic_time();
        this._stop.visible = false;
        this._resume.visible = false;
        this._ring.ease({scale_x: 1, scale_y: 1, duration: 80, mode: Clutter.AnimationMode.LINEAR});
        this._doing.visible = false;
        this._renderReply(words);
        this._hint.text = HINT;
        this._hint.visible = true;
        this._place();
        this._clearLimit(); // it stays: the user closes it
    }

    // Arcee asks (a confirmation, or a question with choices): the question, its answers
    // as buttons, and the voice works too ("hey Arcee", then the answer). False when
    // the pill is not up (the panel asks instead).
    ask(question) {
        if (!this.visible)
            return false;
        this._questionId = question.id;
        this._phase = 'asking';
        this._clearLimit();
        const confirm = question.kind === 'confirm';
        const choices = confirm ? (question.allowable ? ['Allow', 'Always allow', 'Deny'] : ['Allow', 'Deny'])
            : (question.options ?? []).slice(0, 4);
        this._answers.destroy_all_children();
        for (const [n, choice] of choices.entries()) {
            const value = confirm ? {'Allow': 'yes', 'Always allow': 'always', 'Deny': 'no'}[choice] : choice;
            const button = new St.Button({
                style_class: `arcee-cue-button${n === 0 ? ' arcee-cue-primary' : ''}`, label: choice,
            });
            button.connect('clicked', () => {
                if (fromUser(Clutter.get_current_event()))
                    this._onAnswer(question.id, value);
            });
            this._answers.add_child(button);
        }
        let text = question.text ?? '';
        if (question.preview?.length)
            text += `\n${question.preview.slice(0, 4).map(p => `- ${p}`).join('\n')}`;
        this._doing.visible = false;
        this._renderReply(text);
        this._answers.visible = choices.length > 0;
        this._choices = choices.length;
        this._saying = confirm ? 'say “allow” or “deny”' : choices.length ? 'say your choice' : 'say your answer';
        this._hint.text = choices.length ? `Click, or ${this._saying}` : this._saying;
        this._hint.visible = true;
        this._place();
        return true;
    }

    // A question waits here for its answer.
    get asking() {
        return this.visible && this._phase === 'asking';
    }

    // The mic is open for the answer (`on`), or closed: the question and its buttons
    // stay; `note` says why it closed ("Say yes or no").
    hearAnswer(on, note = '') {
        if (!this.asking)
            return;
        if (on)
            this._hint.text = `Listening… ${this._saying}${this._choices ? ', or click' : ''}`;
        else
            this._hint.text = note || (this._choices ? `Click, or ${this._saying}` : this._saying);
        this._hint.visible = true;
        this._place();
    }

    // The question was answered (here, by voice, or in the panel or the app).
    answered(questionId) {
        if (this._questionId !== questionId)
            return;
        this._questionId = null;
        this._answers.visible = false;
        this._answers.destroy_all_children();
        this._hint.text = HINT;
        if (this.visible && this._phase === 'asking') {
            this._phase = 'working';
            this._reply.visible = false;
            this._hint.visible = false;
            this._set(null, this._step || 'Going on…');
            this._expire(SILENT_RUN_MS, () => this._settle());
        }
    }

    // `words` (Markdown) on the reply label: as wide as they need, up to REPLY_WIDTH_PX.
    _renderReply(words) {
        this._reply.set_size(-1, -1);
        try {
            this._reply.clutter_text.set_markup(toPango(words));
        } catch {
            this._reply.text = plain(words);
        }
        this._reply.visible = true;
        // As wide as the reply needs, up to REPLY_WIDTH_PX, and as tall as it wraps to at
        // that width: set both, as the pill's layout asked the label's height without its
        // width and grew by one line only.
        const [, natural] = this._reply.get_preferred_width(-1);
        const width = Math.min(REPLY_WIDTH_PX, Math.ceil(natural));
        const [, height] = this._reply.get_preferred_height(width);
        this._reply.set_size(width, Math.ceil(height));
        this._text.visible = true;
    }

    // Nothing more is coming (no words after the wake word, an error shown, a step
    // that never ended): back to the conversation's last reply, or gone if there is none.
    _settle() {
        if (!this._conversation) {
            this.hide();
            return;
        }
        if (this._lastReply) {
            this._said.text = this._lastSaid;
            this._said.visible = Boolean(this._lastSaid);
            this._showReply(this._lastReply);
            return;
        }
        this._phase = 'done';
        this._doneAt = GLib.get_monotonic_time();
        this.working(false);
        this._hint.visible = true;
        this._place();
        this._clearLimit();
    }

    // The user closed the pill: the conversation ends.
    dismiss() {
        this.hide();
        this._onDismiss();
    }

    fail(text) {
        if (!this.visible)
            return;
        this.working(false);
        this._phase = 'done';
        this._doneAt = GLib.get_monotonic_time();
        this._set(null, text);
        this._expire(ERROR_MS, () => this._settle());
    }

    // Stopped by the user (Esc, Stop): said briefly, then gone.
    stopped() {
        if (!this.visible)
            return;
        this.working(false);
        this._phase = 'done';
        this._doneAt = GLib.get_monotonic_time();
        this._set(null, 'Stopped');
        this._hint.visible = this._conversation;
        if (this._conversation)
            this._clearLimit(); // the conversation goes on
        else
            this._expire(STOPPED_MS);
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
        this._releaseKeys();
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
                this._conversation = false;
                this._lastSaid = this._lastReply = '';
            },
        });
    }

    // Hidden for the moment of a screenshot: Arcee reads the user's screen, not its own.
    setShown(shown) {
        this._box.opacity = shown && this.visible ? 255 : 0;
    }

    destroy() {
        if (this._filter)
            Clutter.Event.remove_filter(this._filter);
        this._filter = null;
        this._releaseKeys();
        this._clearLimit();
        if (this._ticker)
            GLib.source_remove(this._ticker);
        this._ticker = 0;
        this._box.destroy();
    }

    _open(said, doing) {
        if (!this._frames.length)
            return;
        this._step = '';
        this._reply.visible = false;
        this._hint.visible = false;
        this._doing.visible = true;
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
        this._filter ??= Clutter.Event.add_filter(global.stage, (event, actor) => this._onEvent(event, actor));
        this._grabKeys();
    }

    // Esc as a shortcut, and the focus watch, for as long as the pill shows.
    _grabKeys() {
        if (!this._escAction) {
            const action = global.display.grab_accelerator('Escape', Meta.KeyBindingFlags.NONE);
            if (action !== Meta.KeyBindingAction.NONE) {
                this._escAction = action;
                this._escName = Meta.external_binding_name_for_action(action);
                Main.wm.allowKeybinding(this._escName, Shell.ActionMode.ALL);
                this._escSignal = global.display.connect('accelerator-activated', (_display, which, device) => {
                    if (which !== this._escAction)
                        return;
                    // A real keyboard has a device node; Arcee's virtual one has none, and
                    // when GNOME does not say, an Esc right after Arcee typed is its own.
                    const ours = device ? device.get_device_node?.() == null && this._typedJustNow() : this._typedJustNow();
                    if (!ours)
                        this._escape();
                });
            }
        }
        this._focusSignal ||= global.display.connect('notify::focus-window', () => {
            if (this._phase === 'done' && GLib.get_monotonic_time() - this._doneAt > SETTLE_MS * 1000)
                this.dismiss(); // the user went to another window
        });
    }

    _releaseKeys() {
        if (this._escAction) {
            global.display.disconnect(this._escSignal);
            global.display.ungrab_accelerator(this._escAction);
            Main.wm.allowKeybinding(this._escName, Shell.ActionMode.NONE);
            this._escAction = this._escSignal = 0;
        }
        if (this._focusSignal) {
            global.display.disconnect(this._focusSignal);
            this._focusSignal = 0;
        }
    }

    // Esc: stop what runs; once done, close the pill (and end the conversation).
    _escape() {
        if (this._phase === 'done')
            this.dismiss();
        else
            this._onStop();
    }

    // Esc stops what runs (and goes no further); once done, Esc or a click anywhere but
    // the pill's buttons closes it and ends the conversation (the click still lands).
    _onEvent(event, actor) {
        if (!fromUser(event))
            return Clutter.EVENT_PROPAGATE;
        const type = event.type();
        if (type === Clutter.EventType.KEY_PRESS && event.get_key_symbol() === Clutter.KEY_Escape) {
            if (!this._escAction)
                this._escape(); // over the Shell's own UI, when the shortcut could not be had
            return Clutter.EVENT_PROPAGATE; // the shortcut handles it everywhere else
        }
        if (PRESSES.has(type) && this._phase === 'done' && !(actor && this.contains(actor)))
            this.dismiss();
        return Clutter.EVENT_PROPAGATE;
    }

    // `said` null keeps what is there.
    _set(said, doing) {
        if (said !== null) {
            this._said.text = said;
            this._said.visible = Boolean(said);
        }
        this._doing.text = shorten(doing);
        this._doing.visible = Boolean(this._doing.text);
        this._reply.visible = false;
        this._hint.visible = false;
        this._answers.visible = false;
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

    _expire(ms, then = () => this.hide()) {
        this._clearLimit();
        this._limit = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._limit = 0;
            then();
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
