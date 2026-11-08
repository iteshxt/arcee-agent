#!/bin/sh
# Builds the local dev .deb (phase 4): dist/arcee_<version>_amd64.deb.
#
# No debhelper and no sudo: the tree is staged under build/deb and packed with
# dpkg-deb. Arcee runs on the system Python (3.14, for PyGObject and pyatspi from
# apt; decision 0001) with its own Python dependencies vendored in
# /usr/lib/arcee/vendor at the versions uv.lock pins. Every user gets the shell
# extension, the arceed user service, D-Bus activation, the app launcher and the
# Chrome host; each user still turns the extension on and loads the Chrome extension.
#
#   sh packaging/build-deb.sh
#   sudo apt install ./dist/arcee_<version>_amd64.deb
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
STAGE=build/deb
LIB=$STAGE/usr/lib/arcee
EXT=arcee@iteshxt.me

rm -rf "$STAGE"
mkdir -p "$LIB" "$STAGE/DEBIAN" "$STAGE/usr/bin" dist

# The arcee package (no tests, no caches) and its dependencies.
tar --exclude=__pycache__ -cf - arcee | tar -xf - -C "$LIB"
uv export --frozen --no-dev --no-hashes --no-emit-project --quiet > build/requirements.txt
uv pip install --quiet --python python3.14 --only-binary :all: \
    --target "$LIB/vendor" -r build/requirements.txt
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
# arcee.__version__ comes from package metadata, as in a uv install.
mkdir -p "$LIB/vendor/arcee-$VERSION.dist-info"
printf 'Metadata-Version: 2.1\nName: arcee\nVersion: %s\n' "$VERSION" \
    > "$LIB/vendor/arcee-$VERSION.dist-info/METADATA"

for pair in arcee-cli:arcee.cli arceed:arcee.daemon arcee-app:arcee.app.window; do
    name=${pair%%:*}
    module=${pair#*:}
    cat > "$STAGE/usr/bin/$name" <<EOF
#!/usr/bin/python3
import sys

sys.path[:0] = ["/usr/lib/arcee", "/usr/lib/arcee/vendor"]
from $module import main

sys.exit(main())
EOF
    chmod 755 "$STAGE/usr/bin/$name"
done

# Chrome's native messaging host, registered for every user.
cat > "$LIB/browser-host" <<'EOF'
#!/usr/bin/python3
import asyncio
import sys

sys.path[:0] = ["/usr/lib/arcee", "/usr/lib/arcee/vendor"]
from arcee.browser.host import main

asyncio.run(main())
EOF
chmod 755 "$LIB/browser-host"
EXTENSION_ID=$(sed -n 's/^EXTENSION_ID: Final = "\(.*\)"/\1/p' arcee/browser/host.py)
for hosts in etc/opt/chrome/native-messaging-hosts etc/chromium/native-messaging-hosts; do
    mkdir -p "$STAGE/$hosts"
    cat > "$STAGE/$hosts/me.iteshxt.arcee.browser.json" <<EOF
{
  "name": "me.iteshxt.arcee.browser",
  "description": "Arcee browser bridge",
  "path": "/usr/lib/arcee/browser-host",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXTENSION_ID/"
  ]
}
EOF
done
mkdir -p "$STAGE/usr/share/arcee"
cp -r browser-ext "$STAGE/usr/share/arcee/browser-ext"

# The GNOME Shell extension, its settings schema compiled.
mkdir -p "$STAGE/usr/share/gnome-shell/extensions"
cp -r "extension/$EXT" "$STAGE/usr/share/gnome-shell/extensions/$EXT"
rm -f "$STAGE/usr/share/gnome-shell/extensions/$EXT/schemas/gschemas.compiled"
glib-compile-schemas --strict "$STAGE/usr/share/gnome-shell/extensions/$EXT/schemas"

# The user service, D-Bus activation, the launcher and its icon.
mkdir -p "$STAGE/usr/lib/systemd/user" "$STAGE/usr/share/dbus-1/services" \
    "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps"
cat > "$STAGE/usr/lib/systemd/user/arceed.service" <<'EOF'
[Unit]
Description=Arcee agent
PartOf=graphical-session.target

