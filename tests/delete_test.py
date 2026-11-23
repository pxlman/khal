import datetime as dt
import json

import pytest
from click.testing import CliRunner

from khal import trash
from khal.cli import main_khal


ME = "me@example.com"


def _setup(tmp_path, ics, name):
    cal = tmp_path / "cal"
    cal.mkdir()
    (cal / name).write_text(ics)
    cfg = tmp_path / "khal.conf"
    cfg.write_text(
        f"""[calendars]
[[c]]
path = {cal}
[locale]
timeformat = %H:%M
dateformat = %d.%m.
longdateformat = %d.%m.%Y
datetimeformat = %d.%m. %H:%M
longdatetimeformat = %d.%m.%Y %H:%M
local_timezone = Europe/Berlin
default_timezone = Europe/Berlin
[sqlite]
path = {tmp_path}/khal.db
"""
    )
    return cfg, cal / name


OWN_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:gym@example.com
SUMMARY:Gym
DTSTART;TZID=Europe/Berlin:{d}T180000
DTEND;TZID=Europe/Berlin:{d}T190000
RRULE:FREQ=DAILY{rr}
END:VEVENT
END:VCALENDAR
"""

OWN_SINGLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:dentist@example.com
SUMMARY:Dentist
DTSTART;TZID=Europe/Berlin:{d}T120000
DTEND;TZID=Europe/Berlin:{d}T130000
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture(autouse=True)
def isolated_trash(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def _day(n=1):
    return (dt.date.today() + dt.timedelta(days=n)).strftime("%Y%m%d")


def run(cfg, *args):
    return CliRunner().invoke(main_khal, ["-c", str(cfg), *args])


def test_delete_one_off_with_this(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SINGLE.format(d=_day()), "d.ics")
    r = run(cfg, "delete", "--this", "-y", "Dentist")
    assert r.exit_code == 0, r.output
    assert not f.exists()
    assert trash.entries()[0][1]["action"] == "deleted"


def test_delete_this_occurrence_adds_exdate(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SERIES.format(d=_day(), rr=""), "g.ics")
    r = run(cfg, "delete", "--this", "-y", "Gym")
    assert r.exit_code == 0, r.output
    text = f.read_text()
    assert "EXDATE" in text and "RRULE" in text
    assert trash.entries()[0][1]["action"] == "changed"
    # the next day is still there, today's is gone
    out = run(cfg, "list", "--format", "{title}", "today", "5d").output
    assert out.count("Gym") == 3


def test_delete_following_sets_until(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SERIES.format(d=_day(), rr=";COUNT=10"), "g.ics")
    r = run(cfg, "delete", "--this", "-y", "--at", _day(3)[6:] + "." + _day(3)[4:6] + ". 18:00", "Gym")
    assert r.exit_code == 0, r.output
    r = run(cfg, "delete", "--following", "-y", "--at", _day(5)[6:] + "." + _day(5)[4:6] + ". 18:00", "Gym")
    assert r.exit_code == 0, r.output
    text = f.read_text()
    assert "UNTIL=" in text and "COUNT" not in text
    out = run(cfg, "list", "--format", "{start-date} {title}", "today", "10d").output
    assert out.count("Gym") == 3  # day 1, 2, 4 remain (3 was deleted, 5+ gone)


def test_delete_following_from_first_deletes_all(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SERIES.format(d=_day(), rr=""), "g.ics")
    r = run(cfg, "delete", "--following", "-y", "Gym")
    assert r.exit_code == 0, r.output
    assert not f.exists()


def test_delete_all(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SERIES.format(d=_day(), rr=""), "g.ics")
    r = run(cfg, "delete", "--all", "-y", "Gym")
    assert r.exit_code == 0, r.output
    assert not f.exists()


def test_undelete_restores_changed_in_place(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SERIES.format(d=_day(), rr=""), "g.ics")
    run(cfg, "delete", "--this", "-y", "Gym")
    assert "EXDATE" in f.read_text()
    r = run(cfg, "undelete")
    assert r.exit_code == 0, r.output
    assert "EXDATE" not in f.read_text()
    assert trash.entries() == []


def test_undelete_deleted_gets_new_uid(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SINGLE.format(d=_day()), "d.ics")
    run(cfg, "delete", "-y", "Dentist")
    assert not f.exists()
    r = run(cfg, "undelete")
    assert r.exit_code == 0, r.output
    files = list(f.parent.glob("*.ics"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "SUMMARY:Dentist" in text and "UID:dentist@example.com" not in text


def test_undelete_list_and_empty(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SINGLE.format(d=_day()), "d.ics")
    assert run(cfg, "undelete").exit_code == 1
    run(cfg, "delete", "-y", "Dentist")
    r = run(cfg, "undelete", "--list")
    assert "Dentist" in r.output and "deleted" in r.output


def test_delete_asks_confirmation(tmp_path):
    cfg, f = _setup(tmp_path, OWN_SINGLE.format(d=_day()), "d.ics")
    r = CliRunner().invoke(main_khal, ["-c", str(cfg), "delete", "Dentist"], input="n\n")
    assert r.exit_code == 0 and f.exists()


def test_trash_keeps_json(tmp_path):
    p = trash.save("c", "x.ics", "ICS", "deleted", "S", "u")
    assert json.loads(p.read_text())["ics"] == "ICS"
