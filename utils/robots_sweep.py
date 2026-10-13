#!/usr/bin/env python3
"""
robots_sweep.py — which retailers let which AI agents in, per their own robots.txt.
===================================================================================

Reads robots.txt files already saved by discovery_pipeline.py
(<out>/sites/*/robots.txt). With --fetch, it fetches any that are missing,
once each, politely. It never touches anything but /robots.txt.

For every site it evaluates a fixed list of crawler tokens against:
  - the homepage "/"
  - up to three real product paths from product_audit.json / sitemap.json
and reports allow / block / partial, plus whether the decision came from a
group that NAMES that crawler or from the "*" fallback. That distinction is
the finding: a retailer that writes "User-agent: GPTBot / Disallow: /" has made
a deliberate policy decision about AI; one that merely blocks "*" has not.

Matching follows RFC 9309: case-insensitive product-token match, groups
naming the same token are merged, longest matching path pattern wins,
Allow wins ties, '*' and '$' wildcards supported. (Python's urllib.robotparser
does not implement wildcards or longest-match correctly, so it is not used.)

Usage
-----
  python -m utils.robots_sweep --out discovery_output
  python -m utils.robots_sweep --out discovery_output --fetch        # fill gaps
  python -m utils.robots_sweep --out discovery_output --only leroymerlin.it,amazon.it

Outputs
-------
  <out>/robots_sweep.csv       one row per site, one column per crawler
  <out>/robots_sweep.md        readable matrix
  <out>/robots_fetched/*.txt   any robots.txt fetched by --fetch
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

# Crawler tokens to evaluate. Order = column order.
CRAWLERS: List[Tuple[str, str]] = [
    ("OAI-SearchBot",      "ChatGPT search index"),
    ("ChatGPT-User",       "ChatGPT fetch on user request"),
    ("GPTBot",             "OpenAI training"),
    ("PerplexityBot",      "Perplexity index"),
    ("Perplexity-User",    "Perplexity fetch on user request"),
    ("Claude-SearchBot",   "Claude search index"),
    ("Claude-User",        "Claude fetch on user request"),
    ("ClaudeBot",          "Anthropic training"),
    ("Google-Extended",    "Gemini training/grounding control token"),
    ("Googlebot",          "Google Search"),
    ("Applebot-Extended",  "Apple AI training control token"),
    ("Amazonbot",          "Amazon"),
    ("meta-externalagent", "Meta AI"),
    ("CCBot",              "Common Crawl"),
    ("Bytespider",         "ByteDance"),
    ("*",                  "generic client (your crawler)"),
]

AI_TOKENS_OF_INTEREST = {t.lower() for t, _ in CRAWLERS if t != "*"}


# ─────────────────────────────────────────────────────────────────────
# RFC 9309 parser / matcher
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Group:
    agents: List[str] = field(default_factory=list)
    rules: List[Tuple[str, str]] = field(default_factory=list)   # (allow|disallow, pattern)
    crawl_delay: Optional[str] = None
    other: List[str] = field(default_factory=list)                # non-standard lines


@dataclass
class Robots:
    groups: List[Group] = field(default_factory=list)
    sitemaps: List[str] = field(default_factory=list)
    content_signals: List[str] = field(default_factory=list)
    nonstandard: List[str] = field(default_factory=list)


def parse_robots(text: str) -> Robots:
    r = Robots()
    cur: Optional[Group] = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key in ("user-agent", "useragent", "user agent"):
            if cur is None or not last_was_agent:
                cur = Group()
                r.groups.append(cur)
            cur.agents.append(val)
            last_was_agent = True
            continue
        last_was_agent = False
        if key == "sitemap":
            r.sitemaps.append(val)
            continue
        if key == "content-signal":
            r.content_signals.append(val)
            if cur is not None:
                cur.other.append(f"content-signal: {val}")
            continue
        if cur is None:
            r.nonstandard.append(line)
            continue
        if key in ("allow", "disallow"):
            cur.rules.append((key, val))
        elif key == "crawl-delay":
            cur.crawl_delay = val
        else:
            cur.other.append(f"{key}: {val}")
    return r


def _token(agent_value: str) -> str:
    # "GPTBot/1.1" -> "gptbot"; "Mozilla/5.0 (compatible; GPTBot)" is not a valid
    # group value per RFC 9309 and we don't try to be clever about it.
    return agent_value.split("/", 1)[0].strip().lower()


def select_groups(robots: Robots, token: str) -> Tuple[List[Group], str]:
    """Return (groups that apply, 'named'|'wildcard'|'none')."""
    t = token.lower()
    if t != "*":
        named = [g for g in robots.groups if any(_token(a) == t for a in g.agents)]
        if named:
            return named, "named"
    star = [g for g in robots.groups if any(a.strip() == "*" for a in g.agents)]
    if star:
        return star, "wildcard"
    return [], "none"


def _pattern_to_regex(pat: str) -> re.Pattern:
    anchored = pat.endswith("$")
    body = pat[:-1] if anchored else pat
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + rx + ("$" if anchored else ""))


def is_allowed(groups: List[Group], path: str) -> Tuple[bool, str]:
    """RFC 9309: longest match wins; Allow wins ties; no match => allowed."""
    path = unquote(path or "/")
    best_len, best_kind, best_pat = -1, "allow", ""
    for g in groups:
        for kind, pat in g.rules:
            if kind == "disallow" and pat == "":
                continue                       # empty Disallow = no restriction
            p = unquote(pat)
            if not p.startswith("/") and not p.startswith("*"):
                p = "/" + p
            if _pattern_to_regex(p).match(path):
                L = len(p)
                if L > best_len or (L == best_len and kind == "allow"):
                    best_len, best_kind, best_pat = L, kind, pat
    return best_kind == "allow", (f"{best_kind} {best_pat}" if best_len >= 0 else "no rule")


# ─────────────────────────────────────────────────────────────────────
# Site loading
# ─────────────────────────────────────────────────────────────────────

def product_paths(site_dir: Path, limit: int = 3) -> List[str]:
    out: List[str] = []
    pa = site_dir / "product_audit.json"
    if pa.exists():
        try:
            data = json.loads(pa.read_text(encoding="utf-8"))
            for p in data.get("products", []):
                if p.get("http_status") == 200 and p.get("url"):
                    out.append(urlparse(p["url"]).path or "/")
                if len(out) >= limit:
                    return out
            # blocked sites have no 200s — still use the sampled URLs
            for p in data.get("products", []):
                if p.get("url"):
                    pth = urlparse(p["url"]).path or "/"
                    if pth not in out:
                        out.append(pth)
                if len(out) >= limit:
                    return out
        except Exception:
            pass
    sm = site_dir / "sitemap.json"
    if sm.exists() and len(out) < limit:
        try:
            data = json.loads(sm.read_text(encoding="utf-8"))
            for u in data.get("urls", [])[:500]:
                loc = u.get("loc", "")
                pth = urlparse(loc).path
                if pth and pth != "/" and pth.count("/") >= 1 and pth not in out \
                        and not re.search(r"\.(xml|jpg|png|pdf)$", pth):
                    out.append(pth)
                if len(out) >= limit:
                    break
        except Exception:
            pass
    return out


def load_sites(out: Path, only: set) -> List[dict]:
    sites = []
    root = out / "sites"
    if not root.is_dir():
        sys.exit(f"no such directory: {root}")
    seen_hosts = set()
    # newest dir wins when the same host was run more than once
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        pr = d / "probe_results.json"
        if not pr.exists():
            continue
        try:
            meta = json.loads(pr.read_text(encoding="utf-8"))
        except Exception:
            continue
        url = meta.get("url") or ""
        host = (urlparse(url).netloc or "").lower().removeprefix("www.")
        if not host or host in seen_hosts:
            continue
        if only and host not in only and (meta.get("company") or "").lower() not in only:
            continue
        seen_hosts.add(host)
        sites.append(dict(company=meta.get("company") or host, url=url, host=host, dir=d))
    return sorted(sites, key=lambda s: s["host"])


def fetch_robots(url: str, dest: Path, timeout: float) -> Tuple[Optional[str], int, str]:
    import httpx
    target = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}/robots.txt"
    try:
        r = httpx.get(target, timeout=timeout, follow_redirects=True, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "text/plain,*/*;q=0.8"})
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"
    body = r.text or ""
    if r.status_code == 200 and body.lstrip()[:1] == "<" and "<html" in body[:800].lower():
        return None, r.status_code, "HTML served at /robots.txt (soft 404 or challenge)"
    if r.status_code == 200:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        return body, 200, ""
    return None, r.status_code, f"HTTP {r.status_code}"


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────

def classify(results: List[bool]) -> str:
    if not results:
        return "?"
    if all(results):
        return "allow"
    if not any(results):
        return "block"
    return "partial"


def main():
    ap = argparse.ArgumentParser(description="AI-crawler robots.txt sweep")
    ap.add_argument("--out", default="discovery_output")
    ap.add_argument("--fetch", action="store_true",
                    help="fetch /robots.txt where no saved copy exists (one request per site)")
    ap.add_argument("--only", default="", help="comma-separated hosts")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    out = Path(args.out)
    only = {s.strip().lower().removeprefix("www.") for s in args.only.split(",") if s.strip()}
    sites = load_sites(out, only)
    if not sites:
        sys.exit("no sites found")

    rows = []
    for s in sites:
        text, status, note, source = None, 0, "", ""
        saved = s["dir"] / "robots.txt"
        cached = out / "robots_fetched" / f"{s['host']}.txt"
        if saved.exists() and saved.stat().st_size > 0:
            text, status, source = saved.read_text(encoding="utf-8", errors="replace"), 200, "pipeline"
        elif cached.exists():
            text, status, source = cached.read_text(encoding="utf-8", errors="replace"), 200, "cache"
        elif args.fetch:
            text, status, note = fetch_robots(s["url"], cached, args.timeout)
            source = "fetched"
            time.sleep(args.delay)
        else:
            note = "no saved robots.txt (re-run with --fetch)"

        row = {"company": s["company"], "host": s["host"], "robots_source": source,
               "robots_http": status, "robots_note": note}

        if text is None:
            # 4xx on robots.txt = RFC 9309 says "allow all"; 5xx/unreachable = "disallow all".
            if source == "fetched" and status and 400 <= status < 500:
                row["robots_note"] = (note + "; RFC 9309: 4xx ⇒ treat as allow-all").strip("; ")
            elif source == "fetched" and (status >= 500 or status == 0):
                row["robots_note"] = (note + "; RFC 9309: unreachable ⇒ treat as disallow-all").strip("; ")
            for tok, _ in CRAWLERS:
                row[tok] = "n/a"
            row.update(named_ai_groups="", blanket_block_star="", crawl_delay_star="",
                       content_signal="", sitemaps=0, paths_tested="")
            rows.append(row)
            print(f"{s['host']:28} robots: {row['robots_note']}")
            continue

        rb = parse_robots(text)
        paths = ["/"] + product_paths(s["dir"])
        named_ai = sorted({a.strip() for g in rb.groups for a in g.agents
                           if _token(a) in AI_TOKENS_OF_INTEREST
                           or re.search(r"(gpt|openai|claude|anthropic|perplexity|ccbot|"
                                        r"bytespider|cohere|meta-external|diffbot|"
                                        r"google-extended|applebot-extended|amazonbot)",
                                        a, re.I)})
        star_groups, _ = select_groups(rb, "*")
        star_home_ok, _ = is_allowed(star_groups, "/")
        star_delay = next((g.crawl_delay for g in star_groups if g.crawl_delay), "")

        row["named_ai_groups"] = "; ".join(named_ai)
        row["blanket_block_star"] = "yes" if not star_home_ok else "no"
        row["crawl_delay_star"] = star_delay
        row["content_signal"] = " | ".join(rb.content_signals)
        row["sitemaps"] = len(rb.sitemaps)
        row["paths_tested"] = " ".join(paths)

        for tok, _ in CRAWLERS:
            groups, how = select_groups(rb, tok)
            verdict = classify([is_allowed(groups, p)[0] for p in paths])
            row[tok] = verdict + ("·named" if how == "named" else "")
        rows.append(row)
        print(f"{s['host']:28} named AI groups: {row['named_ai_groups'] or '—'}")

    cols = (["company", "host", "robots_source", "robots_http", "robots_note",
             "named_ai_groups", "blanket_block_star", "crawl_delay_star",
             "content_signal", "sitemaps", "paths_tested"] + [t for t, _ in CRAWLERS])
    with open(out / "robots_sweep.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Readable matrix
    short = {"allow": "✓", "block": "✗", "partial": "½", "n/a": "–", "?": "?"}
    def cell(v: str) -> str:
        base, _, named = v.partition("·")
        return short.get(base, base) + ("ⁿ" if named else "")
    head = ["host"] + [t for t, _ in CRAWLERS]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lines.append("| " + " | ".join([r["host"]] + [cell(r[t]) for t, _ in CRAWLERS]) + " |")
    legend = ("\n✓ allowed · ✗ blocked · ½ some paths blocked · – no robots.txt available · "
              "ⁿ = decided by a group that NAMES this crawler (deliberate policy), "
              "otherwise inherited from User-agent: *\n")
    (out / "robots_sweep.md").write_text("\n".join(lines) + "\n" + legend, encoding="utf-8")

    # Console summary
    print("\n── summary ──")
    have = [r for r in rows if r.get("OAI-SearchBot") not in ("n/a", None)]
    print(f"sites with robots.txt evaluated: {len(have)}/{len(rows)}")
    for tok in ("OAI-SearchBot", "ChatGPT-User", "GPTBot", "PerplexityBot",
                "ClaudeBot", "Google-Extended", "*"):
        blk = sum(1 for r in have if r[tok].startswith("block"))
        nam = sum(1 for r in have if r[tok].endswith("·named"))
        print(f"  {tok:18} blocked on {blk:2} · named explicitly on {nam:2}")
    asym = [r["host"] for r in have
            if r["*"].startswith(("allow", "partial")) and r["GPTBot"].startswith("block")]
    print(f"  block AI training but allow generic clients: {', '.join(asym) or '—'}")
    asym2 = [r["host"] for r in have
             if r["OAI-SearchBot"].startswith("allow") and r["GPTBot"].startswith("block")]
    print(f"  allow ChatGPT search but block OpenAI training: {', '.join(asym2) or '—'}")
    print(f"\nwrote {out/'robots_sweep.csv'} and {out/'robots_sweep.md'}")


if __name__ == "__main__":
    main()