[Service]
Type=dbus
BusName=me.iteshxt.Arcee.Agent
ExecStart=/usr/bin/arceed
TimeoutStopSec=2
Restart=on-failure
EOF
# At login, arceed starts when "Listen for Hey Arcee" is on (wanted by every user's
# graphical session; with listening off it starts nothing).
mkdir -p "$STAGE/usr/lib/systemd/user/graphical-session.target.wants"
uv run python -c 'from arcee.install import listen_unit; print(listen_unit("/usr/bin/env PYTHONPATH=/usr/lib/arcee:/usr/lib/arcee/vendor /usr/bin/python3 -m arcee.settings listening"), end="")' \
    > "$STAGE/usr/lib/systemd/user/arceed-listen.service"
ln -s ../arceed-listen.service "$STAGE/usr/lib/systemd/user/graphical-session.target.wants/arceed-listen.service"
cat > "$STAGE/usr/share/dbus-1/services/me.iteshxt.Arcee.Agent.service" <<'EOF'
[D-BUS Service]
Name=me.iteshxt.Arcee.Agent
Exec=/usr/bin/arceed
SystemdService=arceed.service
EOF
cat > "$STAGE/usr/share/applications/me.iteshxt.Arcee.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Arcee
Comment=An agent for your desktop, by Itesh Tomar
Exec=/usr/bin/arcee-app
Icon=me.iteshxt.Arcee
Categories=Utility;
StartupWMClass=me.iteshxt.Arcee
EOF
# The icon (tools/make_art.py): the particle ring at 64 px and up, the simpler vector
# ring below that and as the scalable fallback.
cp art/arcee.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/me.iteshxt.Arcee.svg"
for px in 64 128 256 512; do
    mkdir -p "$STAGE/usr/share/icons/hicolor/${px}x${px}/apps"
    cp "art/arcee-$px.png" "$STAGE/usr/share/icons/hicolor/${px}x${px}/apps/me.iteshxt.Arcee.png"
done
for px in 16 32 48; do
    mkdir -p "$STAGE/usr/share/icons/hicolor/${px}x${px}/apps"
    cp "browser-ext/icons/icon-$px.png" "$STAGE/usr/share/icons/hicolor/${px}x${px}/apps/me.iteshxt.Arcee.png"
done

mkdir -p "$STAGE/usr/share/doc/arcee"
cp README.md "$STAGE/usr/share/doc/arcee/"

# The voice models (decision 0019): the wake word's, and Arcee's voice, so an installed
# Arcee downloads nothing to listen or answer aloud. Fetched once into build/bundle.
uv run python -m arcee.bundle build/bundle/models "$STAGE/usr/share/arcee/models"
cat > "$STAGE/usr/share/doc/arcee/MODELS" <<'EOF'
Models in /usr/share/arcee/models, as their makers publish them:

wake     sherpa-onnx keyword spotter (zipformer gigaspeech 3.3M), Apache-2.0,
         https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models
vad      Silero VAD, MIT, https://github.com/snakers4/silero-vad
denoise  GTCRN, MIT, https://github.com/Xiaobin-Rong/gtcrn
voice    Kitten TTS nano 0.8 (fp32), Apache-2.0, by KittenML,
         https://huggingface.co/KittenML/kitten-tts-nano-0.8-fp32
         (its voices as voices.bin and sherpa-onnx's metadata added; the weights as published)
EOF

SIZE=$(du -sk "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: arcee
Version: $VERSION
Architecture: amd64
Maintainer: Itesh Tomar <petrioteer007@gmail.com>
Homepage: https://iteshxt.me
Installed-Size: $SIZE
Depends: python3 (>= 3.14), python3 (<< 3.15), python3-gi, python3-pyatspi,
 gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-secret-1, gir1.2-atspi-2.0, gir1.2-pango-1.0,
 gnome-shell (>= 50), tesseract-ocr, pipewire-bin, libglib2.0-bin, libespeak-ng1
Recommends: google-chrome-stable | chromium
Section: utils
Priority: optional
Description: desktop agent for GNOME on Wayland
 Arcee does what you ask on your desktop: it opens apps and files, works in
 Chrome, reads the screen and clicks for you, inside a visible control session
 you can stop at any time (Super+Shift+Escape). By Itesh Tomar (iteshxt.me).
EOF

dpkg-deb --root-owner-group --build "$STAGE" "dist/arcee_${VERSION}_amd64.deb"
