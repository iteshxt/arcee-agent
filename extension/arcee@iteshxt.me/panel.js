// Arcee's icon in the top bar and its menu: ask (typed or spoken), follow what Arcee is
// doing and stop it, answer its questions, a few recent requests (more in the app), and
// today's model budget as bars. It talks to arceed (me.iteshxt.Arcee.Agent), which D-Bus
// starts on the first request; what the user says is run by arceed itself.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

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
    <signal name="Started"><arg type="s"/></signal>
    <signal name="Progress"><arg type="s"/></signal>
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
    _init(path) {
        super._init(0.5, 'Arcee', false);
        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${path}/icons/arcee-symbolic.svg`),
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
            this._agent.connectSignal('Question', (_p, _s, [json]) => this._onQuestion(json)),
            this._agent.connectSignal('Finished', (_p, _s, [json]) => this._onFinished(json)),
            this._agent.connectSignal('Listening', (_p, _s, [on]) => this._onListening(on)),
            this._agent.connectSignal('Level', (_p, _s, [loudness]) => this._onLevel(loudness)),
            this._agent.connectSignal('Heard', (_p, _s, [json]) => this._onHeard(json)),
        ];
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (!open)
                return;
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

    destroy() {
        for (const id of this._signals ?? [])
            this._agent.disconnectSignal(id);
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

    _say(title, body, working = false) {
        this._statusTitle.text = shorten(title, 160);
        this._statusTitle.visible = Boolean(title);
        this._statusBody.text = body;
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
        if (on)
            this._mic.add_style_class_name('listening');
        else
            this._mic.remove_style_class_name('listening');
    }

    _onLevel(loudness) {
        this._levels.shift();
        this._levels.push(Math.min(1, Math.sqrt(loudness) * 2.5));
        this._bars.forEach((bar, i) => {
            bar.height = Math.max(3, Math.round(this._levels[i] * WAVE_HEIGHT));
        });
    }

    _onHeard(json) {
        // arceed runs what it heard; only a failure is shown here.
        const heard = JSON.parse(json);
        if (heard.error)
            this._say('', heard.error);
    }

    _onStarted(request) {
        this._progress = [];
        this._say(request, 'Working…', true);
    }

    _onProgress(line) {
        this._progress.push(shorten(line, 80));
        this._say(this._statusTitle.text, this._progress.slice(-PROGRESS_LINES).join('\n'), true);
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
        // The app shows the question too; open the menu only when the app is not in front.
        const focused = global.display.focus_window;
        if (focused?.get_gtk_application_id?.() !== APP_ID)
            this.menu.open();
    }

    _onFinished(json) {
        const done = JSON.parse(json);
        this._question.visible = false;
        const reply = done.reply || (done.status === 'stopped' ? 'Stopped.' : `Ended: ${done.status}.`);
        this._say(this._statusTitle.text, shorten(reply, 400));
        if (!this.menu.isOpen)
            Main.notify('Arcee', shorten(reply, 200));
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
        this._agent.LimitsAsync().then(([json]) => this._showBudget(JSON.parse(json)))
            .catch(() => this._showBudget(null));
    }

    _showBudget(limits) {
        this._budget.destroy_all_children();
        const title = new St.Label({text: 'Today', style_class: 'arcee-budget-title'});
        this._budget.add_child(title);
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
