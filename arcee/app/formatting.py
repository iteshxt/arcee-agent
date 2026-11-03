# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""Arcee's replies in the app: the models' Markdown, drawn as GTK markup."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from arcee.markdown import plain, to_pango  # noqa: E402


def show_markdown(label: Gtk.Label, text: str) -> None:
    """Put `text` (Markdown) on `label`, formatted; as plain text if the markup fails."""
    try:
        label.set_markup(to_pango(text))
    except GLib.Error:
        label.set_text(plain(text))
