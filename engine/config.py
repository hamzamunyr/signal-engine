"""Loads the signal catalog and client niche packs, and compiles each client's matchers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache
def catalog() -> dict:
    data = yaml.safe_load((ROOT / "signals.yaml").read_text())
    data["signals"] = {int(k): v for k, v in data["signals"].items()}
    return data


def _compile_phrase(p: str) -> re.Pattern:
    if p.startswith("re:"):
        return re.compile(p[3:], re.I)
    esc = re.escape(p.lower().strip()).replace(r"\ ", r"\s+")
    left = r"(?<![a-z0-9])" if p[:1].isalnum() else ""
    right = r"(?![a-z0-9])" if p[-1:].isalnum() else ""
    return re.compile(left + esc + right, re.I)


def _union(patterns: list[re.Pattern]) -> re.Pattern | None:
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p.pattern})" for p in patterns), re.I)


@dataclass
class SignalRule:
    id: int
    name: str
    group: str
    status: str
    kinds: set[str]
    weight: float
    half_life: float
    stop_after: float
    any_re: re.Pattern | None = None
    all_res: list[re.Pattern] = field(default_factory=list)
    near_re: re.Pattern | None = None
    title_re: re.Pattern | None = None
    computed: set[str] = field(default_factory=set)

    def matches(self, kind: str, text: str, title: str, flags: set[str]) -> bool:
        if kind not in self.kinds:
            return False
        if self.computed & flags:
            return True
        if self.title_re is not None:
            return bool(self.title_re.search(title))
        if self.near_re is not None and not self.near_re.search(text):
            return False
        if self.all_res:
            return all(r.search(text) for r in self.all_res)
        if self.any_re is not None:
            return bool(self.any_re.search(text))
        return self.near_re is not None


@dataclass
class Client:
    name: str
    cfg: dict
    rules: dict[int, SignalRule]
    negative_re: re.Pattern | None
    strong_re: re.Pattern | None   # explicit-intent phrases strong enough outside the client's communities

    @property
    def communities(self) -> set[str]:
        return {c.lower() for c in self.cfg.get("communities", [])}

    def skips(self, source: str) -> bool:
        return source in set(self.cfg.get("skip_sources", []))


def _vocab_for(cfg: dict) -> dict[str, list[str]]:
    v = {k: list(vals) for k, vals in catalog()["vocab"].items()}
    for k, vals in (cfg.get("vocab") or {}).items():
        v.setdefault(k, []).extend(vals)
    for key in ("offer_terms", "tool_terms", "competitors", "pain_terms", "industry_terms"):
        v[key] = list(cfg.get(key) or [])
    v["offer_or_competitors"] = v["offer_terms"] + v["competitors"]
    v["offer_or_tools"] = v["offer_terms"] + v["tool_terms"]
    return v


def _expand(entry, vocab) -> list[str]:
    if isinstance(entry, str):
        m = re.fullmatch(r"\{(\w+)\}", entry)
        return list(vocab.get(m.group(1), [])) if m else [entry]
    out: list[str] = []
    for e in entry:
        out.extend(_expand(e, vocab))
    return out


def build_client(cfg: dict) -> Client:
    cat = catalog()
    vocab = _vocab_for(cfg)
    sig_cfg = cfg.get("signals") or {}
    off = {int(s) for s in sig_cfg.get("disabled", [])}
    mult = {int(k): float(v) for k, v in (sig_cfg.get("weights") or {}).items()}
    rules: dict[int, SignalRule] = {}
    for sid, s in cat["signals"].items():
        if sid in off or s.get("built") is False or not s.get("kinds"):
            continue
        g = cat["groups"][s["group"]]
        m = s.get("match") or {}
        rule = SignalRule(
            id=sid, name=s["name"], group=s["group"], status=s["status"], kinds=set(s["kinds"]),
            weight=g["weight"] * mult.get(sid, 1.0), half_life=g["half_life"], stop_after=g["stop_after"],
        )
        if "any" in m:
            rule.any_re = _union([_compile_phrase(p) for p in _expand(m["any"], vocab)])
        if "all_of" in m:
            parts = [_union([_compile_phrase(p) for p in _expand(lst, vocab)]) for lst in m["all_of"]]
            if any(p is None for p in parts):   # a list is empty for this client -> rule can't fire on text
                parts = []
            rule.all_res = parts
        if "near" in m:  # phrase from list A followed by phrase from list B within N words
            a = _union([_compile_phrase(p) for p in _expand(m["near"][0], vocab)])
            b = _union([_compile_phrase(p) for p in _expand(m["near"][1], vocab)])
            gap = int(m["near"][2]) if len(m["near"]) > 2 else 6
            if a is not None and b is not None:
                rule.near_re = re.compile(f"(?:{a.pattern})(?:\\W+\\w+){{0,{gap}}}?\\W+(?:{b.pattern})", re.I)
        if "title_any" in m:
            rule.title_re = _union([_compile_phrase(p) for p in _expand(m["title_any"], vocab)])
        if "computed" in m:
            rule.computed.add(m["computed"])
        rule.computed.update(m.get("computed_any", []))
        if rule.any_re is None and not rule.all_res and rule.title_re is None and rule.near_re is None and not rule.computed:
            continue
        rules[sid] = rule
    neg = _union([_compile_phrase(p) for p in cfg.get("negative_keywords", [])])
    strong_ids = [i for i in (1, 2, 3, 4, 5, 19, 89) if i in rules]
    strong = None
    if strong_ids:
        pats = []
        for i in strong_ids:
            r = rules[i]
            if r.any_re is not None:
                pats.append(r.any_re)
        strong = _union(pats) if pats else None
    return Client(name=cfg["client"], cfg=cfg, rules=rules, negative_re=neg, strong_re=strong)


@lru_cache
def clients() -> tuple[Client, ...]:
    out = []
    for p in sorted((ROOT / "clients").glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        cfg = yaml.safe_load(p.read_text())
        if cfg.get("active", True):
            out.append(build_client(cfg))
    return tuple(out)


def union_list(key: str) -> list:
    seen, out = set(), []
    for c in clients():
        for v in c.cfg.get(key) or []:
            k = str(v).lower()
            if k not in seen:
                seen.add(k)
                out.append(v)
    return out


def any_client_uses(source: str) -> bool:
    return any(not c.skips(source) for c in clients())
