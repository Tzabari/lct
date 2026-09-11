#!/usr/bin/env python3
"""Renders the battle report, local or hosted, behind the in-game EXPORT button.

Tabletop Simulator's Lua sandbox cannot write files or launch programs; the only
outbound channel it has is WebRequest. So the in-game button POSTs the battle log
here, and this renders the report and hands back a way to see it. Two modes:

    python3 scripts/battle_report_server.py
        Local (the default). Binds 127.0.0.1, writes report/report.html to disk,
        and opens it in a browser -- unchanged from before hosting existed.

    python3 scripts/battle_report_server.py --mode hosted --host 0.0.0.0
        Hosted (what Render runs, via render.yaml). No disk write, no browser.
        Every report also goes into an in-memory ReportStore (scripts/report_store.py)
        with a TTL, and the response carries a /r/<id> view link and a /d/<id>
        download link instead of a local file path.

The store is populated in local mode too -- that is what lets the whole hosted
flow (short ids, TTL, the /r/ and /d/ routes) be exercised against a plain
`python scripts/battle_report_server.py --mode hosted` on a LAN address, with
nothing deployed. See the plan's Step 8 for that walkthrough.

If the server is not running (or not reachable), the in-game button fails softly
and tells you to use export_battle_report.py on a saved game instead -- that path
needs no server at all.
"""

import argparse
import gzip
import importlib
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
import battle_report as BR
import report_store as RS


@dataclass
class Config:
    """Everything a request handler needs to know how to behave.

    Every field here has a literal default -- no reference to a constant
    defined elsewhere in this module -- so this class can sit above renderer()
    without caring what order the rest of the file defines things in. The real
    defaults (DEFAULT_HOST, DEFAULT_PORT, ...) live in the constants block below
    and are applied by resolve_config(), which is the only place environment
    variables are read (see resolve_config's docstring for why that matters).
    """
    host: str = "127.0.0.1"
    port: int = 8787
    mode: str = "local"                 # "local" | "hosted"
    out_dir: Path = Path("report")
    open_browser: bool = True
    dev_reload: bool = True
    public_base_url: str = ""
    token: str = ""
    max_body_bytes: int = 32 * 1024 * 1024
    store_dir: str = ""
    report_max: int = 500
    store_max_bytes: int = 32 * 1024 * 1024
    ttl_seconds: float = 900.0
    ttl_under_load: float = 120.0
    high_watermark: float = 0.8
    download_grace: float = 60.0
    rate_window: float = 600.0
    rate_limit_ip: int = 30
    rate_limit_steam: int = 20


# Read by renderer() below; replaced wholesale by main() before serve_forever().
# A module-level global (rather than a ReportHandler class attribute) because
# renderer() is a free function, called the same way it always has been.
CONFIG = Config()


def renderer():
    """Return battle_report, re-imported so edits land without a restart.

    Local mode is meant to be left running for a whole game -- and, while the
    report is being worked on, across edits to the renderer. A module imported
    once at startup would keep serving the version that was on disk when the
    server booted, which looks exactly like the report "not updating".

    Hosted mode turns this off (CONFIG.dev_reload is False): reloading a module
    out from under a ThreadingHTTPServer that may be mid-request elsewhere is a
    race, and a half-written edit on a dev machine has no business reaching a
    real player's browser. The deployed service is fixed at whatever it booted
    with; ship a new one to change it.

    The reloaded module object is returned rather than read from the global so
    that a caller uses one consistent module for both the call and its `except`
    clause: reloading rebuilds BattleReportError, and the old class would no
    longer catch the new one.
    """
    global BR
    if not CONFIG.dev_reload:
        return BR
    try:
        BR = importlib.reload(BR)
    except Exception as exc:  # noqa: BLE001 - a broken edit must not kill the server
        print(f"  warning: reload failed ({exc}); serving the previously loaded copy")
    return BR


