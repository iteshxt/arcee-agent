// Arcee's icon in the top bar and its menu: ask (typed or spoken), follow what Arcee is
// doing and stop it, answer its questions, a few recent requests (more in the app), and
// today's model budget as bars. It talks to arceed (me.iteshxt.Arcee.Agent), which D-Bus
// starts on the first request; what the user says is run by arceed itself.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {ListeningCue} from './cue.js';
import {plain, toPango} from './markdown.js';

const AGENT_INTERFACE = `
<node>
  <interface name="me.iteshxt.Arcee.Agent">
    <method name="Run"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="Cancel"/>
    <method name="Quit"/>
    <method name="ShowApp"><arg type="s" direction="in"/></method>
    <method name="Answer"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Status"><arg type="s" direction="out"/></method>
    <method name="Recent"><arg type="u" direction="in"/><arg type="s" direction="out"/></method>
    <method name="Limits"><arg type="s" direction="out"/></method>
    <method name="StartListening"/>
    <method name="StopListening"/>
    <method name="EndConversation"/>
    <signal name="Started"><arg type="s"/></signal>
    <signal name="Progress"><arg type="s"/></signal>
    <signal name="Step"><arg type="s"/></signal>
    <signal name="Question"><arg type="s"/></signal>
    <signal name="Finished"><arg type="s"/></signal>
    <signal name="Listening"><arg type="b"/></signal>
    <signal name="Level"><arg type="d"/></signal>
    <signal name="Heard"><arg type="s"/></signal>
  </interface>
</node>`;
const AgentProxy = Gio.DBusProxy.makeProxyWrapper(AGENT_INTERFACE);

const APP_ID = 'me.iteshxt.Arcee';
const PROGRESS_LINES = 3;
const RECENT_TASKS = 3;
const WAVE_BARS = 32;
const WAVE_HEIGHT = 28;
const BUDGET_BAR_PX = 120;

function shorten(text, length) {
    const line = (text ?? '').replace(/\s+/g, ' ').trim();
    return line.length > length ? `${line.slice(0, length - 1)}…` : line;
}

function thousands(n) {
    return n >= 1000 ? `${Math.round(n / 1000)}K` : `${n}`;
}

// A row inside the menu that holds widgets, not a clickable item.
function section(child) {
    const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
    item.add_child(child);
    return item;
}

