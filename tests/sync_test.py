import json
import os

import pytest
from click.testing import CliRunner

from khal import sync
from khal.cli import main_khal

ICS = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:{uid}\nSUMMARY:{s}\nDTSTART:20260101T100000Z\nDTEND:20260101T110000Z\nEND:VEVENT\nEND:VCALENDAR\n"
BASE = "/caldav/v2/me/events/"


class FakeDAV:
    """in-memory CalDAV server"""

    def __init__(self):
        self.base = "https://apidata.googleusercontent.com" + BASE
        self.items = {}  # href -> (etag, ics)
        self.n = 0

    def _etag(self):
        self.n += 1
        return f'"{self.n}"'

    def add(self, uid, summary):
        self.items[f"{BASE}{uid}.ics"] = (self._etag(), ICS.format(uid=uid, s=summary))

    def list_etags(self):
        return {h: e for h, (e, _) in self.items.items()}

    def multiget(self, hrefs):
        return {h: self.items[h] for h in hrefs if h in self.items}

    def put(self, href, ics, etag):
        cur = self.items.get(href)
        if (cur and etag != cur[0]) or (not cur and etag):
            return 412, None
        new = self._etag()
        self.items[href] = (new, ics)
        return (200 if cur else 201), new

    def delete(self, href, etag):
        cur = self.items.get(href)
        if not cur:
            return 404
        if cur[0] != etag:
            return 412
        del self.items[href]
        return 204


def test_download_upload_delete(tmp_path):
    dav = FakeDAV()
    dav.add("a", "remote A")
    stats = sync.sync_vdir(dav, tmp_path)
    assert stats["down"] == 1
    assert "remote A" in (tmp_path / "a.ics").read_text()

    (tmp_path / "b.ics").write_text(ICS.format(uid="b", s="local B"))
    stats = sync.sync_vdir(dav, tmp_path)
    assert stats["up"] == 1
    assert f"{BASE}b.ics" in dav.items

    (tmp_path / "a.ics").unlink()
    stats = sync.sync_vdir(dav, tmp_path)
    assert stats["deleted"] == 1
    assert f"{BASE}a.ics" not in dav.items

    del dav.items[f"{BASE}b.ics"]
    sync.sync_vdir(dav, tmp_path)
    assert not (tmp_path / "b.ics").exists()


def test_local_edit_uploaded(tmp_path):
    dav = FakeDAV()
    dav.add("a", "old")
    sync.sync_vdir(dav, tmp_path)
    (tmp_path / "a.ics").write_text(ICS.format(uid="a", s="edited"))
    assert sync.sync_vdir(dav, tmp_path)["up"] == 1
    assert "edited" in dav.items[f"{BASE}a.ics"][1]


def test_conflict_remote_wins(tmp_path):
    dav = FakeDAV()
    dav.add("a", "old")
    sync.sync_vdir(dav, tmp_path)
    (tmp_path / "a.ics").write_text(ICS.format(uid="a", s="local"))
    dav.add("a", "remote")
    assert sync.sync_vdir(dav, tmp_path)["conflicts"] == 1
    assert "remote" in (tmp_path / "a.ics").read_text()
    assert "local" in (tmp_path / "a.ics.conflict").read_text()


def test_idempotent(tmp_path):
    dav = FakeDAV()
    dav.add("a", "x")
    sync.sync_vdir(dav, tmp_path)
    assert sync.sync_vdir(dav, tmp_path) == {"down": 0, "up": 0, "deleted": 0, "conflicts": 0}


def test_has_internet_false_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError

    monkeypatch.setattr(sync.urllib.request, "urlopen", boom)
    assert sync.has_internet() is False


def test_token_file_mode(tmp_path):
    p = tmp_path / "t.json"
    sync._write_token(p, {"refresh_token": "x"})
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def _conf(tmp_path, auto):
    cfg = tmp_path / "khal.conf"
    cal = tmp_path / "cal"
    cal.mkdir()
    cfg.write_text(
        f"""[calendars]
[[g]]
path = {cal}
[locale]
timeformat = %H:%M
dateformat = %d.%m.
longdateformat = %d.%m.%Y
datetimeformat = %d.%m. %H:%M
longdatetimeformat = %d.%m.%Y %H:%M
[sqlite]
path = {tmp_path}/khal.db
[default]
auto_sync = {auto}
[sync]
client_id = id
client_secret = secret
path = {cal}
token_path = {tmp_path}/tok.json
"""
    )
    return cfg


