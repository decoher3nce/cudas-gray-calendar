#!/usr/bin/env python3
"""
Front Range Barracudas (CUDAS) - Gray Group swim/strength calendar builder.

Daily job:
  1. Discover every weekly tab in the club's published Google Sheet.
  2. Pull each tab as CSV and parse the "Gray Group PM swim" and
     "Gray Group strength" rows.
  3. Merge with the saved state (past events are kept even after the club
     deletes old tabs), write docs/gray.ics, and diff against the previous run.
  4. If any upcoming event was added / removed / changed, email a summary.

Stdlib only. Config via environment variables (see README.md).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

PUB_ID = os.environ.get(
    "SHEET_PUB_ID",
    "2PACX-1vSGu-lfgzpzORerqW6yonzbo-e4DOxIcpAzrP8natIt-j8r3-SgNHWubr-QMDBptYX3AbIUDP5ihNqu",
)
PUB_BASE = f"https://docs.google.com/spreadsheets/d/e/{PUB_ID}"
SOURCE_PAGE = "https://www.gomotionapp.com/team/csbbsc/page/schedule"
TZ = ZoneInfo("America/Denver")

ROWS = {
    # row label in sheet (lowercased, whitespace-collapsed) -> (kind, event title)
    "gray group pm swim": ("swim", "Gray swim"),
    "gray group strength": ("strength", "Gray strength"),
}
LOCATIONS = {
    "VMAC": "Veterans Memorial Aquatic Center, 5310 E 136th Ave, Thornton, CO 80241",
    "BCC": "Broomfield Community Center, 280 Spader Way, Broomfield, CO 80020",
    "MOB": "CrossFit Mob, 12351 Grant St Unit 300, Thornton, CO 80241",
}
# Cell text that means "nothing scheduled" -> no event at all.
SKIP_TEXT = re.compile(r"^\s*$|no\s*practice|holiday|closed|^-+$", re.I)

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "events.json"
ICS_FILE = ROOT / "docs" / "gray.ics"

UA = {"User-Agent": "Mozilla/5.0 (swim-calendar bot)"}


# --------------------------------------------------------------------------- fetch
def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def discover_tabs(pubhtml: str) -> list[tuple[str, str]]:
    """Return [(tab_name, gid)] from the published-sheet HTML."""
    tabs = re.findall(r'name:\s*"([^"]+)"[^}]*?gid:\s*"(\d+)"', pubhtml)
    if not tabs:  # fallback: sheet menu buttons
        gids = re.findall(r'id="sheet-button-(\d+)"[^>]*>.*?<a[^>]*>([^<]+)</a>', pubhtml, re.S)
        tabs = [(name.strip(), gid) for gid, name in gids]
    seen, out = set(), []
    for name, gid in tabs:
        if gid not in seen:
            seen.add(gid)
            out.append((name, gid))
    return out


def fetch_tab_csv(gid: str) -> str:
    return http_get(f"{PUB_BASE}/pub?gid={gid}&single=true&output=csv")


# --------------------------------------------------------------------------- parse
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
DATE_RE = re.compile(r"^\s*([A-Za-z]+)\.?\s+(\d{1,2})\s*$")
TIME_RE = re.compile(
    r"(?P<sh>\d{1,2})(?::(?P<sm>\d{2}))?\s*(?P<sap>[ap]\.?m\.?)?\s*[-\u2013\u2014]\s*"
    r"(?:\d{1,2}:(?=\d{1,2}:\d{2}))?"      # tolerate typos like 5:00-5:5:45pm
    r"(?P<eh>\d{1,2})(?::(?P<em>\d{2}))?\s*(?P<eap>[ap]\.?m\.?)",
    re.I,
)
LOC_RE = re.compile(r"@\s*([A-Za-z]+)\s*(\([^)]*\))?")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def resolve_year(month: int, day: int, today: date) -> date:
    """Sheet dates have no year: pick the year that lands closest to today."""
    cands = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cands.append(date(y, month, day))
        except ValueError:
            pass
    return min(cands, key=lambda d: abs((d - today).days))


def parse_date_cell(cell: str, today: date) -> date | None:
    m = DATE_RE.match(cell or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower()) or next(
        (v for k, v in MONTHS.items() if k.startswith(m.group(1).lower()[:3])), None)
    if not mon:
        return None
    return resolve_year(mon, int(m.group(2)), today)


def _h24(h: int, ap: str) -> int:
    pm = ap.lower().startswith("p")
    h = h % 12
    return h + 12 if pm else h


def parse_time_cell(cell: str):
    """'6:15-8:15pm @VMAC(M)' -> ((18,15),(20,15),'VMAC','(M)') or None."""
    m = TIME_RE.search(cell)
    if not m:
        return None
    eh = _h24(int(m["eh"]), m["eap"])
    em = int(m["em"] or 0)
    sap = m["sap"]
    if sap:
        sh = _h24(int(m["sh"]), sap)
    else:  # start inherits end's am/pm unless that would put start after end
        sh = _h24(int(m["sh"]), m["eap"])
        if (sh, int(m["sm"] or 0)) > (eh, em):
            sh -= 12
    sm = int(m["sm"] or 0)
    if not (0 <= sh < 24 and (sh, sm) < (eh, em)):
        return None
    loc = LOC_RE.search(cell)
    return (sh, sm), (eh, em), (loc.group(1).upper() if loc else ""), (loc.group(2) or "" if loc else "")


def parse_tab(csv_text: str, tab_name: str, today: date):
    """Return (events, covered_dates, warnings) for one weekly tab.

    The Gray rows live under the 'Veterans Memorial Aquatic Center' block; the
    nearest preceding row of dates (e.g. 'November 2, November 3, ...') gives
    the column -> date mapping.
    """
    rows = list(csv.reader(io.StringIO(csv_text)))
    events, warnings, covered = [], [], set()
    col_dates: dict[int, date] = {}
    found = set()
    for row in rows:
        dates = {i: d for i, c in enumerate(row) if (d := parse_date_cell(c, today))}
        if len(dates) >= 3:
            col_dates = dates
            continue
        if not row:
            continue
        key = norm(row[0])
        if key not in ROWS or not col_dates:
            continue
        kind, title = ROWS[key]
        if kind in found:   # same row repeated in a tab -> ignore duplicate
            continue
        found.add(kind)
        for i, d in col_dates.items():
            covered.add(d.isoformat())
            raw = row[i].strip() if i < len(row) else ""
            if SKIP_TEXT.search(raw):
                continue
            t = parse_time_cell(raw)
            if t:
                (sh, sm), (eh, em), loc, pool = t
                events.append({
                    "kind": kind, "date": d.isoformat(), "allday": False,
                    "start": f"{sh:02d}:{sm:02d}", "end": f"{eh:02d}:{em:02d}",
                    "title": f"{title}" + (f" @{loc}{pool}" if loc else ""),
                    "location": LOCATIONS.get(loc, loc), "raw": raw,
                })
            elif kind == "swim":
                # Meet names etc. ("ACES FALL/ D-INVITE") -> all-day heads-up
                events.append({
                    "kind": "note", "date": d.isoformat(), "allday": True,
                    "start": "", "end": "", "title": f"Gray: {raw}",
                    "location": "", "raw": raw,
                })
            else:
                warnings.append(f"{tab_name} {d}: couldn't read {title} cell {raw!r}")
    for key, (kind, title) in ROWS.items():
        if kind not in found:
            warnings.append(f"{tab_name}: no '{title}' row found")
    return events, covered, warnings


# --------------------------------------------------------------------------- merge / diff
def slot(e) -> str:
    """Identity of an event independent of its time: one per kind per day."""
    return f"{e['date']}|{e['kind']}"


def merge(old: list[dict], new: list[dict], covered: set[str]) -> list[dict]:
    """Dates covered by the current sheet come from the sheet; anything else
    (past weeks whose tabs were deleted) is kept from the saved state."""
    kept = [e for e in old if e["date"] not in covered]
    return sorted(kept + new, key=lambda e: (e["date"], e["start"], e["kind"]))


def diff(old: list[dict], new: list[dict], today: date):
    fields = ("start", "end", "title", "location", "allday")
    o = {slot(e): e for e in old if e["date"] >= today.isoformat()}
    n = {slot(e): e for e in new if e["date"] >= today.isoformat()}
    added = [n[k] for k in sorted(n.keys() - o.keys())]
    removed = [o[k] for k in sorted(o.keys() - n.keys())]
    changed = [(o[k], n[k]) for k in sorted(o.keys() & n.keys())
               if any(o[k][f] != n[k][f] for f in fields)]
    return added, removed, changed


def fmt(e) -> str:
    d = date.fromisoformat(e["date"]).strftime("%a %b %-d")
    if e["allday"]:
        return f"{d}: {e['title']}"
    t = lambda hm: datetime.strptime(hm, "%H:%M").strftime("%-I:%M%p").lower()
    return f"{d}: {e['title']} {t(e['start'])}-{t(e['end'])}"


# --------------------------------------------------------------------------- ICS
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Denver
BEGIN:DAYLIGHT
TZOFFSETFROM:-0700
TZOFFSETTO:-0600
TZNAME:MDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0600
TZOFFSETTO:-0700
TZNAME:MST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    out, b = [], line.encode()
    while len(b) > 75:
        cut = 75 if not out else 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 char
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def build_ics(events: list[dict], stamp: datetime, seq: dict[str, int]) -> str:
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//swim-calendar//Gray Group//EN",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:CUDAS Gray Group",
         "X-WR-TIMEZONE:America/Denver",
         "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
         *VTIMEZONE.split("\n")]
    dtstamp = stamp.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    for e in events:
        uid = hashlib.sha1(slot(e).encode()).hexdigest()[:16] + "@cudas-gray"
        d = e["date"].replace("-", "")
        L += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dtstamp}",
              f"SEQUENCE:{seq.get(slot(e), 0)}"]
        if e["allday"]:
            nd = (date.fromisoformat(e["date"]) + timedelta(days=1)).strftime("%Y%m%d")
            L += [f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{nd}", "TRANSP:TRANSPARENT"]
        else:
            L += [f"DTSTART;TZID=America/Denver:{d}T{e['start'].replace(':', '')}00",
                  f"DTEND;TZID=America/Denver:{d}T{e['end'].replace(':', '')}00"]
        L.append(f"SUMMARY:{esc(e['title'])}")
        if e["location"]:
            L.append(f"LOCATION:{esc(e['location'])}")
        L.append(f"DESCRIPTION:{esc('Sheet says: ' + e['raw'] + chr(10) + 'Source: ' + SOURCE_PAGE)}")
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"


# --------------------------------------------------------------------------- email
def send_email(subject: str, body: str) -> None:
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "NOTIFY_TO"))
    if not (host and user and pw and to):
        print("Email not configured; skipping send.\n--- would send ---\n" + subject + "\n" + body)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM", user), to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=60) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    print(f"Emailed {to}")


# --------------------------------------------------------------------------- main
def run(today: date | None = None, fetch=None, list_tabs=None) -> int:
    now = datetime.now(TZ)
    today = today or now.date()
    fetch = fetch or fetch_tab_csv
    tabs = list_tabs() if list_tabs else discover_tabs(http_get(f"{PUB_BASE}/pubhtml"))
    if not tabs:
        print("ERROR: no tabs discovered; sheet layout or URL changed.", file=sys.stderr)
        return 2

    new_events, covered, warnings = [], set(), []
    for name, gid in tabs:
        ev, cov, w = parse_tab(fetch(gid), name, today)
        new_events += ev
        covered |= cov
        warnings += w
    if not new_events:
        print("ERROR: parsed zero Gray events; refusing to overwrite calendar.", file=sys.stderr)
        print("\n".join(warnings), file=sys.stderr)
        return 3

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    old = state.get("events", [])
    seq = state.get("sequence", {})
    first_run = not STATE_FILE.exists()

    merged = merge(old, new_events, covered)
    added, removed, changed = diff(old, merged, today)
    for _, e in changed:
        seq[slot(e)] = seq.get(slot(e), 0) + 1

    ICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ICS_FILE.write_text(build_ics(merged, now, seq), newline="")
    STATE_FILE.write_text(json.dumps(
        {"events": merged, "sequence": seq, "last_check": now.isoformat(timespec="seconds"),
         "tabs": [n for n, _ in tabs], "warnings": warnings}, indent=1))

    n_up = sum(e["date"] >= today.isoformat() for e in merged)
    print(f"{len(tabs)} tabs, {len(merged)} events ({n_up} upcoming). "
          f"+{len(added)} -{len(removed)} ~{len(changed)}")
    for w in warnings:
        print("WARN:", w)

    if first_run:
        print("First run: baseline saved, no change email.")
        return 0
    if added or removed or changed:
        lines = ["The CUDAS Gray Group schedule changed. Your subscribed calendar will pick this up "
                 "on its next refresh.", ""]
        if changed:
            lines += ["CHANGED:"] + [f"  {fmt(a)}\n    -> now {fmt(b).split(': ', 1)[1]}" for a, b in changed] + [""]
        if added:
            lines += ["ADDED:"] + [f"  {fmt(e)}" for e in added] + [""]
        if removed:
            lines += ["REMOVED:"] + [f"  {fmt(e)}" for e in removed] + [""]
        if warnings:
            lines += ["Cells I couldn't read (check the sheet):"] + [f"  {w}" for w in warnings] + [""]
        lines.append(f"Source: {SOURCE_PAGE}")
        n = len(added) + len(removed) + len(changed)
        send_email(f"Gray Group swim schedule: {n} change{'s' * (n != 1)}", "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(run())
