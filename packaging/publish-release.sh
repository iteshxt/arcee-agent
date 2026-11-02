#!/bin/sh
# Publishes dist/ (from packaging/build-release.sh) as a GitHub pre-release for the
# tag v<version>, which must already be pushed. Needs the gh CLI, logged in once:
#
#   sudo apt install gh && gh auth login
#   sh packaging/publish-release.sh
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)

gh release create "v$VERSION" \
    --repo iteshxt/arcee-agent \
    --prerelease \
    --title "Arcee $VERSION (test build)" \
    --notes-file dist/release-notes.md \
    "dist/arcee_${VERSION}_amd64.deb" \
    "dist/arcee-gnome-extension-$VERSION.zip" \
    "dist/arcee-chrome-extension-$VERSION.zip"
