from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# kind values: post, comment, job, news, filing, launch, review, tender, website
KINDS = {"post", "comment", "job", "news", "filing", "launch", "review", "tender", "website"}


@dataclass
class Item:
    source: str
    source_id: str
    kind: str
    url: str | None = None
    title: str | None = None
    body: str | None = None
    author: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    published_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return f"{self.title or ''}\n{self.body or ''}".strip()
