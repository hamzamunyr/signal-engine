from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.I)

# Domains that never identify the poster's company.
GENERIC_DOMAINS = {
    "reddit.com", "redd.it", "imgur.com", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "linkedin.com", "facebook.com", "instagram.com", "tiktok.com", "google.com", "docs.google.com",
    "github.com", "medium.com", "substack.com", "news.ycombinator.com", "ycombinator.com",
    "wikipedia.org", "amazon.com", "apple.com", "microsoft.com", "notion.so", "loom.com",
    "calendly.com", "cal.com", "bit.ly", "t.co", "stackoverflow.com", "stackexchange.com",
    "producthunt.com", "trustpilot.com", "g2.com", "capterra.com", "hubspot.com", "salesforce.com",
    "news.google.com", "techcrunch.com", "crunchbase.com", "sec.gov", "prnewswire.com",
    "businesswire.com", "globenewswire.com", "indiehackers.com", "preview.redd.it", "i.redd.it",
    "v.redd.it", "archive.org", "dropbox.com", "drive.google.com", "forms.gle", "typeform.com",
}


def clean_text(s: str | None, limit: int | None = None) -> str:
    if not s:
        return ""
    s = html.unescape(TAG_RE.sub(" ", s))
    s = WS_RE.sub(" ", s).strip()
    return s[:limit] if limit else s


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def from_epoch(ts) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def parse_date(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if isinstance(s, (int, float)):
        return from_epoch(s)
    s = str(s).strip()
    for fn in (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        parsedate_to_datetime,
        lambda v: datetime.strptime(v[:10], "%Y-%m-%d"),
    ):
        try:
            d = fn(s)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def registrable_domain(url_or_host: str | None) -> str | None:
    if not url_or_host:
        return None
    host = url_or_host
    if "://" in host:
        host = urlparse(host).netloc
    host = host.split("@")[-1].split(":")[0].lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) < 2:
        return None
    # handle common 2-level public suffixes
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def company_domains_in_text(text: str) -> list[str]:
    out: list[str] = []
    for u in URL_RE.findall(text or ""):
        d = registrable_domain(u)
        if d and d not in GENERIC_DOMAINS and d not in out:
            out.append(d)
    return out


def phrase_regex(phrases: list[str]) -> re.Pattern | None:
    phrases = [p.strip() for p in phrases if p and p.strip()]
    if not phrases:
        return None
    parts = []
    for p in sorted(set(phrases), key=len, reverse=True):
        esc = re.escape(p.lower()).replace(r"\ ", r"\s+")
        parts.append(esc)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(parts) + r")(?![a-z0-9])", re.I)
