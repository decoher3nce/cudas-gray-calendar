"""Tests built from cell formats observed in the live sheet on 2026-11-02."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import swimcal  # noqa: E402

TODAY = date(2026, 11, 2)


def tab(week_dates, swim, strength, dup=False):
    head = ",,,,,,,\n2026 Practice Schedule - week of X,,,,,,,\n,,,,,,,\nBroomfield Community Center,,,,,,,\n"
    head += ",Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday\n"
    head += "," + ",".join(week_dates) + "\n"
    head += "White Group swim,4:45-6:30pm,4:45-6:30pm,,,,,\n,,,,,,,\nVeterans Memorial Aquatic Center,,,,,,,\n"
    head += ",Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday\n"
    head += "," + ",".join(week_dates) + "\n"
    body = "Gray Group PM swim," + ",".join(swim) + "\n"
    if dup:
        body += body
    body += "Gray Group strength," + ",".join(strength) + "\n"
    body += 'Gold Group PM swim,4:45-6:45pm @VMAC,,,,,,\n,"*All VMAC workouts will be held at Veterans Memorial Aquatic Center: 5310 E 136th Ave, Thornton 80241",,,,,,\n'
    return head + body


NOV2 = tab(["November 2", "November 3", "November 4", "November 5", "November 6", "November 7", "November 8"],
           ["6:30-8:15pm @VMAC(M)", "6:15-8:15pm @VMAC(M)", "6:15-8:00pm @VMAC(M)", "6:00-7:45pm @VMAC(M)",
            "ACES FALL/ D-INVITE", "ACES FALL/ D-INVITE", "ACES FALL/ D-INVITE"],
           ["", "6:15-6:45pm @VMAC ", "", "", "", "", ""], dup=True)
NOV23 = tab(["November 23", "November 24", "November 25", "November 26", "November 27", "November 28", "November 29"],
            ["8:45am-10:45am @VMAC"] * 6 + [""],
            ["8:45-9:15am @VMAC", "", "8:45-9:15am @VMAC", "", "", "", ""])
DEC14 = tab(["December 14", "December 15", "December 16", "December 17", "December 18", "December 19", "December 20"],
            ["6:30-8:15pm@VMAC"] * 5 + ["9:00-11:00am @BCC", ""],
            ["", "", "", "", "", "10:30-11:00am @BCC", ""])
HOLIDAY = tab(["December 21", "December 22", "December 23", "December 24", "December 25", "December 26", "December 27"],
              ["HAPPY HOLIDAYS"] + [""] * 6, [""] * 7)
NEWYEAR = tab(["December 28", "December 29", "December 30", "December 31", "January 1", "January 2", "January 3"],
              ["8:45am-10:45am @VMAC", "", "", "", "NO PRACTICE", "11:30-1:00pm @VMAC", ""], [""] * 7)


def test_time_parsing():
    p = swimcal.parse_time_cell
    assert p("6:15-8:15pm @VMAC(M)") == ((18, 15), (20, 15), "VMAC", "(M)")
    assert p("8:45am-10:45am @VMAC")[:2] == ((8, 45), (10, 45))
    assert p("8:45-9:15am @VMAC")[:2] == ((8, 45), (9, 15))
    assert p("6:30-8:15pm@VMAC")[:3] == ((18, 30), (20, 15), "VMAC")
    assert p("11:30-1:00pm")[:2] == ((11, 30), (13, 0))      # crosses noon
    assert p("5:00-5:5:45pm")[:2] == ((17, 0), (17, 45))     # real typo in sheet
    assert p("10:30-11:00am @BCC")[2] == "BCC"
    assert p("ACES FALL/ D-INVITE") is None


def test_parse_tab_and_dedupe():
    ev, cov, warn = swimcal.parse_tab(NOV2, "Nov 2-8", TODAY)
    swims = [e for e in ev if e["kind"] == "swim"]
    notes = [e for e in ev if e["kind"] == "note"]
    strength = [e for e in ev if e["kind"] == "strength"]
    assert len(swims) == 4 and len(notes) == 3 and len(strength) == 1
    assert swims[0] == {**swims[0], "date": "2026-11-02", "start": "18:30", "end": "20:15"}
    assert "Thornton" in swims[0]["location"]
    assert strength[0]["date"] == "2026-11-03"
    assert not warn and len(cov) == 7
    # White/Gold group rows and the BCC date header must not leak in
    assert all(e["title"].startswith("Gray") for e in ev)


def test_year_rollover_and_skips():
    ev, _, _ = swimcal.parse_tab(NEWYEAR, "Dec 28-Jan 3", date(2026, 12, 20))
    dates = [e["date"] for e in ev]
    assert dates == ["2026-12-28", "2027-01-02"]           # NO PRACTICE skipped
    ev, _, _ = swimcal.parse_tab(HOLIDAY, "Dec 21-27", TODAY)
    assert ev == []


def run_with(tabs, today, tmp_path, monkeypatch, sent):
    monkeypatch.setattr(swimcal, "STATE_FILE", tmp_path / "state" / "events.json")
    monkeypatch.setattr(swimcal, "ICS_FILE", tmp_path / "docs" / "gray.ics")
    monkeypatch.setattr(swimcal, "send_email", lambda s, b: sent.append((s, b)))
    return swimcal.run(today=today, fetch=lambda gid: tabs[gid], list_tabs=lambda: [(g, g) for g in tabs])


def test_end_to_end_change_detection(tmp_path, monkeypatch):
    sent = []
    tabs = {"a": NOV2, "b": NOV23, "c": DEC14}
    assert run_with(tabs, TODAY, tmp_path, monkeypatch, sent) == 0
    assert sent == []                                       # first run = baseline only
    ics = (tmp_path / "docs" / "gray.ics").read_text()
    assert ics.count("BEGIN:VEVENT") == 4 + 3 + 1 + 6 + 2 + 6 + 1
    assert "DTSTART;TZID=America/Denver:20261102T183000" in ics

    # Unchanged rerun -> no email
    assert run_with(tabs, TODAY, tmp_path, monkeypatch, sent) == 0
    assert sent == []

    # Club edits: time change, a cancellation, a new strength session
    tabs["a"] = NOV2.replace("6:15-8:00pm @VMAC(M)", "6:45-8:30pm @VMAC(S)", 1)
    tabs["b"] = NOV23.replace("8:45am-10:45am @VMAC,8:45am-10:45am @VMAC,8:45am-10:45am @VMAC,8:45am-10:45am @VMAC",
                              "8:45am-10:45am @VMAC,8:45am-10:45am @VMAC,8:45am-10:45am @VMAC,NO PRACTICE", 1)
    tabs["c"] = DEC14.replace('Gray Group strength,,', 'Gray Group strength,5:30-6:00pm @VMAC,', 1)
    run_with(tabs, TODAY, tmp_path, monkeypatch, sent)
    assert len(sent) == 1
    subj, body = sent[0]
    assert "3 changes" in subj
    assert "Wed Nov 4: Gray swim @VMAC(M) 6:15pm-8:00pm" in body and "6:45pm-8:30pm" in body
    assert "REMOVED:\n  Thu Nov 26" in body
    assert "ADDED:\n  Mon Dec 14: Gray strength" in body
    ics = (tmp_path / "docs" / "gray.ics").read_text()
    assert "SEQUENCE:1" in ics

    # Club deletes an old tab later: past events survive in the calendar
    sent.clear()
    del tabs["a"]
    run_with(tabs, date(2026, 11, 20), tmp_path, monkeypatch, sent)
    state = json.loads((tmp_path / "state" / "events.json").read_text())
    assert any(e["date"] == "2026-11-02" for e in state["events"])
    assert sent == []                                       # past-only differences don't email


def test_refuses_to_blank_calendar(tmp_path, monkeypatch):
    assert run_with({"x": "garbage,,,\n"}, TODAY, tmp_path, monkeypatch, []) == 3
    assert not (tmp_path / "docs" / "gray.ics").exists()


def test_ics_lines_are_folded():
    e = {"kind": "swim", "date": "2026-11-02", "allday": False, "start": "18:30", "end": "20:15",
         "title": "Gray swim", "location": "x" * 200, "raw": "y"}
    ics = swimcal.build_ics([e], swimcal.datetime.now(swimcal.TZ), {})
    assert all(len(l.encode()) <= 75 for l in ics.split("\r\n"))