export const ArceeIndicator = GObject.registerClass(
class ArceeIndicator extends PanelMenu.Button {
    // `onStep` hears what Arcee is doing now ('' when it is done); `stopControl` and
    // `resumeControl` act on the control session (ShellBridge).
    _init(path, {onStep = () => {}, stopControl = () => {}, resumeControl = () => {}} = {}) {
        super._init(0.5, 'Arcee', false);
        this._onStep = onStep;
        this._stopControl = stopControl;
        // Arcee's pill at the top centre: for a spoken request, the ring while Arcee
        // listens (the wake word, or the mic), then what was said, each step and the
        // reply; also whenever Arcee holds the screen. Stop (and Esc) stop it all.
        this._cue = new ListeningCue(path, {
            onStop: () => this._stopAll(),
            onResume: resumeControl,
            // The user closed the pill: the next request starts a new conversation.
            onDismiss: () => this._agent.EndConversationAsync().catch(logError),
        });
        this._spoken = false; // the request under way is followed on the pill
        this._request = '';
        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${path}/icons/arcee.svg`),
            style_class: 'system-status-icon',
        }));
        this.menu.actor.add_style_class_name('arcee-menu');
        this._progress = [];
        this._listening = false;
        this._buildMenu();
        this._agent = new AgentProxy(
            Gio.DBus.session, 'me.iteshxt.Arcee.Agent', '/me/iteshxt/Arcee/Agent', null, null,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION);
        this._signals = [
            this._agent.connectSignal('Started', (_p, _s, [request]) => this._onStarted(request)),
            this._agent.connectSignal('Progress', (_p, _s, [line]) => this._onProgress(line)),
            this._agent.connectSignal('Step', (_p, _s, [doing]) => this._onStepChanged(doing)),
            this._agent.connectSignal('Question', (_p, _s, [json]) => this._onQuestion(json)),
            this._agent.connectSignal('Finished', (_p, _s, [json]) => this._onFinished(json)),
            this._agent.connectSignal('Listening', (_p, _s, [on]) => this._onListening(on)),
            this._agent.connectSignal('Level', (_p, _s, [loudness]) => this._onLevel(loudness)),
            this._agent.connectSignal('Heard', (_p, _s, [json]) => this._onHeard(json)),
        ];
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (!open)
                return;
            this._matchMenuShade();
            this._refresh();
            // Open the menu and type: the field takes the keyboard (unless a question waits).
            if (!this._question.visible) {
                GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                    this._entry.grab_key_focus();
                    return GLib.SOURCE_REMOVE;
                });
            }
        });
    }

    // Hidden for the moment of a screenshot (see ShellBridge.Screenshot).
    setCueShown(shown) {
        this._cue.setShown(shown);
    }

    // Arcee took the screen ('active'), the user took it back ('paused'), or it ended:
    // the pill shows it, and follows the request even when it was typed.
    controlChanged(state) {
        if (state !== 'off' && !this._cue.visible) {
            this._cue.follow(this._request, this._step);
            this._spoken = true;
        }
        this._cue.control(state);
    }

    // Whether `actor` is the pill's (pressing Stop is not the user taking control).
    ownsActor(actor) {
        return this._cue.contains(actor);
    }

    // Stop, or Esc: whatever is under way ends, heard or running.
    _stopAll() {
        this._agent.CancelAsync().catch(logError);
        this._stopControl();
        this._cue.stopped();
        this._spoken = false;
    }

    destroy() {
        for (const id of this._signals ?? [])
            this._agent.disconnectSignal(id);
        this._cue.destroy();
        super.destroy();
    }

    _buildMenu() {
        // Ask: type, or press the mic.
        const ask = new St.BoxLayout({style_class: 'arcee-ask', x_expand: true});
        this._entry = new St.Entry({
            style_class: 'arcee-entry', hint_text: 'Ask Arcee…', can_focus: true, x_expand: true,
        });
        this._entry.clutter_text.connect('activate', () => this._run());
        this._mic = new St.Button({
            style_class: 'arcee-mic', can_focus: true,
            child: new St.Icon({icon_name: 'audio-input-microphone-symbolic', icon_size: 16}),
        });
        this._mic.connect('clicked', () => this._toggleListening());
        ask.add_child(this._entry);
        ask.add_child(this._mic);
        this.menu.addMenuItem(section(ask));

        // The waveform, while the mic listens.
        const bars = new St.BoxLayout({style_class: 'arcee-wave', x_expand: true,
            x_align: Clutter.ActorAlign.CENTER});
        this._bars = [];
        this._levels = new Array(WAVE_BARS).fill(0);
        for (let i = 0; i < WAVE_BARS; i++) {
            const bar = new St.Widget({style_class: 'arcee-wave-bar', y_align: Clutter.ActorAlign.CENTER});
            bar.height = 3;
            bars.add_child(bar);
            this._bars.push(bar);
        }
        this._wave = section(bars);
        this._wave.visible = false;
        this.menu.addMenuItem(this._wave);

        // What Arcee is doing, or what it last said.
        const card = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL,
            style_class: 'arcee-card', x_expand: true});
        this._statusTitle = new St.Label({style_class: 'arcee-card-title'});
        this._statusTitle.clutter_text.line_wrap = true;
        this._statusBody = new St.Label({style_class: 'arcee-card-body'});
        this._statusBody.clutter_text.line_wrap = true;
        this._stop = new St.Button({style_class: 'arcee-button arcee-stop', label: 'Stop',
            x_align: Clutter.ActorAlign.START});
        this._stop.connect('clicked', () => this._agent.CancelAsync().catch(logError));
        card.add_child(this._statusTitle);
        card.add_child(this._statusBody);
        card.add_child(this._stop);
        this._status = section(card);
        this._status.visible = false;
        this.menu.addMenuItem(this._status);

        // A question or a confirmation, while one waits.
        this._questionBox = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL,
            style_class: 'arcee-card arcee-question', x_expand: true});
        this._question = section(this._questionBox);
        this._question.visible = false;
        this.menu.addMenuItem(this._question);

        // Recent requests, folded: three, and the rest in the app.
        this._recent = new PopupMenu.PopupSubMenuMenuItem('Recent', true);
        this._recent.icon.icon_name = 'document-open-recent-symbolic';
        this.menu.addMenuItem(this._recent);

        // Today's budget, a bar per model.
        this._budget = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL,
            style_class: 'arcee-budget', x_expand: true});
        this.menu.addMenuItem(section(this._budget));

        // Open the app, or quit.
        const footer = new St.BoxLayout({style_class: 'arcee-footer', x_expand: true});
        const open = new St.Button({style_class: 'arcee-button arcee-primary', label: 'Open Arcee',
            x_expand: true});
        open.connect('clicked', () => this._showApp(''));
        const quit = new St.Button({style_class: 'arcee-button', label: 'Quit'});
        quit.connect('clicked', () => {
            this.menu.close();
            this._agent.QuitAsync().catch(() => {});
        });
        footer.add_child(open);
        footer.add_child(quit);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(section(footer));
    }

    _showApp(page) {
        this.menu.close();
        this._agent.ShowAppAsync(page).catch(logError);
    }

    // --- asking --------------------------------------------------------------------

    _run() {
        const text = this._entry.get_text().trim();
        if (!text)
            return;
        this._entry.set_text('');
        this._agent.RunAsync(text).catch(error => this._say('', this._cleanError(error)));
    }

    _toggleListening() {
        const call = this._listening ? this._agent.StopListeningAsync()
            : this._agent.StartListeningAsync();
        call.catch(error => this._say('', this._cleanError(error)));
    }

    _cleanError(error) {
        return (error.message ?? `${error}`).replace(/^GDBus\.Error:[\w.-]+:\s*/, '');
    }

    // `markdown`: the body is Arcee's reply, written in Markdown, shown formatted.
    _say(title, body, working = false, markdown = false) {
        this._statusTitle.text = shorten(title, 160);
        this._statusTitle.visible = Boolean(title);
        try {
            if (markdown)
                this._statusBody.clutter_text.set_markup(toPango(body));
            else
                this._statusBody.text = body;
        } catch {
            this._statusBody.text = plain(body);
        }
        this._statusBody.visible = Boolean(body);
        this._stop.visible = working;
        this._status.visible = Boolean(title || body);
    }

    // --- what arceed says ------------------------------------------------------------

    _onListening(on) {
        this._listening = on;
        this._wave.visible = on;
        this._levels.fill(0);
        this._entry.hint_text = on ? 'Listening… (press the mic to finish)' : 'Ask Arcee…';
        // The cue stays after the voice stops: it follows the request to its reply.
        if (on) {
            this._spoken = true;
            this._cue.show();
        } else {
            this._cue.transcribing();
        }
        if (on)
            this._mic.add_style_class_name('listening');
        else
            this._mic.remove_style_class_name('listening');
    }

    _onLevel(loudness) {
        this._cue.level(loudness);
        this._levels.shift();
        this._levels.push(Math.min(1, Math.sqrt(loudness) * 2.5));
        this._bars.forEach((bar, i) => {
            bar.height = Math.max(3, Math.round(this._levels[i] * WAVE_HEIGHT));
        });
    }

    _onHeard(json) {
        // arceed runs what it heard; the panel shows only a failure, the cue both.
        const heard = JSON.parse(json);
        if (heard.error) {
            this._spoken = false;
            this._cue.fail(heard.error);
            this._say('', heard.error);
        } else {
            this._cue.heard(heard.text ?? '');
        }
    }

    _onStarted(request) {
        this._progress = [];
        this._step = '';
        this._say(request, 'Working…', true);
        this._request = request;
        // A request made while the pill shows (typed too) is the conversation's next turn.
        this._spoken ||= this._cue.visible;
        if (this._spoken) {
            this._cue.heard(request);
            this._cue.working(true);
        }
    }

    _onProgress(line) {
        this._progress.push(shorten(line, 80));
        this._showProgress();
    }

    _onStepChanged(doing) {
        this._step = doing;
        this._onStep(doing);
        this._showProgress();
        if (this._spoken)
            this._cue.doing(doing);
    }

    _showProgress() {
        const lines = this._progress.slice(-PROGRESS_LINES);
        this._say(this._statusTitle.text, [this._step ? `${this._step}` : '', ...lines]
            .filter(Boolean).join('\n'), true);
    }

    _onQuestion(json) {
        const question = JSON.parse(json);
        this._questionBox.destroy_all_children();
        const text = new St.Label({text: question.text, style_class: 'arcee-card-title'});
        text.clutter_text.line_wrap = true;
        this._questionBox.add_child(text);
        for (const target of question.preview ?? [])
            this._questionBox.add_child(new St.Label({text: shorten(target, 80), style_class: 'arcee-card-body'}));
        const buttons = new St.BoxLayout({style_class: 'arcee-buttons'});
        const answer = value => {
            this._question.visible = false;
            this._agent.AnswerAsync(question.id, value).catch(logError);
        };
        if (question.kind === 'confirm') {
            for (const [label, value, style] of [['Allow', 'yes', 'arcee-primary'], ['Deny', 'no', '']]) {
                const button = new St.Button({style_class: `arcee-button ${style}`, label});
                button.connect('clicked', () => answer(value));
                buttons.add_child(button);
            }
        } else if (question.options?.length) {
            for (const option of question.options) {
                const button = new St.Button({style_class: 'arcee-button', label: option});
                button.connect('clicked', () => answer(option));
                buttons.add_child(button);
            }
        } else {
            const entry = new St.Entry({style_class: 'arcee-entry', hint_text: 'Your answer', x_expand: true});
            entry.clutter_text.connect('activate', () => answer(entry.get_text()));
            buttons.add_child(entry);
        }
        this._questionBox.add_child(buttons);
        this._question.visible = true;
        if (this._spoken)
            this._cue.doing(`Asks: ${question.text}`);
        // The app shows the question too; open the menu only when the app is not in front.
        const focused = global.display.focus_window;
        if (focused?.get_gtk_application_id?.() !== APP_ID)
            this.menu.open();
    }

    _onFinished(json) {
        const done = JSON.parse(json);
        this._question.visible = false;
        this._step = '';
        this._onStep('');
        const reply = done.reply || (done.status === 'stopped' ? 'Stopped.' : `Ended: ${done.status}.`);
        this._say(this._statusTitle.text, reply.length > 600 ? `${reply.slice(0, 599)}…` : reply, false, true);
        // The reply goes on the pill, typed or spoken, until it has been read; not when
        // the menu or the app is in front showing it already. No notification banner.
        const appInFront = global.display.focus_window?.get_gtk_application_id?.() === APP_ID;
        if (done.status === 'stopped') {
            if (this._spoken)
                this._cue.stopped();
        } else if (this._spoken || !(this.menu.isOpen || appInFront)) {
            this._cue.reply(reply, this._spoken ? '' : this._request);
        } else {
            this._cue.hide();
        }
        this._spoken = false;
    }

    // GNOME's light style draws menus light; the panel's colours were made for a dark one.
    // Read the menu's own background (whatever theme or setting chose it) and match it.
    _matchMenuShade() {
        let light = false;
        try {
            const c = this.menu.box.get_theme_node().get_background_color();
            light = 0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue > 140;
        } catch (e) {
            logError(e, 'Arcee: could not read the menu colour');
        }
        if (light)
            this.menu.actor.add_style_class_name('arcee-light');
        else
            this.menu.actor.remove_style_class_name('arcee-light');
    }

    // --- recent and budget -------------------------------------------------------------

    _refresh() {
        this._agent.RecentAsync(RECENT_TASKS).then(([json]) => {
            const menu = this._recent.menu;
            menu.removeAll();
            for (const task of JSON.parse(json)) {
                const mark = task.status === 'done' ? '✓' : '·';
                const item = new PopupMenu.PopupMenuItem(`${mark}  ${shorten(task.request, 40)}`);
                item.connect('activate', () => this._say(task.request, shorten(task.reply || task.status, 400)));
                menu.addMenuItem(item);
            }
            const more = new PopupMenu.PopupMenuItem('More in Arcee…');
            more.connect('activate', () => this._showApp('history'));
            menu.addMenuItem(more);
        }).catch(() => {});
        this._checkBudget();
    }

    _checkBudget() {
        this._agent.LimitsAsync().then(([json]) => this._showBudget(JSON.parse(json)))
            .catch(() => this._showBudget(null));
    }

    _showBudget(limits) {
        this._budget.destroy_all_children();
        // Today, and a button to read the counts again (another run may have used some).
        const header = new St.BoxLayout({x_expand: true});
        header.add_child(new St.Label({text: 'Today', style_class: 'arcee-budget-title',
            x_expand: true, y_align: Clutter.ActorAlign.CENTER}));
        const reload = new St.Button({style_class: 'arcee-icon-button', can_focus: true,
            accessible_name: 'Check now',
            child: new St.Icon({icon_name: 'view-refresh-symbolic', icon_size: 14})});
        reload.connect('clicked', () => this._checkBudget());
        header.add_child(reload);
        this._budget.add_child(header);
        const models = Object.entries(limits ?? {}).filter(([, l]) => l.day_tokens_limit);
        if (!models.length) {
            this._budget.add_child(new St.Label({style_class: 'arcee-card-body',
                text: limits ? 'No model use yet today' : 'Arcee is not running'}));
            return;
        }
        for (const [model, l] of models) {
            const fraction = Math.max(0, Math.min(1, l.day_tokens_left / l.day_tokens_limit));
            const row = new St.BoxLayout({style_class: 'arcee-budget-row', x_expand: true});
            row.add_child(new St.Label({text: model.split('/').pop(), x_expand: true,
                style_class: 'arcee-budget-name', y_align: Clutter.ActorAlign.CENTER}));
            const track = new St.Widget({style_class: 'arcee-bar-track', width: BUDGET_BAR_PX,
                y_align: Clutter.ActorAlign.CENTER});
            const fill = new St.Widget({style_class: fraction < 0.2 ? 'arcee-bar-fill low' : 'arcee-bar-fill',
                width: Math.round(BUDGET_BAR_PX * fraction)});
            track.add_child(fill);
            row.add_child(track);
            row.add_child(new St.Label({text: `${thousands(l.day_tokens_left)}`,
                style_class: 'arcee-budget-left', y_align: Clutter.ActorAlign.CENTER}));
            this._budget.add_child(row);
        }
    }
});
