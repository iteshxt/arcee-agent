#!/bin/sh
# Builds what a GitHub release carries, in dist/:
#   arcee_<version>_amd64.deb          everything, for Ubuntu 26.04 (packaging/build-deb.sh)
#   arcee-gnome-extension-<version>.zip  the shell extension alone, for
#                                        gnome-extensions install
#   arcee-chrome-extension-<version>.zip the Chrome extension, to unzip and Load unpacked
# and the release notes, dist/release-notes.md.
#
#   sh packaging/build-release.sh
#   sh packaging/publish-release.sh   (needs the gh CLI, logged in)
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
EXT=arcee@iteshxt.me
mkdir -p dist build

sh packaging/build-deb.sh

# The shell extension, packed the way extensions.gnome.org expects; the schema is
# compiled by gnome-extensions install.
rm -f "dist/arcee-gnome-extension-$VERSION.zip"
for js in "extension/$EXT"/*.js; do
    case "$(basename "$js")" in extension.js) ;; *) set -- "$@" "--extra-source=$(basename "$js")" ;; esac
done
gnome-extensions pack "extension/$EXT" --force --out-dir build \
    --extra-source=icons "$@"
mv "build/$EXT.shell-extension.zip" "dist/arcee-gnome-extension-$VERSION.zip"

# The Chrome extension: unzip, then chrome://extensions, Developer mode, Load unpacked.
rm -f "dist/arcee-chrome-extension-$VERSION.zip"
(cd browser-ext && zip -q -r "../dist/arcee-chrome-extension-$VERSION.zip" .)

CHROME_VERSION=$(sed -n 's/.*"version": "\(.*\)".*/\1/p' browser-ext/manifest.json)
cat > dist/release-notes.md <<EOF
A test build of Arcee $VERSION for Ubuntu 26.04 (GNOME 50, Wayland). Not a
stable release.

## Install

1. Install the package. It brings the app, the arceed service, the GNOME Shell
   extension and the Chrome helper, for every user on the machine:

       sudo apt install ./arcee_${VERSION}_amd64.deb

2. Log out and back in (GNOME loads new extensions at login), open **Arcee**
   from the app grid and follow its setup. In **Diagnostics**, press **Fix** on
   anything not green: it turns the GNOME extension on and shows how to load the
   Chrome extension.
3. Chrome: open \`chrome://extensions\`, turn on **Developer mode**, press
   **Load unpacked** and choose \`/usr/share/arcee/browser-ext\` (installed by
   the package).

Arcee uses Groq for its models by default: get a free key at
https://console.groq.com/keys and enter it in setup. Or choose **Local
(Ollama)** on the Models page to keep everything on your computer.

## The other files

- \`arcee-gnome-extension-$VERSION.zip\`: the shell extension on its own
  (\`gnome-extensions install arcee-gnome-extension-$VERSION.zip\`). The .deb
  already includes it.
- \`arcee-chrome-extension-$VERSION.zip\` (extension $CHROME_VERSION): unzip it
  and use **Load unpacked** on the folder, if you run Arcee from a checkout
  instead of the .deb.

Super+Shift+Escape stops Arcee at any time.
EOF

# What changed, from packaging/changes/<version>.md when there is one.
if [ -f "packaging/changes/$VERSION.md" ]; then
    { echo; cat "packaging/changes/$VERSION.md"; } >> dist/release-notes.md
fi

ls -l dist
