"""Polite HTTP: one shared session, per-host rate limits, small retry budget."""
from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger("http")

CONTACT = os.environ.get("CONTACT_EMAIL", "hamza@thepixelserve.com")
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
BOT_UA = f"signal-engine/1.0 (+https://thepixelserve.com; {CONTACT})"

# Minimum seconds between requests to the same host.
HOST_INTERVAL = {
    "efts.sec.gov": 0.25,
    "www.sec.gov": 0.25,
    "arctic-shift.photon-reddit.com": 1.0,
    "www.reddit.com": 2.5,
    "old.reddit.com": 2.5,
    "hn.algolia.com": 0.3,
    "api.stackexchange.com": 1.0,
    "news.google.com": 1.5,
    "suggestqueries.google.com": 1.0,
    "www.trustpilot.com": 4.0,
    "api.ted.europa.eu": 1.0,
    "dns.google": 0.1,
}
DEFAULT_INTERVAL = 0.5
NO_RETRY_HOSTS = {"www.linkedin.com", "linkedin.com"}


class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BOT_UA, "Accept-Language": "en-US,en;q=0.9"})
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait(self, host: str):
        gap = HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
        with self._lock:
            last = self._last.get(host, 0.0)
            delay = last + gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()

    def request(self, method: str, url: str, *, browser: bool = False, timeout: float = 25,
                retries: int = 2, **kw) -> requests.Response:
        host = urlparse(url).netloc
        headers = kw.pop("headers", {}) or {}
        if browser:
            headers.setdefault("User-Agent", BROWSER_UA)
            headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        if host in NO_RETRY_HOSTS:
            retries = 0
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                r = self.s.request(method, url, headers=headers, timeout=timeout, **kw)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = float(r.headers.get("Retry-After", 0) or 0) or (3 * (attempt + 1))
                    log.info("%s %s -> %s, retry in %.0fs", method, host, r.status_code, wait)
                    time.sleep(min(wait, 30))
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_exc = e
                resp = getattr(e, "response", None)
                if resp is not None and resp.status_code in (400, 401, 403, 404, 410):
                    raise
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def json(self, url, **kw):
        return self.get(url, **kw).json()


http = Http()