ROOT = Path(__file__).parent.parent
DEFAULT_OUT = ROOT / "report"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Matches BATTLE_REPORT_URL in TTSLUA/global.ttslua.
REPORT_PATH = "/report"

VIEW_PREFIX = "/r/"         # inline view of a stored report
DOWNLOAD_PREFIX = "/d/"     # the same bytes, as an attachment
HEALTH_PATH = "/healthz"    # liveness/wake probe, separate from REPORT_PATH so
                             # the two are distinguishable in logs

# A two-army five-round log is a few hundred KB; local keeps the old generous
# headroom. Hosted is tighter -- above the ~3.2 MB BATTLE_MAX_* worst case, but
# nowhere near the old ceiling, since this one is reachable from the internet.
MAX_BODY_BYTES = 32 * 1024 * 1024
HOSTED_MAX_BODY_BYTES = 4 * 1024 * 1024


def _env_str(name, default):
    return os.environ.get(name, default)


def _env_int(name, default):
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name, default):
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def resolve_config(args):
    """Build a Config from CLI args, then environment variables, then defaults.

    Environment is read only here, never at import: test_report_url_matches_the_
    server_default does `import battle_report_server as SRV` and reads module
    constants straight off it, so import has to stay side-effect-free regardless
    of what happens to be set in the environment a test runs in.

    CLI args win when given; otherwise the matching env var; otherwise the
    literal default. `args.*` fields default to None from argparse specifically
    so "not passed" is distinguishable from "passed as empty string".
    """
    hosted = (args.mode or _env_str("LCT_REPORT_MODE", "local")) == "hosted"
    mode = "hosted" if hosted else "local"

    return Config(
        host=args.host or _env_str("HOST", DEFAULT_HOST),
        port=args.port if args.port is not None else _env_int("PORT", DEFAULT_PORT),
        mode=mode,
        out_dir=args.out if args.out is not None else DEFAULT_OUT,
        # Hosted never opens a browser or writes to disk, regardless of --no-open.
        open_browser=(not hosted) and (not args.no_open),
        dev_reload=not hosted,
        public_base_url=(args.public_base_url if args.public_base_url is not None
                         else _env_str("LCT_PUBLIC_BASE_URL", "")),
        token=args.token if args.token is not None else _env_str("LCT_REPORT_TOKEN", ""),
        max_body_bytes=_env_int("LCT_MAX_BODY_BYTES",
                                HOSTED_MAX_BODY_BYTES if hosted else MAX_BODY_BYTES),
        store_dir=(args.store_dir if args.store_dir is not None
                  else _env_str("LCT_REPORT_STORE_DIR", "")),
        report_max=_env_int("LCT_REPORT_MAX", 500),
        store_max_bytes=_env_int("LCT_REPORT_STORE_MAX_BYTES", 32 * 1024 * 1024),
        ttl_seconds=_env_float("LCT_REPORT_TTL", 900.0),
        ttl_under_load=_env_float("LCT_REPORT_TTL_UNDER_LOAD", 120.0),
        high_watermark=_env_float("LCT_REPORT_STORE_HIGH_WATERMARK", 0.8),
        download_grace=_env_float("LCT_REPORT_DOWNLOAD_GRACE", 60.0),
        rate_window=_env_float("LCT_REPORT_RATE_WINDOW", 600.0),
        rate_limit_ip=_env_int("LCT_REPORT_RATE_LIMIT", 30),
        rate_limit_steam=_env_int("LCT_REPORT_RATE_STEAM_LIMIT", 20),
    )


