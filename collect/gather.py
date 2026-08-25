#!/usr/bin/env python3
"""
collect/gather.py — pull everything the brief needs into one JSON file.

Fetches in parallel:
  * market data (10-year Treasury and S&P from FRED; your REIT from Yahoo)
  * every RSS feed in sources.yaml
  * REO's live event calendar

Writes state/raw.json. Nothing here talks to Claude — this is pure data
collection, so it's cheap, fast, and easy to debug on its own:

    python collect/gather.py --verbose

Design note: every fetch is wrapped so one dead source can't kill the run.
A brief with 12 of 15 feeds is fine; a brief that crashed is not.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:
    CT = dt.timezone(dt.timedelta(hours=-6))

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "collect" / "sources.yaml"
STATE = ROOT / "state"
UA = "Mozilla/5.0 (compatible; reo-cre-brief/1.0; +https://github.com)"

VERBOSE = False


# strftime's "%-d" / "%-I" (no zero padding) is a glibc extension — it raises on
# Windows. Build those parts by hand so local runs work everywhere.

def day_label(d: dt.datetime) -> str:
    return f"{d:%A, %B} {d.day}, {d:%Y}"


def short_day(d: dt.datetime) -> str:
    return f"{d:%a %b} {d.day}"


def clock(d: dt.datetime) -> str:
    return f"{d.hour % 12 or 12}:{d:%M %p}"


def log(msg: str) -> None:
    if VERBOSE:
        print(f"  {msg}", file=sys.stderr)


def get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ----------------------------------------------------------------------------
# Market data
# ----------------------------------------------------------------------------

def fred_series(series_id: str, timeout: int = 20) -> dict:
    """
    Pull a FRED series as CSV. No API key needed for the graph endpoint.
    Returns the two most recent real observations so we can report a change.
    FRED uses '.' for missing values (holidays), which we skip.
    """
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    rows = []
    for line in get(url, timeout).splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date, value = parts[0].strip(), parts[-1].strip()
        if value in (".", ""):
            continue
        try:
            rows.append((date, float(value)))
        except ValueError:
            continue
    if not rows:
        raise ValueError(f"no usable observations for {series_id}")

    latest_date, latest = rows[-1]
    prior_date, prior = rows[-2] if len(rows) > 1 else (None, None)
    out = {"series": series_id, "date": latest_date, "value": latest}
    if prior is not None:
        out["prior"] = prior
        out["prior_date"] = prior_date
        out["change"] = round(latest - prior, 4)
        # For yields, the change professionals quote is in basis points.
        out["change_bps"] = round((latest - prior) * 100)
        if prior:
            out["change_pct"] = round((latest - prior) / prior * 100, 2)
    return out


def yahoo_quote(symbol: str, timeout: int = 20) -> dict:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range=5d&interval=1d")
    meta = json.loads(get(url, timeout))["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    out = {"symbol": symbol, "price": price, "previous_close": prev,
           "currency": meta.get("currency")}
    if price is not None and prev:
        out["change"] = round(price - prev, 2)
        out["change_pct"] = round((price - prev) / prev * 100, 2)
    return out


# ----------------------------------------------------------------------------
# RSS / Atom
# ----------------------------------------------------------------------------

DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z",
]


def parse_date(raw: str):
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            d = dt.datetime.strptime(raw.replace("GMT", "+0000"), fmt)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def tag(block: str, name: str):
    m = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>",
                  block, re.S | re.I)
    return m.group(1).strip() if m else None


def clean(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_feed(xml: str, source_name: str, max_items: int, max_age_hours: int,
               via: str | None = None) -> list[dict]:
    items = re.findall(r"<item[ >].*?</item>|<entry[ >].*?</entry>", xml, re.S | re.I)
    now = dt.datetime.now(dt.timezone.utc)
    out = []

    for block in items[: max_items * 3]:
        title = clean(tag(block, "title"), 300)
        if not title:
            continue

        link = tag(block, "link") or ""
        if not link.startswith("http"):
            m = re.search(r'<link[^>]*href="([^"]+)"', block)
            link = m.group(1) if m else ""

        raw_date = (tag(block, "pubDate") or tag(block, "published")
                    or tag(block, "updated") or "")
        published = parse_date(raw_date) if raw_date else None

        if published and max_age_hours:
            age = (now - published).total_seconds() / 3600
            if age > max_age_hours:
                continue

        # Google News wraps the real publisher name into the title as
        # "Headline - Publisher". Split it out so attribution stays honest.
        publisher = source_name
        if via == "google_news" and " - " in title:
            head, _, pub = title.rpartition(" - ")
            if head and len(pub) < 60:
                title, publisher = head.strip(), pub.strip()

        out.append({
            "source": source_name,
            "publisher": publisher,
            "title": title,
            "url": link.strip(),
            "summary": clean(tag(block, "description") or tag(block, "summary"), 400),
            "published": published.isoformat() if published else None,
        })
        if len(out) >= max_items:
            break

    return out


def fetch_feed(spec: dict, limits: dict) -> dict:
    try:
        xml = get(spec["url"], limits.get("fetch_timeout_seconds", 20))
        items = parse_feed(
            xml, spec["name"],
            limits.get("per_feed_items", 12),
            limits.get("max_age_hours", 72),
            spec.get("via"),
        )
        log(f"ok    {spec['id']:<22} {len(items)} items")
        return {"id": spec["id"], "name": spec["name"], "tier": spec.get("tier", "cre"),
                "weight": spec.get("weight", 3), "items": items, "error": None}
    except Exception as e:
        log(f"FAIL  {spec['id']:<22} {type(e).__name__}: {e}")
        return {"id": spec["id"], "name": spec["name"], "tier": spec.get("tier", "cre"),
                "weight": spec.get("weight", 3), "items": [], "error": f"{type(e).__name__}: {e}"}


# ----------------------------------------------------------------------------
# REO calendar
# ----------------------------------------------------------------------------

def fetch_reo(url: str, days_ahead: int = 30, timeout: int = 20) -> list[dict]:
    raw = re.sub(r"\r?\n[ \t]", "", get(url, timeout))
    now = dt.datetime.now(CT)
    horizon = now + dt.timedelta(days=days_ahead)
    events = []

    for chunk in raw.split("BEGIN:VEVENT")[1:]:
        block = chunk.split("END:VEVENT")[0]
        ev = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            head, _, val = line.partition(":")
            name, _, params = head.partition(";")
            name, val = name.upper(), val.strip()
            if name == "DTSTART":
                try:
                    if "VALUE=DATE" in params:
                        ev["start"] = dt.datetime.strptime(val, "%Y%m%d").replace(tzinfo=CT)
                        ev["all_day"] = True
                    elif val.endswith("Z"):
                        ev["start"] = (dt.datetime.strptime(val, "%Y%m%dT%H%M%SZ")
                                       .replace(tzinfo=dt.timezone.utc).astimezone(CT))
                    else:
                        tzid = re.search(r"TZID=([^;:]+)", params)
                        tz = ZoneInfo(tzid.group(1)) if tzid else CT
                        ev["start"] = dt.datetime.strptime(val, "%Y%m%dT%H%M%S").replace(tzinfo=tz).astimezone(CT)
                except Exception:
                    pass
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
                ev[name.lower()] = (val.replace("\\n", " ").replace("\\,", ",")
                                       .replace("\\;", ";").strip())

        start = ev.get("start")
        if not start or not (now.replace(hour=0, minute=0) <= start <= horizon):
            continue

        days_out = (start.date() - now.date()).days
        summary = ev.get("summary", "")
        events.append({
            "summary": summary,
            "start": start.isoformat(),
            "when": short_day(start),
            "time": "all day" if ev.get("all_day") else clock(start),
            "location": ev.get("location", ""),
            "days_out": days_out,
            # Deadlines are the expensive things to miss, so flag them explicitly.
            "is_deadline": bool(re.search(r"due|deadline|applications|rsvp|registration",
                                          summary, re.I)),
            "urgent": days_out <= 7,
        })

    events.sort(key=lambda e: e["start"])
    log(f"ok    reo_calendar           {len(events)} events in {days_ahead}d")
    return events


# ----------------------------------------------------------------------------

def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--out", default=str(STATE / "raw.json"))
    ap.add_argument("--calendar-days", type=int, default=30)
    args = ap.parse_args()
    VERBOSE = args.verbose

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    limits = cfg.get("limits", {})
    STATE.mkdir(exist_ok=True)

    # Which REIT is the user following? Set once in state/log.json.
    log_path = STATE / "log.json"
    history = (json.loads(log_path.read_text(encoding="utf-8"))
               if log_path.exists() else {})
    reit_symbol = history.get("reit") or cfg["markets"]["reit"]["default_symbol"]

    log("fetching...")
    markets, feeds = {}, []

    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs = {}
        jobs[pool.submit(fred_series, "DGS10")] = ("market", "treasury_10y")
        jobs[pool.submit(fred_series, "SP500")] = ("market", "sp500")
        jobs[pool.submit(yahoo_quote, reit_symbol)] = ("market", "reit")
        jobs[pool.submit(fetch_reo, cfg["reo_calendar"], args.calendar_days)] = ("reo", None)
        for spec in cfg["feeds"]:
            jobs[pool.submit(fetch_feed, spec, limits)] = ("feed", spec["id"])

        reo_events = []
        for fut in as_completed(jobs):
            kind, key = jobs[fut]
            try:
                result = fut.result()
            except Exception as e:
                log(f"FAIL  {kind}:{key} {type(e).__name__}: {e}")
                if kind == "market":
                    markets[key] = {"error": f"{type(e).__name__}: {e}"}
                continue
            if kind == "market":
                markets[key] = result
                log(f"ok    market:{key}")
            elif kind == "reo":
                reo_events = result
            else:
                feeds.append(result)

    feeds.sort(key=lambda f: (-f["weight"], f["name"]))

    payload = {
        "generated_at": dt.datetime.now(CT).isoformat(),
        "date_label": day_label(dt.datetime.now(CT)),
        "reit_symbol": reit_symbol,
        "markets": markets,
        "reo_events": reo_events,
        "feeds": feeds,
        "stats": {
            "feeds_ok": sum(1 for f in feeds if not f["error"]),
            "feeds_failed": sum(1 for f in feeds if f["error"]),
            "total_items": sum(len(f["items"]) for f in feeds),
            "reo_events": len(reo_events),
        },
    }

    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    s = payload["stats"]
    print(f"Collected {s['total_items']} items from {s['feeds_ok']}/{len(feeds)} feeds, "
          f"{s['reo_events']} REO events -> {args.out}")

    if s["feeds_ok"] == 0:
        print("Every feed failed — check network access.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
