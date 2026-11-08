# 0010. Screen and input only inside a visible control session; the user's input pauses Arcee

Date: 2026-10-31
Status: accepted

## Context

Decision 0009 let any program in the user's session take screenshots and drive input
through `org.arcee.Shell`, and nothing on screen showed when Arcee was in control. The
user asked for a glow while Arcee controls the screen, a Stop button at the top centre,
and their own input stopped while Arcee works.

Blocking the user's input outright would lock them out if Arcee hung, and Arcee's own
events travel the same input path. Measured in a headless GNOME Shell 50: Arcee's
synthetic events come from virtual devices with no device node, while keyboards and
mice have `/dev/input/event*` nodes, so the two can be told apart.

## Decision

1. Screen and input methods work only inside a control session. `BeginControl` starts
   one for the calling D-Bus connection: the screen edges glow blue and a pill below the
   top bar reads "Arcee is working" with a Stop button. `EndControl`, the Stop button,
   the stop shortcut, the owner's connection closing (even by a crash) and two minutes
   without a call end it. Calls from anyone but the owner are refused.
2. The user's input pauses Arcee instead of being blocked (the user chose this): a key,
   click, scroll, touch or more than 40 px of pointer travel from a device with a device
   node pauses the session. The user's input still reaches their apps. The glow turns
   amber, the pill offers Resume and Stop, and Arcee's input calls wait until the user
   resumes or stop the task if they press Stop.
3. Glow and pill are hidden for the moment of each screenshot, so neither spoils OCR.
5. Control covers every action the user can see, not only the screen tools: before a
   browser, window-tree, screen, keyboard or app-opening action the agent takes control
   and waits out a pause. Background work (files, web search, system facts) does not.
   While Arcee asks the user something in the terminal it gives control back, so typing
   the answer does not pause it. Opening or switching a Chrome tab, or navigating, brings
   Chrome's window forward through the extension (Chrome cannot raise itself on Wayland).
4. OCR splits a line at gaps wider than one and a half text heights and drops words
   under 30% confidence, so controls side by side read as separate targets.

## Consequences

- Nothing reads or drives the desktop through Arcee without the glow on screen.
- Taking the mouse is always safe and always wins; there is no lockout to recover from.
- A remote-desktop session's input also comes from virtual devices, so it would not pause
  Arcee. Revisit if remote use matters.
- The pause was tested only through its parts (the headless shell has no physical
  devices); it needs a check on the real desktop.

## Amended 2026-11-08: the user's input over app windows pauses too

The pause relied on a Clutter event filter, and GNOME gives an extension's filter only
input meant for the Shell's own UI: over Chrome, Files or any app window the user's
mouse and keys never reached it, so Arcee kept driving while the user worked (found
with Esc on 2026-11-04, when a headless test with an app focused saw no key at the
filter). The session now also watches, ten times a second, what GNOME knows whoever the
input is for: the pointer's position, and the time since any input (its idle monitor).
The pointer moving more than 40 px that Arcee did not move pauses it (small moves are
forgotten after a second still); input after Arcee's last event, with the pointer
still, is a key, a click or a scroll and pauses it. Arcee's own input is told apart by
time: the bridge notes each event it injects, and input within 250 ms of one counts as
Arcee's. The filter stays for Esc and the Shell's own UI.
`tests/desktop/control_pause.sh` checks it in a headless GNOME Shell with an app window
in front: Arcee's own click and typing, and a 7 px nudge, do not pause; the user's key,
mouse, a key after a nudge, and a key after Arcee acted, do.

A pause no longer counts against the request's time limit (10 minutes): while Arcee
waits to take control back, the limit is suspended, and afterwards moved back by the
time the user held it (a wait under half a second is Arcee's own, and counts). A
request paused for longer than its whole limit finishes once resumed.
