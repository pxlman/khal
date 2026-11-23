"""Native two-way sync between a local vdir and Google Calendar (CalDAV + OAuth2).

No external tools (vdirsyncer, gogcli) are needed; only the standard library.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger("khal")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"
CALDAV_BASE = "https://apidata.googleusercontent.com/caldav/v2/"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
CONNECTIVITY_URL = "https://clients3.google.com/generate_204"
STATE_FILE = ".khal_gsync_state.json"

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------
# connectivity
# --------------------------------------------------------------------------
def has_internet(timeout: float = 2.5) -> bool:
    """Fast-fail connectivity probe. Returns False on any error."""
    try:
        with urllib.request.urlopen(CONNECTIVITY_URL, timeout=timeout) as resp:
            return resp.status == 204
    except Exception:
        return False


# --------------------------------------------------------------------------
# OAuth2 (loopback redirect + PKCE)
# --------------------------------------------------------------------------
def _post_form(url: str, data: dict, timeout: float) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SyncError(f"OAuth error {e.code}: {e.read().decode(errors='replace')}") from e
    except OSError as e:
        raise SyncError(f"OAuth request failed: {e}") from e


def _write_token(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(token, f)
    os.replace(tmp, path)


def login(client_id: str, client_secret: str, token_path: Path, open_browser: bool = True) -> None:
    """Interactive OAuth login; stores a refresh token at token_path (mode 600)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"khal: login complete, you can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print(f"Open this URL to authorize khal:\n\n{url}\n")
    if open_browser:
        webbrowser.open(url)

    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    t.join(timeout=300)
    server.server_close()

    if result.get("state") != state or "code" not in result:
        raise SyncError(f"Login failed or timed out ({result.get('error', 'no code received')}).")

    tok = _post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=30,
    )
    if "refresh_token" not in tok:
        raise SyncError("Google returned no refresh token; revoke khal's access and retry.")
    _write_token(
        token_path,
        {
            "refresh_token": tok["refresh_token"],
            "access_token": tok["access_token"],
            "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        },
    )


def access_token(client_id: str, client_secret: str, token_path: Path, timeout: float) -> str:
    try:
        token = json.loads(Path(token_path).read_text())
    except FileNotFoundError as e:
        raise SyncError("Not logged in. Run `khal sync --login` first.") from e
    if token.get("expires_at", 0) - 60 > time.time():
        return token["access_token"]
    fresh = _post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=timeout,
    )
    token["access_token"] = fresh["access_token"]
    token["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
    _write_token(Path(token_path), token)
    return token["access_token"]


# --------------------------------------------------------------------------
# CalDAV client
# --------------------------------------------------------------------------
class GoogleCalDAV:
    def __init__(self, calendar_id: str, token: str, timeout: float) -> None:
        self.base = f"{CALDAV_BASE}{urllib.parse.quote(calendar_id, safe='@')}/events/"
        self.token = token
        self.timeout = timeout

    def _url(self, href: str) -> str:
        return "https://apidata.googleusercontent.com" + urllib.parse.quote(href, safe="/@:")

    def _request(self, method: str, url: str, body: Optional[bytes] = None, headers=None):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 412:
                return 412, dict(e.headers), b""
            if e.code == 404:
                return 404, dict(e.headers), b""
            raise SyncError(f"{method} {url}: HTTP {e.code} {e.read()[:300]!r}") from e
        except OSError as e:
            raise SyncError(f"{method} {url}: {e}") from e

    def list_etags(self) -> dict:
        """href -> etag for every event"""
        body = (
            b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop>'
            b"</d:propfind>"
        )
        _, _, data = self._request(
            "PROPFIND",
            self.base,
            body,
            {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        out = {}
        for resp in ET.fromstring(data).findall("d:response", NS):
            href = resp.findtext("d:href", namespaces=NS)
            etag = resp.findtext(".//d:getetag", namespaces=NS)
            if href and etag and href.endswith(".ics"):
                out[urllib.parse.unquote(href)] = etag
        return out

    def multiget(self, hrefs: list) -> dict:
        """href -> (etag, ics text)"""
        out: dict = {}
        for i in range(0, len(hrefs), 50):
            chunk = hrefs[i : i + 50]
            items = "".join(f"<d:href>{urllib.parse.quote(h)}</d:href>" for h in chunk)
            body = (
                '<?xml version="1.0"?><c:calendar-multiget xmlns:d="DAV:" '
                'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/>'
                f"</d:prop>{items}</c:calendar-multiget>"
            ).encode()
            _, _, data = self._request(
                "REPORT",
                self.base,
                body,
                {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            )
            for resp in ET.fromstring(data).findall("d:response", NS):
                href = urllib.parse.unquote(resp.findtext("d:href", namespaces=NS) or "")
                etag = resp.findtext(".//d:getetag", namespaces=NS)
                ics = resp.findtext(".//c:calendar-data", namespaces=NS)
                if href and etag and ics:
                    out[href] = (etag, ics)
        return out

    def put(self, href: str, ics: str, etag: Optional[str]):
        """returns (status, new_etag)"""
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers["If-Match" if etag else "If-None-Match"] = etag or "*"
        status, hdrs, _ = self._request("PUT", self._url(href), ics.encode(), headers)
        return status, hdrs.get("ETag") or hdrs.get("Etag")

    def delete(self, href: str, etag: str) -> int:
        status, _, _ = self._request("DELETE", self._url(href), None, {"If-Match": etag})
        return status


# --------------------------------------------------------------------------
# sync engine
# --------------------------------------------------------------------------
def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _load_state(vdir: Path) -> dict:
    try:
        return json.loads((vdir / STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(vdir: Path, state: dict) -> None:
    tmp = vdir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, vdir / STATE_FILE)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def sync_vdir(dav: GoogleCalDAV, vdir: Path, readonly: bool = False) -> dict:
    """Two-way sync of a single vdir (pull only if readonly). Returns counters."""
    vdir.mkdir(parents=True, exist_ok=True)
    state = _load_state(vdir)  # name -> {href, etag, hash}
    stats = {"down": 0, "up": 0, "deleted": 0, "conflicts": 0}

    remote = dav.list_etags()  # href -> etag
    by_href = {href: etag for href, etag in remote.items()}
    local = {}
    for p in vdir.glob("*.ics"):
        local[p.name] = _hash(p.read_text())

    names = set(local) | set(state)
    for href in by_href:
        names.add(os.path.basename(href))

    base_path = urllib.parse.unquote(urllib.parse.urlparse(dav.base).path)
    to_fetch: list = []
    plan: dict = {}

    for name in sorted(names):
        s = state.get(name)
        href = (s or {}).get("href") or base_path + name
        r_etag = by_href.get(href)
        l_hash = local.get(name)

        remote_changed = (r_etag != s["etag"]) if s else r_etag is not None
        local_changed = (l_hash != s["hash"]) if s else l_hash is not None
        if not remote_changed and not local_changed:
            continue

        if remote_changed and local_changed:
            if r_etag is None and l_hash is None:
                state.pop(name, None)
                continue
            plan[name] = ("conflict", href)
            if r_etag is not None:
                to_fetch.append(href)
        elif remote_changed:
            if r_etag is None:
                plan[name] = ("local_delete", href)
            else:
                plan[name] = ("download", href)
                to_fetch.append(href)
        else:
            plan[name] = ("remote_delete", href) if l_hash is None else ("upload", href)

    if readonly:  # never push; restore the remote version instead
        for name in list(plan):
            action, href = plan[name]
            if action in ("upload", "remote_delete"):
                if by_href.get(href):
                    plan[name] = ("download", href)
                    to_fetch.append(href)
                else:
                    del plan[name]

    fetched = dav.multiget(to_fetch) if to_fetch else {}

    for name, (action, href) in plan.items():
        path = vdir / name
        s = state.get(name)
        if action == "download":
            etag, ics = fetched[href]
            _write_atomic(path, ics)
            state[name] = {"href": href, "etag": etag, "hash": _hash(path.read_text())}
            stats["down"] += 1
        elif action == "local_delete":
            path.unlink(missing_ok=True)
            state.pop(name, None)
            stats["deleted"] += 1
        elif action == "remote_delete":
            status = dav.delete(href, s["etag"])
            if status in (200, 204, 404):
                state.pop(name, None)
                stats["deleted"] += 1
            elif status == 412:
                logger.warning("khal sync: %s changed remotely while deleting; kept remote", name)
                state.pop(name, None)
        elif action == "upload":
            ics = path.read_text()
            status, etag = dav.put(href, ics, s["etag"] if s else None)
            if status == 412:
                logger.warning("khal sync: %s changed remotely; will resolve next sync", name)
                continue
            if status not in (200, 201, 204):
                raise SyncError(f"upload of {name} failed with HTTP {status}")
            got = dav.multiget([href]).get(href)
            if got:
                etag, canonical = got
                _write_atomic(path, canonical)
                ics = canonical
            state[name] = {"href": href, "etag": etag or "", "hash": _hash(ics)}
            stats["up"] += 1
        elif action == "conflict":
            # remote wins; local version kept next to it without an .ics suffix
            if path.exists():
                _write_atomic(vdir / (name + ".conflict"), path.read_text())
            if href in fetched:
                etag, ics = fetched[href]
                _write_atomic(path, ics)
                state[name] = {"href": href, "etag": etag, "hash": _hash(ics)}
            else:
                path.unlink(missing_ok=True)
                state.pop(name, None)
            stats["conflicts"] += 1

    _save_state(vdir, state)
    return stats


def list_calendars(token: str, timeout: float) -> list:
    """All calendars of the account: dicts with id, name, color, readonly."""
    out: list = []
    page = None
    while True:
        url = CALENDAR_LIST_URL + ("?pageToken=" + page if page else "")
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SyncError(f"calendar list: HTTP {e.code} {e.read()[:300]!r}") from e
        except OSError as e:
            raise SyncError(f"calendar list: {e}") from e
        for item in data.get("items", []):
            role = item.get("accessRole", "reader")
            if role == "freeBusyReader":
                continue  # no event details available
            out.append(
                {
                    "id": item["id"],
                    "name": item.get("summaryOverride") or item.get("summary") or item["id"],
                    "color": item.get("backgroundColor"),
                    "readonly": role == "reader",
                }
            )
        page = data.get("nextPageToken")
        if not page:
            return out


def _dirname(calendar_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", calendar_id)


def sync_calendars(conf: dict, timeout: Optional[float] = None, check_internet: bool = True) -> dict:
    """Sync every selected Google calendar into its own vdir below [sync] path.

    Raises SyncError (after syncing what it can) if any calendar failed.
    """
    sc = conf["sync"]
    for key in ("client_id", "client_secret", "path"):
        if not sc.get(key):
            raise SyncError(f"[sync] {key} is not set in the khal config")
    timeout = timeout if timeout is not None else conf["default"]["sync_timeout"]
    if check_internet and not has_internet(min(timeout, 2.5)):
        raise SyncError("no internet connection")
    token = access_token(sc["client_id"], sc["client_secret"], Path(sc["token_path"]), timeout)

    wanted = [c for c in (sc.get("calendar_ids") or ["all"]) if c]
    known = list_calendars(token, timeout)
    if "all" in wanted:
        calendars = known
    else:
        meta = {c["id"]: c for c in known}
        calendars = [
            meta.get(w, {"id": w, "name": w, "color": None, "readonly": False}) for w in wanted
        ]

    root = Path(sc["path"])
    total = {"down": 0, "up": 0, "deleted": 0, "conflicts": 0}
    errors = []
    for cal in calendars:
        vdir = root / _dirname(cal["id"])
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "displayname").write_text(cal["name"])
        if cal.get("color"):
            (vdir / "color").write_text(cal["color"])
        try:
            stats = sync_vdir(GoogleCalDAV(cal["id"], token, timeout), vdir, cal["readonly"])
        except SyncError as error:
            errors.append(f"{cal['name']}: {error}")
            continue
        for k in total:
            total[k] += stats[k]
    total["calendars"] = len(calendars) - len(errors)
    if errors:
        raise SyncError("; ".join(errors))
    return total
