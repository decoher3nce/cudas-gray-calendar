# CUDAS Gray Group calendar

Once a day, a GitHub Action reads the club's published schedule sheet (every weekly tab), pulls the
**Gray Group PM swim** and **Gray Group strength** rows, and rebuilds `docs/gray.ics`. GitHub Pages serves
that file at a fixed URL that your phone, your wife's phone, and your work calendar subscribe to. If any
upcoming session is added, removed, or retimed, it emails both of you a summary.

What ends up in the calendar:
- **Timed events** for each swim and strength session, with the pool address as the location (VMAC or BCC).
  The original cell text (e.g. `6:15-8:15pm @VMAC(M)`) is in the event notes.
- **All-day events** for meet days in the swim row (e.g. "Gray: ACES FALL/ D-INVITE").
- **Nothing** for "NO PRACTICE", "HAPPY HOLIDAYS", or blank cells.
- Past weeks stay in the calendar even after the club deletes their tabs.

## One-time setup (about 15 minutes)

1. **Create a GitHub repo** (e.g. `cudas-gray-calendar`) and push these files to it. It has to be **public**
   for free GitHub Pages. Nothing private goes in it: the schedule is already public, and email
   settings live in encrypted Secrets.
2. **Turn on Pages:** Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs` → Save.
3. **Add Secrets** (Settings → Secrets and variables → Actions → New repository secret):

   | Secret | Example |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | a Gmail address you control |
   | `SMTP_PASS` | a Gmail **App Password** (Google Account → Security → 2-Step Verification → App passwords). Not your normal password. |
   | `NOTIFY_TO` | `you@proton.me, wife@example.com` |

   Without these, the calendar still updates but no emails go out.
4. **Run it once:** Actions → *Update Gray Group calendar* → **Run workflow**. The first run saves a
   baseline and does not send email. Check that `docs/gray.ics` was committed.
5. **Subscribe** using `webcal://<your-github-username>.github.io/<repo>/gray.ics`
   - **iPhone:** Settings → Apps → Calendar → Calendar Accounts → Add Account → Other →
     *Add Subscribed Calendar*. Or on a Mac in Calendar, File → New Calendar Subscription with
     Location = **iCloud**, which puts it on every Apple device you use.
   - **Your wife:** send her the same link. Tapping it on an iPhone offers to subscribe.
   - **Work (Outlook):** Add calendar → *Subscribe from web* → paste the `https://` version of the URL.

## Good to know
- **Refresh delay is set by the calendar app, not this job.** iOS lets you set the fetch interval for
  subscribed calendars. Outlook and Google Calendar refresh URL subscriptions on their own schedule,
  often every few hours and sometimes 24 h or more, and you can't change that. The email is the fast path.
- **Failures are loud.** If the sheet's layout changes so that no Gray events can be read, the job exits
  with an error. It leaves the calendar alone, and GitHub emails the repo owner about the failed run.
  Cells it can't read (e.g. a garbled time) are listed in the change email.
- **Daily commit.** Each run commits `state/events.json` (last-check time) on purpose: GitHub turns off
  scheduled workflows in repos with no activity for 60 days.
- **Times are parsed as written.** `6:15-8:15pm` means 6:15 pm to 8:15 pm. A start time with no am/pm
  takes the end time's suffix unless that would put the start after the end (`11:30-1:00pm` → 11:30 am).
- If the club starts a new sheet (new season), update `SHEET_PUB_ID` at the top of `swimcal.py`.

## Local test
```
python -m pytest -q tests
python swimcal.py   # live run; prints the would-be email if SMTP isn't set
```
