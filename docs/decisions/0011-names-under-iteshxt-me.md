# 0011. Names under the developer's domain, iteshxt.me

Date: 2026-11-01
Status: accepted

## Context

Arcee's identifiers were placeholders under domains nobody here owns: the GNOME
extension `arcee@arcee.dev`, the app `org.arcee.App`, the D-Bus services `org.arcee.Shell`
and `org.arcee.Agent`, and the Chrome native host `org.arcee.browser`. GNOME, D-Bus and
Chrome expect these to be under a domain the publisher controls. Arcee is made by Itesh
Tomar, whose domain is iteshxt.me.

## Decision

| Part | Was | Now |
| --- | --- | --- |
| GNOME Shell extension | `arcee@arcee.dev` | `arcee@iteshxt.me` |
| App id | `org.arcee.App` | `me.iteshxt.Arcee` |
| Shell bridge | `org.arcee.Shell`, `/org/arcee/Shell` | `me.iteshxt.Arcee.Shell`, `/me/iteshxt/Arcee/Shell` |
| Service | `org.arcee.Agent`, `/org/arcee/Agent` | `me.iteshxt.Arcee.Agent`, `/me/iteshxt/Arcee/Agent` |
| Chrome native host | `org.arcee.browser` | `me.iteshxt.arcee.browser` (Chrome wants lowercase) |

Developer credit (Itesh Tomar, https://iteshxt.me) goes in the extension and Chrome
extension metadata, the package metadata, the README and the app's About dialog. The
extension's settings schema stays `org.gnome.shell.extensions.arcee`, GNOME's own
namespace for extension settings.

## Consequences

- The installers (`arcee/install.py`, used by arcee-cli and the app) remove what an older
  Arcee left under the old names: the extension link, the D-Bus activation file, the app
  launcher and the native host manifest.
- An existing install needs the installers run once, the Chrome extension reloaded, and a
  new login for GNOME to load the extension under its new UUID.
- Earlier decisions keep the old names as they were written.
