"""In-memory store for hosted battle reports, keyed by a short, unguessable id.

The hosted server (scripts/battle_report_server.py) has nowhere durable to put a
rendered report: Render's free tier has no persistent disk, and the process can be
recycled at any time. So a report lives here -- gzip-compressed, in a dict, behind
a TTL -- and the id handed back to the mod (`/r/<id>`, `/d/<id>`) is the only way
to reach it. This module is deliberately HTTP-free so it can be unit tested without
a server: put/get/mark_downloaded/prune are the whole surface.

Three TTL mechanisms compose rather than replace one another:

  * a base TTL (`ttl_seconds`, default 15 minutes) -- generous, because a report
    a player wants to show a teammate should still be alive when they get around
    to it;
  * a shorter TTL (`ttl_under_load`) assigned to *new* entries once the store is
    at or above `high_watermark` full -- so a burst of exports raises turnover
    before anyone hits the hard cap, rather than the store just filling up;
  * a post-download cap (`mark_downloaded`) -- once a viewer has the permanent,
    self-contained file, the ephemeral copy has done its job and can free its slot
    early instead of sitting on its full reservation.

None of that is what makes a full store answer "no" rather than delete someone
else's report to make room. `put()` never evicts a live entry: at capacity it
raises StoreFull, which the server turns into HTTP 503 -- busy, try again -- and
that is the whole eviction story. Turnover is TTL, not force.
"""

import gzip
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# 8 url-safe chars per id at the default id_bytes=6 (48 bits) -- short enough to
# retype off a chat line, long enough that guessing one behind a rate limiter is
# not a practical attack. The range is generous only so a caller passing a custom
# id_bytes (or a hand-written test id) is not surprised by it.
_VALID_ID = re.compile(r"\A[A-Za-z0-9_-]{6,64}\Z")


class StoreFull(Exception):
    """Raised by put() when live reports fill the store. Maps to HTTP 503."""


@dataclass
class Entry:
    """One stored report. `expires` is mutable -- mark_downloaded() lowers it."""
    gz: bytes
    nonce: str
    created: float
    expires: float
    meta: dict = field(default_factory=dict)


