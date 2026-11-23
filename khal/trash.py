"""A small undo store: every event version khal deletes or changes is kept here.

It lives outside of the calendar directories on purpose, so syncing and
calendar discovery never see it.
"""

import json
import os
import time
from pathlib import Path

KEEP = 200


def trash_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "khal" / "trash"


def save(calendar: str, href: str, ics: str, action: str, summary: str, uid: str) -> Path:
    """remember the event as it was before `action` ("deleted" or "changed")"""
    d = trash_dir()
    d.mkdir(parents=True, exist_ok=True)
    entry = d / f"{time.time_ns()}.json"
    entry.write_text(
        json.dumps(
            {
                "calendar": calendar,
                "href": href,
                "ics": ics,
                "action": action,
                "summary": summary,
                "uid": uid,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    )
    for old in sorted(d.glob("*.json"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return entry


def entries() -> list:
    """newest first, as (path, data) tuples"""
    out = []
    for p in sorted(trash_dir().glob("*.json"), reverse=True) if trash_dir().exists() else []:
        try:
            out.append((p, json.loads(p.read_text())))
        except (OSError, json.JSONDecodeError):
            continue
    return out
