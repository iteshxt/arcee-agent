#!/bin/sh
# Arranging windows and reading notifications through the extension, in a headless
# GNOME Shell with two app windows and one notification.
#
#
#   sh tests/desktop/arrange.sh
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
T=$(mktemp -d)
mkdir -p "$T/home/.local/share/gnome-shell/extensions"
ln -s "$REPO/extension/arcee@iteshxt.me" "$T/home/.local/share/gnome-shell/extensions/"
# A short runtime dir of our own: Wayland socket paths are limited to 108 bytes.
R="/run/user/$(id -u)/arcee-arrange"
rm -rf "$R"; mkdir -m 700 "$R"
# The document portal may still have its folder mounted in $R: cleaning up never fails the test.
trap 'fusermount3 -uz "$R/doc" 2>/dev/null; rm -rf "$T" "$R" 2>/dev/null || true' EXIT
env -i HOME="$T/home" XDG_RUNTIME_DIR="$R" PATH=/usr/bin:/bin LANG=C.UTF-8 REPO="$REPO" T="$T" \
  dbus-run-session -- sh -c '
    gsettings set org.gnome.shell enabled-extensions "[\"arcee@iteshxt.me\"]"
    gsettings set org.gnome.shell welcome-dialog-last-shown-version 99
    gnome-shell --headless --unsafe-mode --virtual-monitor 1280x800 --wayland-display arcee-arrange --no-x11 > "$T/shell.log" 2>&1 &
    SP=$!
    for i in $(seq 1 60); do gdbus introspect --session --dest me.iteshxt.Arcee.Shell --object-path /me/iteshxt/Arcee/Shell >/dev/null 2>&1 && break; sleep 0.5; done
    WAYLAND_DISPLAY=arcee-arrange GDK_BACKEND=wayland gnome-text-editor --standalone >/dev/null 2>&1 &
    WAYLAND_DISPLAY=arcee-arrange GDK_BACKEND=wayland gnome-calculator >/dev/null 2>&1 &
    notify-send -a "Chat" "Lunch?" "Rahul asks" 2>/dev/null || gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications --method org.freedesktop.Notifications.Notify Chat 0 "" "Lunch?" "Rahul asks" [] {} 5000 >/dev/null
    sleep 5
    "$REPO/.venv/bin/python" "$REPO/tests/desktop/arrange.py"; echo $? > "$T/status"
    kill $SP; wait $SP 2>/dev/null
  ' 2>"$T/bus.log" || true  # dbus-run-session reports its own teardown, not the test
exit "$(cat "$T/status" 2>/dev/null || echo 1)"