@pytest.mark.parametrize("auto,called", [(True, 1), (False, 0)])
def test_auto_sync_flag(tmp_path, monkeypatch, auto, called):
    calls = []
    monkeypatch.setattr(sync, "sync_calendars", lambda conf, **k: calls.append(1) or {})
    r = CliRunner().invoke(main_khal, ["-c", str(_conf(tmp_path, auto)), "list"])
    assert r.exit_code == 0, r.output
    assert len(calls) == called


def test_auto_sync_failure_is_nonfatal(tmp_path, monkeypatch):
    def fail(conf, **k):
        raise sync.SyncError("no internet connection")

    monkeypatch.setattr(sync, "sync_calendars", fail)
    r = CliRunner().invoke(main_khal, ["-c", str(_conf(tmp_path, True)), "list"])
    assert r.exit_code == 0
    assert "auto-sync skipped" in r.output


def test_sync_command_offline_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "has_internet", lambda *a, **k: False)
    r = CliRunner().invoke(main_khal, ["-c", str(_conf(tmp_path, False)), "sync"])
    assert r.exit_code == 1


def test_sync_command_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sync,
        "sync_calendars",
        lambda conf, **k: {"down": 2, "up": 0, "deleted": 0, "conflicts": 0, "calendars": 3},
    )
    r = CliRunner().invoke(main_khal, ["-c", str(_conf(tmp_path, False)), "sync"])
    assert r.exit_code == 0
    assert "2 downloaded" in r.output


def test_no_help_autosync(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "sync_calendars", lambda conf, **k: calls.append(1) or {})
    monkeypatch.setattr("sys.argv", ["khal", "list", "--help"])
    CliRunner().invoke(main_khal, ["-c", str(_conf(tmp_path, True)), "list", "--help"])
    assert calls == []


def test_uid_with_at_sign(tmp_path):
    dav = FakeDAV()
    dav.add("02seq3ilrnj448n3creq5uvrb4@google.com", "google style uid")
    assert sync.sync_vdir(dav, tmp_path)["down"] == 1
    assert (tmp_path / "02seq3ilrnj448n3creq5uvrb4@google.com.ics").exists()


def test_readonly_never_pushes(tmp_path):
    dav = FakeDAV()
    dav.add("a", "orig")
    sync.sync_vdir(dav, tmp_path, readonly=True)
    (tmp_path / "a.ics").write_text(ICS.format(uid="a", s="tampered"))
    (tmp_path / "new.ics").write_text(ICS.format(uid="new", s="local only"))
    sync.sync_vdir(dav, tmp_path, readonly=True)
    assert "orig" in (tmp_path / "a.ics").read_text()
    assert f"{BASE}new.ics" not in dav.items
    assert "orig" in dav.items[f"{BASE}a.ics"][1]


def test_multi_calendar_layout(tmp_path, monkeypatch):
    cals = [
        {"id": "me@gmail.com", "name": "Personal", "color": "#112233", "readonly": False},
        {"id": "x#holiday@group.v.calendar.google.com", "name": "Holidays", "color": None, "readonly": True},
    ]
    daves = {}

    def make(cal_id, token, timeout):
        d = daves.setdefault(cal_id, FakeDAV())
        if not d.items:
            d.add("e1", cal_id)
        return d

    monkeypatch.setattr(sync, "access_token", lambda *a, **k: "t")
    monkeypatch.setattr(sync, "list_calendars", lambda *a, **k: cals)
    monkeypatch.setattr(sync, "GoogleCalDAV", make)
    conf = {
        "sync": {
            "client_id": "i",
            "client_secret": "s",
            "path": str(tmp_path),
            "token_path": str(tmp_path / "tok"),
            "calendar_ids": ["all"],
        },
        "default": {"sync_timeout": 5},
    }
    stats = sync.sync_calendars(conf, check_internet=False)
    assert stats["calendars"] == 2 and stats["down"] == 2
    assert (tmp_path / "me@gmail.com" / "e1.ics").exists()
    assert (tmp_path / "me@gmail.com" / "color").read_text() == "#112233"
    assert (tmp_path / "x_holiday@group.v.calendar.google.com" / "displayname").read_text() == "Holidays"
