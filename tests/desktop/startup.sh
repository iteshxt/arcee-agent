#!/bin/sh
# Does the app start, and keep answering, whatever it finds? Runs a headless GNOME Shell
# in its own D-Bus session and starts the app under each case with a fresh HOME: a
# first run, corrupt settings, rate limits, speech usage and history, a data folder it
# cannot write, every page opened directly, and the .deb's files on the system Python.
# One line per case: how soon the window showed, how soon the app answered on D-Bus
# (twice), whether it is still running, and errors in its output or crash log.
#
#   sh packaging/build-deb.sh   # for the packaged case
#   sh tests/desktop/startup.sh
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
T=$(mktemp -d)
mkdir -p "$T/robust"
export T REPO
# A short runtime dir of our own: Wayland socket paths are limited to 108 bytes.
export XDG_RUNTIME_DIR="/run/user/$(id -u)/arcee-startup"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
export HOME="$T/home"
mkdir -p "$HOME"
exec dbus-run-session -- bash -c "$(sed -n '/^# --- in the session ---$/,$p' "$0")"
# --- in the session ---
gnome-shell --headless --virtual-monitor 1280x900 --wayland-display arcee-startup --no-x11 --unsafe-mode > $T/robust/shell.log 2>&1 &
SHELL_PID=$!
trap 'kill $SHELL_PID 2>/dev/null; wait $SHELL_PID 2>/dev/null' EXIT  # never leave it behind
for i in $(seq 1 40); do gdbus introspect --session --dest org.gnome.Shell --object-path /org/gnome/Shell >/dev/null 2>&1 && break; sleep 0.5; done
sleep 2
PKG=$REPO/build/deb
ping_app() { local t0=$(date +%s%N); timeout 3 gdbus call --session --dest me.iteshxt.Arcee --object-path /me/iteshxt/Arcee --method org.gtk.Actions.List >/dev/null 2>&1 && echo $(( ($(date +%s%N)-t0)/1000000 ))ms || echo NO-ANSWER; }
window_up() { for i in $(seq 1 100); do gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "global.display.list_all_windows().some(w=>w.get_wm_class()=='me.iteshxt.Arcee' || w.title=='Arcee')" 2>/dev/null | grep -q "'true'" && { echo $((i*100))ms; return; }; sleep 0.1; done; echo NO-WINDOW; }
case_run() {  # name, setup function, command
  local name=$1 setup=$2; shift 2
  export HOME=$T/robust/home-$name; rm -rf $HOME; mkdir -p $HOME/.config/arcee $HOME/.local/share/arcee
  $setup
  cd /tmp; WAYLAND_DISPLAY=arcee-startup GDK_BACKEND=wayland "$@" > $T/robust/$name.log 2>&1 &
  local app=$!; cd - >/dev/null
  local w=$(window_up); local p1=$(ping_app); sleep 4; local p2=$(ping_app)
  local alive=$(kill -0 $app 2>/dev/null && echo alive || echo DIED)
  local errs=$(grep -ciE "traceback|error|critical|segfault" $T/robust/$name.log)
  local crash=$(cat $HOME/.local/share/arcee/logs/app-crash.log 2>/dev/null | wc -l)
  echo "$name: window $w, answers $p1 then $p2, $alive, log errors $errs, crash lines $crash"
  kill $app 2>/dev/null; wait $app 2>/dev/null
  # One instance per session: wait until this one has left the bus.
  for i in $(seq 1 50); do gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.NameHasOwner me.iteshxt.Arcee 2>/dev/null | grep -q true || break; sleep 0.1; done
}
setup_fresh() { rm -rf $HOME/.config/arcee $HOME/.local/share/arcee; }
setup_done() { echo '{"setup_done": true}' > $HOME/.config/arcee/settings.json; }
setup_corrupt() {
  head -c 300 /dev/urandom > $HOME/.config/arcee/settings.json
  head -c 300 /dev/urandom > $HOME/.local/share/arcee/rate-limits.json
  head -c 300 /dev/urandom > $HOME/.local/share/arcee/speech-usage.json
  head -c 4096 /dev/urandom > $HOME/.local/share/arcee/history.db
}
setup_readonly() { setup_done; chmod 500 $HOME/.local/share/arcee; }
DEV="$REPO/.venv/bin/arcee-app"
case_run fresh setup_fresh $DEV
case_run corrupt setup_corrupt $DEV
case_run readonly setup_readonly $DEV
for page in agent models history memory permissions diagnostics; do case_run page-$page setup_done $DEV --page=$page; done
case_run packaged setup_fresh /usr/bin/python3 -I -c "import sys; sys.path[:0]=['$PKG/usr/lib/arcee','$PKG/usr/lib/arcee/vendor']; sys.argv=['arcee-app']; from arcee.app.window import main; main()"
chmod -R u+w $T/robust 2>/dev/null
