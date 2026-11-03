# 0017. Calendar, mail and contacts through the Google account GNOME already has

Date: 2026-11-04
Status: accepted 2026-11-04 (the user's approval; amended with the check's results)

## Context

Calendar and mail are the requests people make most that Arcee does worst. In the task
catalogue (docs/tasks, kept locally), 6 tasks need a calendar list, 3 need to add
events, and all 9 mail tasks are only partly doable. Today Arcee does them by driving
Gmail and Google Calendar in Chrome: slow (a dozen steps to read one email), fragile
(stale pages after clicks; Gmail's file picker cannot be driven), and it takes the
screen. One real run (2026-11-04) opened GNOME Calendar, then said it had opened Google
Calendar in Chrome.

There are three ways to reach the data instead:

1. **Arcee's own Google Cloud project** (OAuth client, Gmail and Calendar APIs, narrow
   scopes). Every user would create a project and an OAuth client, or Arcee would ship
   one: Gmail's read scopes are "restricted", so a shared client needs Google's
   verification and a yearly security assessment before more than 100 people can use
   it, and an unverified one shows a warning. In "testing" mode its tokens expire every
   7 days. Too heavy for a .deb anyone installs.
2. **Keep driving the browser.** Stays as the fallback, not the way.
3. **GNOME Online Accounts (GOA).** Ubuntu's Settings → Online Accounts signs in to
   Google with GNOME's own verified client and keeps the tokens in the keyring. Apps
   on the session bus ask GOA for a fresh access token (`GetAccessToken`); that is how
   Evolution, GNOME Calendar and Contacts reach Google. This machine already has a
   Google account there, with Mail, Calendar and Contacts on.

## Decision

Arcee reaches calendar, mail and contacts through **GNOME Online Accounts**, with
what GOA's token is accepted for. A check on the user's account (2026-11-04) showed
which: Google's Calendar API is on for GNOME's project, its Gmail and People APIs are
not ("has not been used in project … or it is disabled"), and IMAP and CardDAV take the
token as Evolution's do:

| Data | Way in (checked) | Library |
| --- | --- | --- |
| Calendar | Google Calendar API v3: all calendars, search, free/busy, guests (CalDAV reached only the main calendar) | httpx |
| Mail | IMAP with XOAUTH2 and Gmail's extensions (X-GM-RAW: Gmail's own search syntax), SMTP to send | Python's `imaplib`, `smtplib`, `email` |
| Contacts | CardDAV | httpx, vCard fields read directly |

- **No sign-in in Arcee.** If no Google account is set up, Arcee says so and opens
  Settings → Online Accounts. Arcee never sees or stores a password, and keeps no
  token: it asks GOA for one per use. Removing the account in Settings (or turning its
  Mail or Calendar off) cuts Arcee off at once.
- **Which account:** the only one; with several, the one the user names, else the one
  chosen in Arcee's settings.
- **The browser stays the fallback** for what the protocols cannot do (a Google Meet
  link that only the web page shows, Gmail settings the user asks to see).

### What Arcee may do, and what it asks first

GOA's Google token grants full mail and calendar access; Arcee limits itself:

| Action | Tools | Arcee |
| --- | --- | --- |
| List, search, read events and free time | `calendar.list` | runs |
| Add an event (no guests) | `calendar.add` | **asks first**, showing title, date, time, calendar |
| Change or delete an event | `calendar.update`, `calendar.delete` | **asks first** |
| Add or change an event **with guests** (Google emails them) | `calendar.add/update` | **asks first**, naming every guest |
| List, search, read mail; read attachment names | `mail.list`, `mail.search`, `mail.read` | runs |
| Save attachments to ~/Downloads | `mail.attachments` | runs (never opens them) |
| Write a draft (left in Drafts, not sent) | `mail.draft` | runs |
| Send, reply, forward | `mail.send` | **asks first, every time**, showing recipients, subject and the whole text |
| Mark read, archive, label, move to Trash | `mail.update` | **asks first** (count and subjects); at most 50 at once |
| Look up a contact's address or number | `contacts.find` | runs |
| Permanently delete, empty Trash, change filters, forwarding, delegates, signatures, any setting | none | **never**: no tool exists |
| Accept or decline invitations (emails the organiser) | `calendar.respond` | **asks first** |

Mail is outside content: what it says is data, never instructions (the existing
untrusted marking and instruction scan apply), and any action taken after reading an
email is confirmed whatever its tier.

### Where the content goes

Reading mail or a calendar means the model reads it. With the Groq Core, that content
goes to Groq; with Local (Ollama), it stays on this computer. At the user's word,
**Arcee asks each time**: before a task's first mail, calendar or contacts result would
go to a cloud model, it asks ("This needs your email's text to go to Groq. Go ahead?"),
once per task; no answer is remembered. On the local model it does not ask. A setting
(Permissions) can later make the answer standing:

- **Ask each time** (the default, the user's choice), or
- **Cloud model may read mail and calendar**, or
- **Local model only for mail and calendar** (those tasks run on Ollama), or
- **Off** (mail and calendar tools are not offered).

Tools return the least that answers: headers and a short snippet for lists, one
message's text (HTML stripped, quoted history trimmed, attachments by name only) for a
read. Task summaries in memory (decision 0014) may mention what a task found; they stay
on this computer.

### Building it

1. Checked, with the user's go-ahead (2026-11-04): a GOA token (no password, about an
   hour's life), an IMAP login (Gmail search available), the Calendar API's calendar
   list (Events, Family, Holidays in India), CardDAV's address book. Gmail's and the
   People APIs refused GNOME's token; nothing is built on them.
2. Calendar: `calendar.list`, `calendar.add`, `calendar.update`, `calendar.delete`,
   `calendar.respond`; contacts: `contacts.find`.
3. Mail: `mail.list`, `mail.search`, `mail.read`, `mail.attachments`, `mail.draft`,
   `mail.send`, `mail.update`.
4. The app: an Accounts section (the GOA accounts, which one is used, the
   content setting), a Diagnostics check, and setup's step that offers it.
5. Held-out eval cases for each tool, and the catalogue updated.

## Consequences

- Calendar and mail need no screen, no Chrome and no clicks: one tool call where the
  browser took a dozen steps, and they work while the user uses the computer.
- Nothing to sign up for: a user with Google in Online Accounts is ready; one without
  adds it once in Settings, with Google's own sign-in.
- No new dependencies: the Calendar API is JSON over httpx; mail is the standard
  library; vCards are read directly.
- Only Google through GOA at first. GOA's other accounts (Microsoft 365, Nextcloud,
  IMAP) use the same protocols and could follow.
- Arcee relies on GNOME's client being allowed by Google; GNOME's own apps depend on
  it too. If it ever stopped, the browser fallback remains.
- The open question in the roadmap ("should saving an event need confirmation?") is
  answered here: yes, with a one-tap preview.
