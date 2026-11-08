# 0018: Timers and reminders, fired by arceed

Date: 2026-11-08. Status: accepted.

## Context

"Set a timer for 10 minutes", "remind me at 5 to call mom" (TIME-001, 003, 004 in the
task list) had no home: a request ends when its reply is given, and nothing in Arcee
kept time after that. The same need comes back in other tasks: a do-not-disturb for an
hour, a focus session, a routine every morning, and Arcee checking back on something
itself (AGENT-003). So the piece built here is a scheduler in arceed, with timers and
reminders as its first use.

## Decision

- Reminders are rows in History (`history.db`, schema 4, table `reminders`: text, when
  due, timer or reminder, pending / fired / cancelled), so they outlive arceed, a
  reboot and the request that set them.
- Three tools, offered when a request speaks of timers, reminders, alarms or "in N
  minutes": `reminder.add` (text, and `in_minutes` or `at`: a local date and time the
  model works out from Now, or a clock time today, tomorrow once past), `reminder.list`,
  `reminder.cancel` (by id). A timer is a reminder set with a duration. They run without
  asking: nothing is sent or lost, and each is undone by another call.
- arceed's scheduler (`arcee/reminders.py`) sleeps until the soonest pending reminder,
  30 s at most (one set by another process is seen within that), and wakes at once when
  one is set, cancelled or snoozed in arceed. A due reminder is marked fired, then shown.
- The reminder is a GNOME notification over arceed's own bus: critical urgency, so it
  stays until dismissed; the theme's alarm sound; Snooze 10 min and Done. Snooze sets
  it pending again.
- arceed has to be running to fire them. The login unit's check (`python -m
  arcee.settings listening`, the name kept for units already installed) now starts
  arceed when listening is on or a reminder is pending, and the app restarts arceed
  after a settings change under the same rule. A reminder that fell due while arceed
  was not running (the computer off, Arcee quit) fires when it next starts, saying when
  it was due.

## Consequences

- Quitting Arcee from the panel stops reminders until arceed starts again (the next
  login, or any use of Arcee); they are late, not lost.
- Recurring reminders, routines (a request run on a schedule), do-not-disturb for a
  time and Arcee waking its own task later can be built on the same scheduler.
- Checked against a headless GNOME Shell: a timer set for 2 s fired on time and sat in
  GNOME's tray with its title, body, critical urgency and both buttons. The sound needs
  a real session to hear.

## Amended 2026-11-08: alarms, repeats and routines

- An alarm (`reminder.add(alarm=true)`, "wake me at 7") is a reminder that rings: the
  alarm sound every 2.5 s through pw-play until the notification is dismissed or
  snoozed, five minutes at most.
- `repeat` (daily, weekdays, weekends, weekly) keeps a reminder or alarm pending after
  it fires, at its next time with the same clock time (history.db schema 6: a `repeat`
  column). Snoozing one that repeats makes a one-off copy, so its own time stays.
- A routine (`routine.add`, "every morning at 9 tell me the weather and my meetings")
  is a repeating reminder of kind `routine` whose text is a request: when due, arceed
  runs it as if the user had asked, in a new conversation, once it is free (up to 30
  minutes). One missed by more than two hours (the computer off) is skipped. Setting
  one asks first: it will act later, unattended. They are listed and cancelled with
  reminder.list and reminder.cancel.
