"""``khal find``: filter events by date, time, text, tag and calendar.

Every filter is optional and can be combined with the others. The output is
either meant for humans (default) or for scripts and small LLMs (``--llm``):
one line of flat JSON with ISO dates and 24h times.
"""

import datetime as dt
import json
import re
from typing import Optional

from . import parse_datetime
from .exceptions import DateTimeParseError, FatalError

DEFAULT_DAYS = 7
DEFAULT_LIMIT = 50
MAX_DAYS = 366
NOTES_LENGTH = 200

PARTS_OF_DAY = {
    "morning": (dt.time(5), dt.time(12)),
    "afternoon": (dt.time(12), dt.time(17)),
    "evening": (dt.time(17), dt.time(22)),
    "night": (dt.time(22), dt.time(5)),  # wraps around midnight
}


class FindError(FatalError):
    """a problem with the query; `extra` is added to the --llm error output"""

    def __init__(self, message: str, **extra) -> None:
        super().__init__(message)
        self.extra = extra


# --------------------------------------------------------------------------
# parsing of the arguments
# --------------------------------------------------------------------------
def parse_day(text: str, locale) -> dt.date:
    """today, tomorrow, yesterday, a weekday name, YYYY-MM-DD or a date in the locale format"""
    value = text.strip()
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        pass
    low = value.lower()
    if low in ("today", "tomorrow", "yesterday"):
        return parse_datetime.calc_day(low).date()
    try:
        return parse_datetime.calc_day(low).date()  # monday, tue, ...
    except Exception:
        pass
    try:
        parsed, _ = parse_datetime.guessdatetimefstr([value], locale, in_future=False)
        return parsed.date()
    except (DateTimeParseError, ValueError, IndexError):
        raise FindError(
            f'Cannot understand the date "{text}". Use YYYY-MM-DD, today, tomorrow, '
            "yesterday or a weekday name like friday."
        )


def parse_clock(text: str) -> dt.time:
    """14:30, 9, 9am, 2:15pm"""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\s*", text.lower())
    if not m:
        raise FindError(f'Cannot understand the time "{text}". Use HH:MM (24h), like 14:30.')
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise FindError(f'"{text}" is not a valid time of day.')
    return dt.time(hour, minute)


def resolve_range(date, date_from, date_to, days, locale, today=None):
    """returns (first_day, last_day), both inclusive; see the defaults in `khal find --help`"""
    today = today or dt.date.today()
    if date and (date_from or date_to or days):
        raise FindError("--date cannot be combined with --from, --to or --days.")
    if date_to and days:
        raise FindError("Use either --to or --days, not both.")
    if date:
        day = parse_day(date, locale)
        return day, day
    first = parse_day(date_from, locale) if date_from else today
    if date_to:
        last = parse_day(date_to, locale)
    else:
        n = days if days else DEFAULT_DAYS
        if n < 1:
            raise FindError("--days must be at least 1.")
        last = first + dt.timedelta(days=n - 1)
    if last < first:
        raise FindError(f"The end ({last}) is before the start ({first}).")
    if (last - first).days + 1 > MAX_DAYS:
        raise FindError(f"The range is too long, the maximum is {MAX_DAYS} days.")
    return first, last


