import datetime as dt
import json

import pytest
from click.testing import CliRunner

from khal import find
from khal.cli import main_khal


def ics(uid, summary, d, start="", end="", extra="", rr=""):
    day = (dt.date.today() + dt.timedelta(days=d)).strftime("%Y%m%d")
    if start:
        end_day = day
        if end < start:  # runs past midnight
            end_day = (dt.date.today() + dt.timedelta(days=d + 1)).strftime("%Y%m%d")
        times = (
            f"DTSTART;TZID=Europe/Berlin:{day}T{start}00\n"
            f"DTEND;TZID=Europe/Berlin:{end_day}T{end}00\n"
        )
    else:
        nxt = (dt.date.today() + dt.timedelta(days=d + 1)).strftime("%Y%m%d")
        times = f"DTSTART;VALUE=DATE:{day}\nDTEND;VALUE=DATE:{nxt}\n"
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\n"
        f"UID:{uid}\nSUMMARY:{summary}\n{times}{extra}{rr}END:VEVENT\nEND:VCALENDAR\n"
    )


@pytest.fixture
def cfg(tmp_path):
    personal, work = tmp_path / "personal", tmp_path / "work"
    personal.mkdir()
    work.mkdir()
    files = {
        personal: {
            "gym": ics("gym", "Gym", 0, "1800", "1900", "CATEGORIES:fitness\n", "RRULE:FREQ=DAILY\n"),
            "dentist": ics("dentist", "Dentist", 1, "0900", "1000", "LOCATION:Clinic\nDESCRIPTION:yearly checkup\n"),
            "holiday": ics("holiday", "Day off", 1),
        },
        work: {
            "standup": ics("standup", "Team standup", 1, "1000", "1030", "CATEGORIES:meeting,work\n"),
            "planning": ics("planning", "Planning", 3, "1400", "1500", "CATEGORIES:meeting\n"),
            "night": ics("night", "Night shift", 1, "2330", "0030"),
            "far": ics("far", "Far away", 30, "1000", "1100"),
        },
    }
    for folder, items in files.items():
        for name, content in items.items():
            (folder / f"{name}.ics").write_text(content)
    cfgfile = tmp_path / "khal.conf"
    cfgfile.write_text(
        f"""[calendars]
[[personal]]
path = {personal}
[[work]]
path = {work}
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
    return cfgfile


def q(cfg, *args, ok=True):
    r = CliRunner().invoke(main_khal, ["-c", str(cfg), "find", "--llm", *args])
    assert (r.exit_code == 0) == ok, r.output
    return json.loads(r.output)


def titles(result):
    return [e["title"] for e in result["events"]]


def test_default_is_next_seven_days(cfg):
    r = q(cfg)
    today = dt.date.today()
    assert r["range"] == {"from": str(today), "to": str(today + dt.timedelta(days=6))}
    assert "Far away" not in titles(r)
    assert titles(r).count("Gym") == 7
    assert {"Dentist", "Planning"} <= set(titles(r))


def test_single_date(cfg):
    r = q(cfg, "--date", "tomorrow")
    assert set(titles(r)) == {"Gym", "Dentist", "Day off", "Team standup", "Night shift"}
    assert r["range"]["from"] == r["range"]["to"]


def test_text_matches_title_notes_location(cfg):
    assert titles(q(cfg, "--text", "dentist")) == ["Dentist"]
    assert titles(q(cfg, "--text", "checkup")) == ["Dentist"]
    assert titles(q(cfg, "--text", "clinic")) == ["Dentist"]
    assert titles(q(cfg, "--text", "TEAM stand")) == ["Team standup"]
    assert titles(q(cfg, "--text", "nothing-like-this"))== []


def test_query_also_matches_calendar_name(cfg):
    # "Planning" never says "work" itself, but it lives in the "work" calendar
    r = q(cfg, "work", "--days", "5")
    assert {"Planning", "Team standup", "Night shift"} <= set(titles(r))
    assert {e["calendar"] for e in r["events"]} >= {"work"}
    assert titles(q(cfg, "work", "planning")) == ["Planning"]  # all words must match
    assert "Gym" not in titles(q(cfg, "work", "--days", "5"))
    assert set(titles(q(cfg, "perso", "--date", "tomorrow"))) == {"Gym", "Dentist", "Day off"}


def test_query_as_argument_equals_text_option(cfg):
    assert q(cfg, "dentist")["events"] == q(cfg, "--text", "dentist")["events"]
    assert titles(q(cfg, "team", "--text", "standup")) == ["Team standup"]


def test_title_only_ignores_notes(cfg):
    assert titles(q(cfg, "--title", "checkup")) == []
    assert titles(q(cfg, "--title", "dentist")) == ["Dentist"]


def test_tag_and_repeat_tags(cfg):
    assert set(titles(q(cfg, "--tag", "meeting"))) == {"Team standup", "Planning"}
    assert titles(q(cfg, "--tag", "meeting", "--date", "tomorrow")) == ["Team standup"]
    assert set(titles(q(cfg, "--tag", "fitness", "--tag", "work", "--days", "2"))) == {"Gym", "Team standup"}


def test_calendar_partial_name_and_unknown(cfg):
    assert {e["calendar"] for e in q(cfg, "--calendar", "wor")["events"]} == {"work"}
    err = q(cfg, "--calendar", "holidays", ok=False)
    assert err["ok"] is False and err["available_calendars"] == ["personal", "work"]


def test_time_filters_on_start_time(cfg):
    day = ["--date", "tomorrow"]
    assert set(titles(q(cfg, *day, "--after", "09:30"))) == {"Gym", "Team standup", "Night shift"}
    assert titles(q(cfg, *day, "--before", "10:00")) == ["Dentist"]
    assert titles(q(cfg, *day, "--after", "9:30am", "--before", "11")) == ["Team standup"]
    assert titles(q(cfg, *day, "--when", "morning")) == ["Dentist", "Team standup"]
    assert titles(q(cfg, *day, "--when", "evening")) == ["Gym"]
    assert titles(q(cfg, *day, "--when", "night")) == ["Night shift"]  # wraps midnight


def test_time_filter_excludes_all_day_and_all_day_flag(cfg):
    assert "Day off" not in titles(q(cfg, "--date", "tomorrow", "--after", "00:00"))
    r = q(cfg, "--date", "tomorrow", "--all-day")
    assert titles(r) == ["Day off"] and r["events"][0]["start"] is None


def test_combined_filters(cfg):
    r = q(cfg, "--calendar", "work", "--when", "morning", "--date", "tomorrow", "--tag", "meeting")
    assert titles(r) == ["Team standup"]


def test_range_options(cfg):
    t = dt.date.today()
    r = q(cfg, "--from", "tomorrow", "--days", "2")
    assert r["range"] == {"from": str(t + dt.timedelta(1)), "to": str(t + dt.timedelta(2))}
    r = q(cfg, "--to", str(t + dt.timedelta(1)))
    assert r["range"]["from"] == str(t)
    assert "Far away" in titles(q(cfg, "--days", "40", "--text", "far"))
    assert titles(q(cfg, "--from", str(t + dt.timedelta(30)), "--days", "1", "--calendar", "work")) == ["Far away"]


def test_bad_input_gives_json_error(cfg):
    for args in (
        ["--date", "someday"],
        ["--after", "late"],
        ["--from", "tomorrow", "--to", "yesterday"],
        ["--date", "today", "--days", "3"],
        ["--when", "morning", "--after", "08:00"],
        ["--days", "400"],
    ):
        r = q(cfg, *args, ok=False)
        assert r["ok"] is False and r["error"]


def test_limit_and_truncation(cfg):
    r = q(cfg, "--limit", "2")
    assert r["returned"] == 2 and r["total"] > 2 and r["truncated"] is True
    assert "showing the first 2" in r["summary"]
    assert q(cfg, "--limit", "0")["truncated"] is False


def test_llm_schema_is_flat_and_simple(cfg):
    ev = q(cfg, "--text", "dentist")["events"][0]
    assert set(ev) == {"n", "title", "date", "weekday", "all_day", "start", "end", "calendar",
                       "location", "tags", "repeats", "uid", "notes"}
    assert ev["n"] == 1 and ev["start"] == "09:00" and ev["end"] == "10:00"
    assert ev["location"] == "Clinic" and ev["notes"] == "yearly checkup"
    night = q(cfg, "--text", "night")["events"][0]
    assert night["end_date"] == (dt.date.fromisoformat(night["date"]) + dt.timedelta(1)).isoformat()


def test_llm_output_is_single_json_line(cfg):
    r = CliRunner().invoke(main_khal, ["-c", str(cfg), "find", "--llm"])
    assert len(r.output.strip().splitlines()) == 1


def test_empty_result_is_clear(cfg):
    r = q(cfg, "--text", "zzz")
    assert r["ok"] and r["events"] == [] and r["summary"].startswith("No events")


def test_human_output(cfg):
    r = CliRunner().invoke(main_khal, ["-c", str(cfg), "find", "--date", "tomorrow", "--tag", "meeting"])
    assert r.exit_code == 0
    assert "Team standup" in r.output and "[work]" in r.output and "#meeting" in r.output
    assert "10:00-10:30" in r.output and "{" not in r.output


def test_list_calendars_and_tags(cfg):
    assert q(cfg, "--list-calendars")["calendars"] == ["personal", "work"]
    assert q(cfg, "--list-tags")["tags"] == ["fitness", "meeting", "work"]
    r = CliRunner().invoke(main_khal, ["-c", str(cfg), "find", "--list-calendars"])
    assert r.output.split() == ["personal", "work"]


def test_parse_day():
    loc = {"dateformat": "%d.%m.", "longdateformat": "%d.%m.%Y", "datetimeformat": "%d.%m. %H:%M",
           "longdatetimeformat": "%d.%m.%Y %H:%M", "timeformat": "%H:%M"}
    assert find.parse_day("2026-11-30", loc) == dt.date(2026, 11, 30)
    assert find.parse_day("30.11.2026", loc) == dt.date(2026, 11, 30)
    assert find.parse_day("tomorrow", loc) == dt.date.today() + dt.timedelta(days=1)
    assert find.parse_day("friday", loc).weekday() == 4
    with pytest.raises(find.FindError):
        find.parse_day("nonsense", loc)