class RateLimiter:
    """A sliding-window request counter, one bucket per key.

    Not cryptographic, not a real token bucket -- just a per-key list of recent
    hit timestamps, trimmed to the window on each check. At the scale this
    serves (a handful of tables, each exporting a few times a game) that is
    plenty, and it is trivial to reason about and test with an injected clock.
    """

    def __init__(self, limit, window_seconds, now=time.time):
        self.limit = limit
        self.window = window_seconds
        self._now = now
        self._lock = threading.Lock()
        self._hits = {}   # key -> [timestamps], ascending

    def check(self, key):
        """(allowed, retry_after_seconds). retry_after is 0 when allowed."""
        now = self._now()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                while hits and hits[0] <= cutoff:
                    hits.pop(0)
                if not hits:
                    del self._hits[key]
                    hits = None
            if hits and len(hits) >= self.limit:
                return False, max(1, int(hits[0] + self.window - now) + 1)
            self._hits.setdefault(key, []).append(now)
            return True, 0


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _download_filename(meta):
    """battle-report-<map slug>-<date>.html, or a safe fallback."""
    meta = meta or {}
    map_slug = _SLUG_RE.sub("-", str(meta.get("map") or "")).strip("-")[:60]
    date_slug = _SLUG_RE.sub("-", str(meta.get("date") or "")).strip("-")[:10]
    parts = [p for p in (map_slug, date_slug) if p]
    if not parts:
        return "battle-report.html"
    return "battle-report-" + "-".join(parts) + ".html"


def hosted_banner(html_text, report_id):
    """Insert a one-line hosted notice + download link after <div class="app">.

    battle_report.py and the local file:// output stay untouched -- this is
    hosting's own concern, added here rather than taught to the renderer. Uses
    an inline style="" (already required by the CSP for the renderer's own
    dots, see csp_header()) so this needs no separate stylesheet rule; if the
    marker is ever not found (the renderer's markup changed underneath this),
    it degrades to no banner rather than raising mid-request.
    """
    banner = (
        '<div style="padding:8px 16px;background:#222;color:#eee;'
        'font:13px system-ui,sans-serif;border-bottom:1px solid #444">'
        "This is a temporarily hosted copy. "
        f'<a download href="{DOWNLOAD_PREFIX}{report_id}" style="color:#9cf">'
        "Download a permanent copy</a> to keep it working after this link expires."
        "</div>"
    )
    marker = '<div class="app">'
    idx = html_text.find(marker)
    if idx == -1:
        return html_text
    insert_at = idx + len(marker)
    return html_text[:insert_at] + banner + html_text[insert_at:]


def csp_header(nonce):
    """default-src 'none' is satisfiable because the page loads nothing -- the
    only "http" strings render_html ever produces are SVG namespace URLs, which
    are not fetches. style-src stays 'unsafe-inline' for the renderer's own
    style="" dots and this module's banner above; no user data reaches a style
    context, so that is not the same risk an inline *script* would be."""
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


