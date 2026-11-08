#!/bin/sh
# Does the user's own input pause Arcee's control of the screen, over an app window too,
# while Arcee's own input does not? Runs a headless GNOME Shell with the extension from
# this checkout, an app window in front, and tests/desktop/control_pause.py.
#
#   sh tests/desktop/control_pause.sh
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
T=$(mktemp -d)
mkdir -p "$T/home/.local/share/gnome-shell/extensions"
ln -s "$REPO/extension/arcee@iteshxt.me" "$T/home/.local/share/gnome-shell/extensions/"
# A short runtime dir of our own: Wayland socket paths are limited to 108 bytes.
R="/run/user/$(id -u)/arcee-pause"
rm -rf "$R"; mkdir -m 700 "$R"
# The document portal may still have its folder mounted in $R: cleaning up never fails the test.
trap 'fusermount3 -uz "$R/doc" 2>/dev/null; rm -rf "$T" "$R" 2>/dev/null || true' EXIT
env -i HOME="$T/home" XDG_RUNTIME_DIR="$R" PATH=/usr/bin:/bin LANG=C.UTF-8 REPO="$REPO" T="$T" \
  dbus-run-session -- sh -c '
    gsettings set org.gnome.shell enabled-extensions "[\"arcee@iteshxt.me\"]"
    gsettings set org.gnome.shell welcome-dialog-last-shown-version 99
    gnome-shell --headless --unsafe-mode --virtual-monitor 1280x800 --wayland-display arcee-pause --no-x11 > "$T/shell.log" 2>&1 &
    SP=$!
    for i in $(seq 1 60); do gdbus introspect --session --dest me.iteshxt.Arcee.Shell --object-path /me/iteshxt/Arcee/Shell >/dev/null 2>&1 && break; sleep 0.5; done
    WAYLAND_DISPLAY=arcee-pause GDK_BACKEND=wayland zenity --entry --title "An app" --text "Type here" >/dev/null 2>&1 &
    sleep 3
    "$REPO/.venv/bin/python" "$REPO/tests/desktop/control_pause.py"; echo $? > "$T/status"
    kill $SP; wait $SP 2>/dev/null
  ' || true  # dbus-run-session reports its own teardown, not the test
exit "$(cat "$T/status" 2>/dev/null || echo 1)"