class ReportStore:
    """Gzip-compressed reports in memory, with TTL eviction and a hard size cap.

    Thread-safe: ThreadingHTTPServer serves one thread per connection, and two
    exports (or an export racing a download) can land at the same moment.
    """

    def __init__(self, directory=None, max_reports=500, ttl_seconds=900,
                 ttl_under_load=120, high_watermark=0.8, download_grace=60,
                 max_bytes=32 * 1024 * 1024, id_bytes=6, now=time.time):
        self.directory = Path(directory) if directory else None
        self.max_reports = max_reports
        self.ttl_seconds = ttl_seconds
        self.ttl_under_load = ttl_under_load
        self.high_watermark = high_watermark
        self.download_grace = download_grace
        self.max_bytes = max_bytes
        self.id_bytes = id_bytes
        self._now = now
        self._lock = threading.Lock()
        self._entries = {}      # id -> Entry, live reports only
        self._total_bytes = 0
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    # -- capacity -------------------------------------------------------

    def __len__(self):
        return len(self._entries)

    def _occupancy_locked(self):
        """The higher of the two fill fractions -- count or bytes."""
        by_count = len(self._entries) / self.max_reports
        by_bytes = self._total_bytes / self.max_bytes
        return max(by_count, by_bytes)

    # -- writing ----------------------------------------------------------

    def put(self, html, nonce="", meta=None):
        """Store a rendered report and return its id.

        Raises StoreFull when the store is at capacity after pruning expired
        entries -- it is the caller's job to turn that into a 503, not this
        method's job to evict a live report to make room.
        """
        gz = gzip.compress(html.encode("utf-8"))
        size = len(gz)
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            if (len(self._entries) >= self.max_reports
                    or self._total_bytes + size > self.max_bytes):
                raise StoreFull(
                    f"store full ({len(self._entries)}/{self.max_reports} reports, "
                    f"{self._total_bytes + size}/{self.max_bytes} bytes)")

            # Decided once, here, from how full the store already is -- an
            # existing entry's promised TTL never changes because of a later put.
            ttl = self.ttl_seconds
            if self._occupancy_locked() >= self.high_watermark:
                ttl = min(self.ttl_seconds, self.ttl_under_load)

            report_id = self._new_id_locked()
            entry = Entry(gz=gz, nonce=nonce, created=now, expires=now + ttl,
                           meta=dict(meta or {}))
            self._entries[report_id] = entry
            self._total_bytes += size
            if self.directory:
                self._write_through_locked(report_id, entry)
            return report_id

    def _new_id_locked(self):
        for _ in range(5):
            candidate = secrets.token_urlsafe(self.id_bytes)
            if candidate not in self._entries:
                return candidate
        # Astronomically unlikely at id_bytes >= 6 (48 bits); a real collision
        # streak points at a broken id_bytes value, not bad luck.
        raise RuntimeError("could not allocate a unique report id")

    # -- reading ------------------------------------------------------------

    def get(self, report_id):
        """The entry for report_id, or None if it is missing, invalid, or expired.

        Validates the id shape before it can touch a filesystem path -- this is
        the traversal guard, and it runs whether or not `directory` is set.
        """
        if not _VALID_ID.match(report_id or ""):
            return None
        now = self._now()
        with self._lock:
            entry = self._entries.get(report_id)
            if entry is None and self.directory:
                entry = self._read_through_locked(report_id)
            if entry is None:
                return None
            if entry.expires <= now:
                self._discard_locked(report_id)
                return None
            return entry

    def mark_downloaded(self, report_id):
        """Cap report_id's remaining life to download_grace from now.

        Never extends a deadline -- a report already scheduled to expire sooner
        than the grace period keeps that sooner deadline. Silently does nothing
        for an id that is unknown or already gone: the caller (the /d/<id>
        handler) calls this only after already serving the bytes via get(), so
        there is nothing left to warn about here.
        """
        now = self._now()
        with self._lock:
            entry = self._entries.get(report_id)
            if entry is None:
                return
            entry.expires = min(entry.expires, now + self.download_grace)
            if self.directory:
                self._write_sidecar_locked(report_id, entry)

    # -- eviction -------------------------------------------------------------

    def prune(self):
        """Discard every expired entry now. Returns how many were removed."""
        now = self._now()
        with self._lock:
            return self._prune_locked(now)

    def _prune_locked(self, now):
        expired = [rid for rid, entry in self._entries.items() if entry.expires <= now]
        for rid in expired:
            self._discard_locked(rid)
        return len(expired)

    def _discard_locked(self, report_id):
        entry = self._entries.pop(report_id, None)
        if entry is not None:
            self._total_bytes -= len(entry.gz)
        if self.directory:
            self._forget_disk_locked(report_id)

    # -- optional disk mirror ------------------------------------------------
    #
    # Unset `directory` (the free-tier default -- no persistent disk to write
    # to) and none of this runs; ReportStore is memory-only. Set it (a Render
    # disk on a paid plan, or any other writable path) and every put() also
    # lands on disk, and a memory miss in get() reads through before giving up
    # -- so a process restart does not silently 404 a report that is still
    # within its TTL. This is the hook `LCT_REPORT_STORE_DIR` exists for; it
    # changes nothing else.

    def _paths(self, report_id):
        return (self.directory / f"{report_id}.html.gz",
                self.directory / f"{report_id}.json")

    def _write_through_locked(self, report_id, entry):
        gz_path, meta_path = self._paths(report_id)
        try:
            gz_path.write_bytes(entry.gz)
            self._write_sidecar_locked(report_id, entry)
        except OSError as exc:  # noqa: BLE001 - durability is best-effort
            print(f"[report_store] warning: could not mirror {report_id} to disk: {exc}")

    def _write_sidecar_locked(self, report_id, entry):
        _, meta_path = self._paths(report_id)
        try:
            meta_path.write_text(json.dumps({
                "nonce": entry.nonce, "created": entry.created,
                "expires": entry.expires, "meta": entry.meta,
            }), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - durability is best-effort
            print(f"[report_store] warning: could not update sidecar for {report_id}: {exc}")

    def _read_through_locked(self, report_id):
        gz_path, meta_path = self._paths(report_id)
        if not gz_path.exists() or not meta_path.exists():
            return None
        try:
            sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
            entry = Entry(gz=gz_path.read_bytes(), nonce=sidecar["nonce"],
                          created=sidecar["created"], expires=sidecar["expires"],
                          meta=sidecar.get("meta") or {})
        except (OSError, ValueError, KeyError) as exc:
            print(f"[report_store] warning: could not read {report_id} from disk: {exc}")
            return None
        self._entries[report_id] = entry
        self._total_bytes += len(entry.gz)
        return entry

    def _forget_disk_locked(self, report_id):
        for path in self._paths(report_id):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001 - durability is best-effort
                print(f"[report_store] warning: could not remove {path}: {exc}")