def _in_window(t: dt.time, start: dt.time, end: dt.time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps around midnight


# --------------------------------------------------------------------------
# collecting and filtering
# --------------------------------------------------------------------------
def _words(text: Optional[str]) -> list:
    return [w for w in (text or "").lower().split() if w]


def _record(event, local) -> dict:
    tags = [t.strip() for t in str(event.categories or "").split(",") if t.strip()]
    rec = {
        "title": str(event.summary or ""),
        "calendar": str(event.calendar),
        "location": str(event.location or ""),
        "notes": str(event.description or ""),
        "tags": tags,
        "repeats": bool(event.recurring),
        "uid": str(event.uid),
        "all_day": bool(event.allday),
    }
    if event.allday:
        rec.update(start_dt=None, date=event.start, end_date=event.end, start=None, end=None)
    else:
        s, e = event.start_local, event.end_local
        rec.update(
            start_dt=s, date=s.date(), end_date=e.date(), start=s.time().replace(tzinfo=None),
            end=e.time().replace(tzinfo=None),
        )
    return rec


def collect(collection, locale, first: dt.date, last: dt.date) -> list:
    local = locale["local_timezone"]
    start = dt.datetime.combine(first, dt.time.min)
    end = dt.datetime.combine(last, dt.time.max)
    events = list(collection.get_localized(local.localize(start), local.localize(end)))
    events += list(collection.get_floating(start, end))
    records = [_record(e, local) for e in events]
    records.sort(key=lambda r: (r["date"], not r["all_day"], r["start"] or dt.time.min, r["title"]))
    return records


def apply_filters(
    records, text=None, title=None, tags=(), calendars=(), after=None, before=None,
    when=None, only_all_day=False,
) -> list:
    window = None
    if when:
        if after or before:
            raise FindError("--when cannot be combined with --after or --before.")
        window = PARTS_OF_DAY[when]
    elif after or before:
        window = (parse_clock(after) if after else dt.time.min,
                  parse_clock(before) if before else dt.time.max)
    text_words, title_words = _words(text), _words(title)
    want_tags = [t.lower() for t in tags]
    want_cals = [c.lower() for c in calendars]

    out = []
    for r in records:
        if only_all_day and not r["all_day"]:
            continue
        if window is not None and (r["all_day"] or not _in_window(r["start"], *window)):
            continue
        if want_cals and not any(c in r["calendar"].lower() for c in want_cals):
            continue
        if want_tags and not any(t in (x.lower() for x in r["tags"]) for t in want_tags):
            continue
        if title_words and not all(w in r["title"].lower() for w in title_words):
            continue
        if text_words:
            hay = " ".join(
                [r["title"], r["notes"], r["location"], " ".join(r["tags"]), r["calendar"]]
            ).lower()
            if not all(w in hay for w in text_words):
                continue
        out.append(r)
    return out


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------
def _describe_filters(f: dict) -> str:
    parts = []
    for key in ("text", "title"):
        if f.get(key):
            parts.append(("matching" if key == "text" else key) + f" '{f[key]}'")
    if f.get("tag"):
        parts.append("tag " + "/".join(f["tag"]))
    if f.get("calendar"):
        parts.append("calendar " + "/".join(f["calendar"]))
    if f.get("when"):
        parts.append(f["when"])
    if f.get("after"):
        parts.append("after " + f["after"])
    if f.get("before"):
        parts.append("before " + f["before"])
    if f.get("all_day"):
        parts.append("all-day only")
    return ", ".join(parts)


def _summary(total, returned, first, last, filters) -> str:
    span = str(first) if first == last else f"{first} to {last}"
    what = "1 event" if total == 1 else f"{total} events"
    if total == 0:
        what = "No events"
    text = f"{what} on {span}" if first == last else f"{what} from {span}"
    desc = _describe_filters(filters)
    text += f" ({desc})" if desc else ""
    if returned < total:
        text += f", showing the first {returned}"
    return text + "."


def llm_event(n: int, r: dict) -> dict:
    ev = {
        "n": n,
        "title": r["title"],
        "date": r["date"].isoformat(),
        "weekday": r["date"].strftime("%A"),
        "all_day": r["all_day"],
        "start": None if r["all_day"] else r["start"].strftime("%H:%M"),
        "end": None if r["all_day"] else r["end"].strftime("%H:%M"),
        "calendar": r["calendar"],
        "location": r["location"],
        "tags": r["tags"],
        "repeats": r["repeats"],
        "uid": r["uid"],
    }
    if r["end_date"] != r["date"]:
        ev["end_date"] = r["end_date"].isoformat()
    if r["notes"]:
        notes = " ".join(r["notes"].split())
        ev["notes"] = notes[:NOTES_LENGTH] + ("..." if len(notes) > NOTES_LENGTH else "")
    return ev


def format_llm(records, total, first, last, filters) -> str:
    result = {
        "ok": True,
        "summary": _summary(total, len(records), first, last, filters),
        "range": {"from": first.isoformat(), "to": last.isoformat()},
        "filters": {k: v for k, v in filters.items() if v},
        "total": total,
        "returned": len(records),
        "truncated": len(records) < total,
        "events": [llm_event(i, r) for i, r in enumerate(records, 1)],
    }
    return json.dumps(result, ensure_ascii=False)


def format_human(records, total, first, last, filters, locale) -> str:
    lines = [_summary(total, len(records), first, last, filters)]
    if not records:
        return lines[0]
    timefmt, datefmt = locale["timeformat"], locale["longdateformat"]
    current = None
    for r in records:
        if r["date"] != current:
            current = r["date"]
            lines.append("")
            lines.append(f"{current.strftime('%A')} {current.strftime(datefmt)}")
        if r["all_day"]:
            when = "all day"
            if r["end_date"] != r["date"]:
                when += f" until {r['end_date'].strftime(datefmt)}"
        else:
            when = f"{r['start'].strftime(timefmt)}-{r['end'].strftime(timefmt)}"
            if r["end_date"] != r["date"]:
                when += f" ({r['end_date'].strftime(datefmt)})"
        line = f"  {when}  {r['title']}"
        extras = [f"[{r['calendar']}]"]
        if r["location"]:
            extras.append(f"@ {r['location']}")
        extras += [f"#{t}" for t in r["tags"]]
        if r["repeats"]:
            extras.append("(repeats)")
        lines.append(line + "  " + " ".join(extras))
    if len(records) < total:
        lines += ["", f"... {total - len(records)} more, use --limit or narrow the search."]
    return "\n".join(lines)


def error_llm(error: FatalError) -> str:
    out = {"ok": False, "error": str(error)}
    out.update(getattr(error, "extra", {}))
    return json.dumps(out, ensure_ascii=False)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def run(
    collection,
    conf,
    *,
    text=None,
    title=None,
    tags=(),
    calendars=(),
    date=None,
    date_from=None,
    date_to=None,
    days=None,
    after=None,
    before=None,
    when=None,
    only_all_day=False,
    limit=DEFAULT_LIMIT,
    llm=False,
    list_calendars=False,
    list_tags=False,
) -> str:
    locale = conf["locale"]
    names = sorted(str(n) for n in collection.names)

    if list_calendars:
        if llm:
            return json.dumps({"ok": True, "calendars": names}, ensure_ascii=False)
        return "\n".join(names) if names else "No calendars configured."

    for wanted in calendars:
        if not any(wanted.lower() in n.lower() for n in names):
            raise FindError(f'No calendar matches "{wanted}".', available_calendars=names)

    first, last = resolve_range(date, date_from, date_to, days, locale)
    records = collect(collection, locale, first, last)

    if list_tags:
        found = sorted({t for r in records for t in r["tags"]}, key=str.lower)
        if llm:
            return json.dumps(
                {"ok": True, "range": {"from": str(first), "to": str(last)}, "tags": found},
                ensure_ascii=False,
            )
        return "\n".join(found) if found else f"No tags used from {first} to {last}."

    matches = apply_filters(
        records, text, title, tags, calendars, after, before, when, only_all_day
    )
    total = len(matches)
    shown = matches[:limit] if limit and limit > 0 else matches
    filters = {
        "text": text, "title": title, "tag": list(tags), "calendar": list(calendars),
        "after": after, "before": before, "when": when, "all_day": only_all_day,
    }
    if llm:
        return format_llm(shown, total, first, last, filters)
    return format_human(shown, total, first, last, filters, locale)