class ReportHandler(BaseHTTPRequestHandler):
    server_version = "LCTBattleReport/1.0"
    timeout = 30  # a slowloris floor; also bounds a stuck client's socket

    # Replaced wholesale by main() before serve_forever(); the bare Config()/None
    # values here exist only so the class is importable (and its methods
    # unit-testable) before that happens.
    config = Config()
    store = None
    ip_limiter = None
    steam_limiter = None

    # -- small helpers ----------------------------------------------------

    def _respond(self, code, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, entry, extra_headers=None):
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = entry.gz if accepts_gzip else gzip.decompress(entry.gz)
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if accepts_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Below the shortest possible remaining life (the post-download grace
        # period) and never `immutable` -- a cached 404 for a link that just
        # expired would be worse than a redundant request.
        self.send_header("Cache-Control", "private, max-age=30")
        self.send_header("Content-Security-Policy", csp_header(entry.nonce))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _base_url(self):
        if self.config.public_base_url:
            return self.config.public_base_url.rstrip("/")
        # Host is client-controlled; prefer the explicit config above. This
        # fallback is what makes local mode (and the LAN rehearsal in Step 8)
        # work with no configuration at all.
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("Host") or f"{self.config.host}:{self.config.port}"
        return f"{proto}://{host}"

    def _client_ip(self):
        # Rightmost X-Forwarded-For: on Render the proxy appends the real
        # client hop, and everything to its left is client-supplied and
        # therefore spoofable. Falls back to the raw socket peer when there is
        # no proxy in front (local mode, and the LAN rehearsal in Step 8).
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
        return self.client_address[0]

    def _host_steam_id(self):
        # Opaque and unvalidated by format on purpose -- TTS documents steam_id
        # only as "unique to each player's Steam account" (a string), with no
        # guaranteed shape. Anything empty, too long, or non-printable is
        # treated as absent rather than rejected: the Steam bucket just doesn't
        # apply, and the request is judged on IP alone, same as before this
        # header existed.
        raw = (self.headers.get("X-LCT-Steam-Id") or "").strip()
        if not raw or len(raw) > 32 or not raw.isprintable():
            return ""
        return raw

    def _check_rate_limit(self):
        """Both buckets must allow. See the plan's "Rate limiting by Steam ID,
        not just IP" section for why two independent buckets rather than one
        composite key."""
        allowed, retry_after = self.ip_limiter.check(self._client_ip())
        if not allowed:
            return False, retry_after
        steam_id = self._host_steam_id()
        if steam_id:
            allowed, retry_after = self.steam_limiter.check(steam_id)
            if not allowed:
                return False, retry_after
        return True, 0

    def _serve_stored(self, report_id, download):
        status = self.store.status(report_id)
        if status != "ok":
            reason = "report expired" if status == "expired" else "report not found"
            self._respond(404, {"ok": False, "error": reason})
            return
        entry = self.store.get(report_id)
        if entry is None:   # expired between status() and get(): a benign race
            self._respond(404, {"ok": False, "error": "report expired"})
            return
        extra = {}
        if download:
            extra["Content-Disposition"] = (
                f'attachment; filename="{_download_filename(entry.meta)}"')
        self._send_html(200, entry, extra)
        if download:
            self.store.mark_downloaded(report_id)

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/", REPORT_PATH):
            self._respond(200, {"ok": True, "service": "lct-battle-report",
                               "mode": self.config.mode})
        elif path == HEALTH_PATH:
            self._respond(200, {"ok": True})
        elif path.startswith(VIEW_PREFIX):
            self._serve_stored(path[len(VIEW_PREFIX):], download=False)
        elif path.startswith(DOWNLOAD_PREFIX):
            self._serve_stored(path[len(DOWNLOAD_PREFIX):], download=True)
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path != REPORT_PATH:
            self._respond(404, {"ok": False, "error": "not found"})
            return

        if self.config.token and not secrets.compare_digest(
                self.headers.get("X-LCT-Token", ""), self.config.token):
            self._respond(401, {"ok": False, "error": "missing or bad token"})
            return

        allowed, retry_after = self._check_rate_limit()
        if not allowed:
            self._respond(429, {"ok": False, "error": "rate limit exceeded - try again shortly"},
                         extra_headers={"Retry-After": str(retry_after)})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, {"ok": False, "error": "bad Content-Length"})
            return
        if length <= 0:
            self._respond(400, {"ok": False, "error": "empty body"})
            return
        if length > self.config.max_body_bytes:
            self._respond(413, {"ok": False, "error": "battle log too large"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._respond(400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return

        br = renderer()
        try:
            log = br.load_log(payload)
            report = br.build_report(log)
        except br.BattleReportError as exc:
            print(f"  rejected: {exc}")
            self._respond(422, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - never take the server down mid-game
            print(f"  failed: {exc}")
            self._respond(500, {"ok": False, "error": str(exc)})
            return

        nonce = secrets.token_urlsafe(16)
        html_text = br.render_html(report, script_nonce=nonce)
        game = report.get("game") or {}
        meta = {"map": game.get("map") or "",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "snapshots": len(log["snaps"])}

        # Minted here, not by store.put(), because the hosted banner's download
        # link needs the id baked into the very HTML that becomes the stored
        # entry -- see report_store.put()'s docstring for report_id.
        report_id = secrets.token_urlsafe(self.store.id_bytes)
        stored_html = (hosted_banner(html_text, report_id)
                       if self.config.mode == "hosted" else html_text)
        try:
            self.store.put(stored_html, nonce=nonce, meta=meta, report_id=report_id)
        except RS.StoreFull:
            self._respond(
                503,
                {"ok": False,
                 "error": "report server is busy right now - try again in a few minutes"},
                extra_headers={"Retry-After": "30"})
            return

        entry = self.store.get(report_id)
        expires_in = max(0, int(entry.expires - time.time())) if entry else 0

        html_path = None
        if self.config.mode == "local":
            html_path = br.write_report(log, self.config.out_dir)
            if self.config.open_browser:
                webbrowser.open(html_path.resolve().as_uri())

        base = self._base_url()
        print(f"  stored report {report_id} ({len(log['snaps'])} snapshot(s))"
              + (f", wrote {html_path}" if html_path else ""))
        self._respond(200, {
            "ok": True,
            "snapshots": len(log["snaps"]),
            "mode": self.config.mode,
            "path": str(html_path) if html_path else None,
            "id": report_id,
            "url": f"{base}{VIEW_PREFIX}{report_id}",
            "download_url": f"{base}{DOWNLOAD_PREFIX}{report_id}",
            "expires_in": expires_in,
        })

    def log_message(self, fmt, *args):
        print(f"[battle-report] {fmt % args}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=None,
                        help=f"Bind address (default: {DEFAULT_HOST}, or $HOST).")
    parser.add_argument("-p", "--port", type=int, default=None,
                        help=f"Port to listen on (default: {DEFAULT_PORT}, or $PORT).")
    parser.add_argument("--mode", choices=("local", "hosted"), default=None,
                        help="local: writes report/ and opens a browser. hosted: no disk, "
                             "no browser, replies with /r/ and /d/ links instead. "
                             "(default: local, or $LCT_REPORT_MODE)")
    parser.add_argument("-o", "--out", type=Path, default=None,
                        help=f"Local-mode output directory (default: {DEFAULT_OUT}).")
    parser.add_argument("--no-open", action="store_true",
                        help="Local mode: write the report but do not open a browser.")
    parser.add_argument("--public-base-url", default=None,
                        help="Absolute base URL for /r/ and /d/ links (default: "
                             "$LCT_PUBLIC_BASE_URL, else derived from request headers).")
    parser.add_argument("--token", default=None,
                        help="Shared secret required as X-LCT-Token on POST (default: "
                             "$LCT_REPORT_TOKEN; empty disables the check).")
    parser.add_argument("--store-dir", default=None,
                        help="Mirror reports to disk here for durability across restarts "
                             "(default: $LCT_REPORT_STORE_DIR; unset means memory only).")
    args = parser.parse_args(argv)

    global CONFIG
    CONFIG = resolve_config(args)

    store = RS.ReportStore(
        directory=CONFIG.store_dir or None,
        max_reports=CONFIG.report_max,
        max_bytes=CONFIG.store_max_bytes,
        ttl_seconds=CONFIG.ttl_seconds,
        ttl_under_load=CONFIG.ttl_under_load,
        high_watermark=CONFIG.high_watermark,
        download_grace=CONFIG.download_grace,
    )

    ReportHandler.config = CONFIG
    ReportHandler.store = store
    ReportHandler.ip_limiter = RateLimiter(CONFIG.rate_limit_ip, CONFIG.rate_window)
    ReportHandler.steam_limiter = RateLimiter(CONFIG.rate_limit_steam, CONFIG.rate_window)

    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), ReportHandler)
    print(f"LCT battle report server ({CONFIG.mode}) listening on "
          f"http://{CONFIG.host}:{CONFIG.port}{REPORT_PATH}")
    if CONFIG.mode == "local":
        print(f"Reports will also be written to {CONFIG.out_dir}")
    print("Press EXPORT REPORT in TTS. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
