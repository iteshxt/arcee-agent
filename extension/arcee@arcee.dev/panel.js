// Arcee's icon in the top bar and its menu (phase 4): ask Arcee something, follow what
// it is doing and stop it, answer its questions and confirmations, see recent tasks
// and today's model budget. It talks to arceed (org.arcee.Agent), which D-Bus starts
// on the first request.

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
  <interface name="org.arcee.Agent">
    <method name="Run"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="Cancel"/>
    <method name="Quit"/>
    <method name="Answer"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Status"><arg type="s" direction="out"/></method>
    <method name="Recent"><arg type="u" direction="in"/><arg type="s" direction="out"/></method>
    <method name="Limits"><arg type="s" direction="out"/></method>
    <signal name="Started"><arg type="s"/></signal>
    <signal name="Progress"><arg type="s"/></signal>
    <signal name="Question"><arg type="s"/></signal>
    <signal name="Finished"><arg type="s"/></signal>
  </interface>
</node>`;
const AgentProxy = Gio.DBusProxy.makeProxyWrapper(AGENT_INTERFACE);

const PROGRESS_LINES = 4;
const RECENT_TASKS = 5;

function shorten(text, length) {
    const line = (text ?? '').replace(/\s+/g, ' ').trim();
    return line.length > length ? `${line.slice(0, length - 1)}…` : line;
}

function thousands(n) {
    return n >= 1000 ? `${Math.round(n / 1000)}K` : `${n}`;
}

export const ArceeIndicator = GObject.registerClass(
class ArceeIndicator extends PanelMenu.Button {
    _init(path) {
        super._init(0.5, 'Arcee', false);
        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${path}/icons/arcee-symbolic.svg`),
            style_class: 'system-status-icon',
        }));
        this._progress = [];
        this._buildMenu();
        this._agent = new AgentProxy(
            Gio.DBus.session, 'org.arcee.Agent', '/org/arcee/Agent', null, null,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION);
        this._signals = [
            this._agent.connectSignal('Started', (_p, _s, [request]) => this._onStarted(request)),
            this._agent.connectSignal('Progress', (_p, _s, [line]) => this._onProgress(line)),
            this._agent.connectSignal('Question', (_p, _s, [json]) => this._onQuestion(json)),
            this._agent.connectSignal('Finished', (_p, _s, [json]) => this._onFinished(json)),
        ];
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (!open)
                return;
            this._refresh();
            // Open the menu and type: the field takes the keyboard (unless a question waits).
            if (!this._question.visible)
                GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                    this._entry.grab_key_focus();
                    return GLib.SOURCE_REMOVE;
                });
        });
    }

    destroy() {
        for (const id of this._signals ?? [])
            this._agent.disconnectSignal(id);
        super.destroy();
    }

    _buildMenu() {
        // Ask: a text field and the mic.
        const ask = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._entry = new St.Entry({
            style_class: 'arcee-entry', hint_text: 'Ask Arcee…', can_focus: true, x_expand: true,
        });
        this._entry.clutter_text.connect('activate', () => this._run());
        // Voice input comes with the next step; the button waits for it.
        this._mic = new St.Button({
            style_class: 'arcee-round-button', can_focus: true, visible: false,
            child: new St.Icon({icon_name: 'audio-input-microphone-symbolic', icon_size: 16}),
        });
        ask.add_child(this._entry);
        ask.add_child(this._mic);
        this.menu.addMenuItem(ask);

        // What Arcee is doing, or its last reply.
        const status = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style_class: 'arcee-status'});
        this._statusLabel = new St.Label({text: 'Idle', style_class: 'arcee-status-title'});
        this._statusLabel.clutter_text.line_wrap = true;
        this._progressLabel = new St.Label({text: '', style_class: 'arcee-progress'});
        this._progressLabel.clutter_text.line_wrap = true;
        this._stop = new St.Button({
            style_class: 'arcee-pill-button arcee-stop', label: 'Stop', visible: false,
            x_align: Clutter.ActorAlign.START,
        });
        this._stop.connect('clicked', () => this._agent.CancelAsync().catch(logError));
        box.add_child(this._statusLabel);
        box.add_child(this._progressLabel);
        box.add_child(this._stop);
        status.add_child(box);
        this.menu.addMenuItem(status);

        // A question or a confirmation, shown only while one is waiting.
        this._question = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._questionBox = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style_class: 'arcee-question'});
        this._question.add_child(this._questionBox);
        this._question.visible = false;
        this.menu.addMenuItem(this._question);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Recent'));
        this._recent = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._recent);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Today'));
        this._budget = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._budget);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Quit Arcee', () => this._agent.QuitAsync().catch(() => {}));
    }

    // Typed or spoken: start a request.
    run(text) {
        this._entry.set_text(text);
        this._run();
    }

    _run() {
        const text = this._entry.get_text().trim();
        if (!text)
            return;
        this._entry.set_text('');
        this._agent.RunAsync(text).catch(error => {
            this._statusLabel.text = shorten(Gio.DBusError.strip_remote_error(error) ??
                error.message, 160);
        });
    }

    _onStarted(request) {
        this._progress = [];
        this._statusLabel.text = shorten(request, 160);
        this._progressLabel.text = 'Working…';
        this._stop.visible = true;
    }

    _onProgress(line) {
        this._progress.push(shorten(line, 90));
        this._progressLabel.text = this._progress.slice(-PROGRESS_LINES).join('\n');
    }

    _onQuestion(json) {
        const question = JSON.parse(json);
        this._questionBox.destroy_all_children();
        const text = new St.Label({text: question.text, style_class: 'arcee-question-text'});
        text.clutter_text.line_wrap = true;
        this._questionBox.add_child(text);
        for (const target of question.preview ?? [])
            this._questionBox.add_child(new St.Label({text: `  ${shorten(target, 80)}`}));

        const buttons = new St.BoxLayout({style_class: 'arcee-question-buttons'});
        const answer = value => {
            this._question.visible = false;
            this._agent.AnswerAsync(question.id, value).catch(logError);
        };
        if (question.kind === 'confirm') {
            for (const [label, value, style] of [['Allow', 'yes', 'arcee-allow'], ['Deny', 'no', '']]) {
                const button = new St.Button({style_class: `arcee-pill-button ${style}`, label});
                button.connect('clicked', () => answer(value));
                buttons.add_child(button);
            }
        } else if (question.options?.length) {
            for (const option of question.options) {
                const button = new St.Button({style_class: 'arcee-pill-button', label: option});
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
        this.menu.open();
    }

    _onFinished(json) {
        const done = JSON.parse(json);
        this._stop.visible = false;
        this._question.visible = false;
        const reply = done.reply || (done.status === 'stopped' ? 'Stopped.' : done.status);
        this._statusLabel.text = shorten(reply, 300);
        this._progressLabel.text = done.status === 'done' ? '' : done.status;
        if (!this.menu.isOpen)
            Main.notify('Arcee', shorten(reply, 200));
    }

    _refresh() {
        this._agent.RecentAsync(RECENT_TASKS).then(([json]) => {
            this._recent.removeAll();
            for (const task of JSON.parse(json)) {
                const mark = task.status === 'done' ? '✓' : '·';
                const item = new PopupMenu.PopupMenuItem(`${mark} ${shorten(task.request, 48)}`);
                item.connect('activate', () => {
                    this._statusLabel.text = shorten(task.reply || task.status, 300);
                    this._progressLabel.text = '';
                });
                this._recent.addMenuItem(item);
            }
        }).catch(() => {});
        this._agent.LimitsAsync().then(([json]) => {
            const parts = Object.entries(JSON.parse(json))
                .filter(([, l]) => l.day_tokens_limit)
                .map(([model, l]) => `${model.split('/').pop()}: ${thousands(l.day_tokens_left)} / ` +
                    `${thousands(l.day_tokens_limit)} tokens`);
            this._budget.label.text = parts.join('\n') || 'No model use recorded yet';
        }).catch(() => {
            this._budget.label.text = 'arceed is not running (arcee-cli daemon-install)';
        });
    }
});
