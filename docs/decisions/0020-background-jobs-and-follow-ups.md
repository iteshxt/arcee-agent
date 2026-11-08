# 0020: Background jobs and Arcee's own follow-ups

Date: 2026-11-08. Status: accepted.

## Context

A request ran to its end inside one task: a tool returned, the model was called, and
so on until the reply. Two kinds of work did not fit (AGENT-002 and AGENT-003 in the
task list):

- Long commands: a build, a test suite, downloads. `shell.run` holds the step for two
  minutes at most, and the model has nothing to do meanwhile.
- Coming back later: "check the render again in 5 minutes", "tell me when this page
  says In stock". Nothing ran after the reply except the user's timers and reminders
  (decision 0018).

## Decision

- **Background jobs** (`arcee/jobs.py`). `shell.start(cmd, cwd, then)` starts a command
  under arceed and returns a job id at once. It is tiered exactly like `shell.run`, so
  anything outside the read-only allowlist asks first. Jobs run under `systemd-run
  --user --scope` with 4 GB, 3 CPUs and 4 hours at most, at most 8 at a time. Each runs
  in its own process group, so `job.stop` ends all of it. Output (stdout and stderr) goes
  to a log file under History's folder (`jobs/`).
- `control.wait_for(event="job_done", target=<job>)` waits inside the task and returns
  the moment the job ends; the model is not called meanwhile. `job.check` reads the
  state, exit code and the end of the output. It is marked untrusted, like `shell.run`.
- **A job that outlives its task.** When a job ends, arceed waits until the task that
  started it is over (it may still look). If the model never saw the job end, arceed
  says so:
  - with `then`, by a follow-up request ("Tell me whether the tests passed"), run like a
    routine once Arcee is free;
  - otherwise by a notification.

  Jobs are not kept across an arceed restart; the log stays.
- **Follow-ups** (`arcee/followups.py`, `control.set_timer`). A follow-up is a reminder of
  kind `followup` in History. It needs schema 7: a `detail` column holds, as JSON, the
  request it continues and an optional watch. It is fired by the same scheduler as the
  user's timers, and it shows in `reminder.list` and is cancelled with
  `reminder.cancel`. When due:
  - without a watch, its `check` runs as a new request, labelled as a follow-up of the
    original request;
  - with a watch, code checks first, without the model, every `every_minutes`, until
    `for_hours` (72 at most) has passed. A watch can be: a page changes, a page says
    or stops saying some words, a file appears or goes, or a read-only command succeeds.
    The request runs only once the watch happens; a watch that gives up is a
    notification.
- Limits: 10 follow-ups pending at once, the first check within 24 hours, at least a
  minute between looks. A page watch reads the page when it is set: this checks the
  address, records the baseline for "changes", and refuses a watch whose event has
  already happened.
- **Untrusted content stays out of requests.** A follow-up request carries what was
  seen in Arcee's own words ("the page now says “In stock”", "job1 finished with exit
  code 0") and points to `job.check` for output. A page's or command's text never
  becomes part of a request, which the model reads as the user's intent.
- `control.set_timer` runs without asking. It runs once, within three days, and it is
  listed and cancellable. The request it starts goes through the gate like any other,
  so anything risky still asks. `routine.add`, which repeats forever, still asks first.
- The scheduler no longer waits for a routine. A routine (or follow-up) can wait up
  to half an hour for Arcee to be free, so it now runs in its own task; before, a
  timer due behind a waiting routine fired late.
- Plans: starting something, waiting for it and reading its result are one plan step,
  since later steps see only the earlier replies. A step that the earlier notes show
  already done replies at once. A 4-step plan for "run X in the background and tell
  me the output" had redone the work and taken 13 calls; it now takes 5.

## Consequences

- "Build the project and run the tests", "download these files and tell me when
  they're done", "check again in 10 minutes" and "watch this page" work without
  keeping the model busy, and keep working after the request has replied.
- A follow-up request runs unattended, while the user may be away. Its confirmations
  wait the usual time and then fail closed.
- Not done: a "price dropped" judgement on a page. A watch is a fixed condition; the
  model judges only once the page changes. Jobs do not survive an arceed restart.
