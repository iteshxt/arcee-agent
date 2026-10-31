# 0009. Screen and input through the shell extension; the screen read as text

Date: 2026-10-31
Status: accepted

## Context

Phase 3 of the spec reads the screen through the ScreenCast portal and PipeWire and
types and clicks through the RemoteDesktop portal, with `ydotool` as a fallback. On
the reference machine:

- `ydotool` needs a udev rule and the `input` group, so sudo, and the portals ask the
  user to approve a session in a dialog. Neither can be set up or tested unattended.
- The Arcee GNOME Shell extension (decision 0007's companion for window focus) already
  runs inside the compositor. There `Shell.Screenshot` captures the whole screen and
  Clutter virtual devices click, scroll and type, with no prompt and no extra rights.
- The default Core models (gpt-oss on Groq, decision 0008) read no images, so a
  screenshot by itself is of no use to them.

## Decision

1. The extension exposes `Screenshot`, `PointerClick`, `Scroll`, `KeyCombo`,
   `TypeText` and `ReleaseAll` on `org.arcee.Shell`. Each input call presses and
   releases within the call, so nothing stays held between calls. A shortcut,
   Super+Shift+Escape, releases everything and emits `Stopped`, and `arcee-cli run`
   cancels the task on that signal.
2. The screen is read as text: the screenshot is greyscaled, doubled and read by
   Tesseract in sparse-text mode, and each line is numbered `t_N` with its centre in
   screen coordinates (set-of-marks as text). `screen.click` clicks a `t_N` or a
   point; `kbd.type` and `kbd.key` type into the focused window.
3. Clicking a bare point, or text such as delete, send or pay, and pressing Enter,
   Delete or a window-closing shortcut need the user's confirmation. Typing takes one
   line at a time, so text alone never presses Enter.
4. The portals and `ydotool` stay as the route for sessions without the extension.

Measured in a headless GNOME Shell 50 with its own home and session bus: OCR at the
screen's own size missed a dialog's "Type here", "Cancel" and "OK", and doubled it
read all of them in 0.5 s. Through the Python tools Arcee read the dialog, typed
"hello arcee", clicked OK by its `t_N` and the dialog returned that text, and the
stop shortcut reached the watcher. A virtual keyboard made just before its first key
lost those keys, so devices are made when the extension is enabled. A move from the
top-left corner pressed on the hot corner's barriers, lost an axis and opened the
overview, so moves are checked and retried, and an overview they open is closed.

## Consequences

- Desktop apps whose window tree is thin (canvases, games, remote desktops) can now
  be read and driven, by text models.
- Any program in the user's session can call these methods on the session bus, as it
  could the focus methods. Sandboxed apps cannot reach the name. Caller checks on the
  input methods stay a phase 4 item.
- OCR is only as good as the text on screen: icons without labels cannot be named,
  only clicked by point, with confirmation. A vision model would add level 3 proper
  (marks drawn on the image) when one is the Core.
- Moving the pointer and typing act on the user's live desktop; the stop shortcut is
  the way out, and the indicator in phase 4 should show when Arcee is driving input.
