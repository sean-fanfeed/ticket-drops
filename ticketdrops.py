#!/usr/bin/env python3
"""
Ticket-drop tracker for Sean's personal agent.

Once a day it pulls US music + sports on-sales and presales landing in the next
N days from the Ticketmaster Discovery API, scores each one 0-100 for resale
potential, and surfaces only what clears the threshold. New high scorers go out
as ntfy push notifications; everything surfaced is written to a mobile-first
HTML page, a JSON file, and a markdown block for the morning brief.

Standard library only - no pip install, so launchd can run it with system python3.

Usage:
    python3 ticketdrops.py              # normal daily run
    python3 ticketdrops.py --dry-run    # pull + score + write files, send nothing
    python3 ticketdrops.py --test-ping  # send one test notification and exit
    python3 ticketdrops.py --no-state   # ignore state.json, treat everything as new
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
LOGS = HERE / "logs"
STATE_PATH = HERE / "state.json"
PAGE_NAME = "tickets.html"

TM_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
TM_ATTRACTIONS = "https://app.ticketmaster.com/discovery/v2/attractions.json"
NTFY_BASE = "https://ntfy.sh"

# Ticketmaster's own segment ids. classificationName= is fuzzier and pulls in
# adjacent segments, so we pin the ids.
SEGMENTS = {"music": "KZFzniwnSyZfZ7v7nJ", "sports": "KZFzniwnSyZfZ7v7nE"}

USER_AGENT = "sean-ticketdrops/1.0 (personal agent)"

log = logging.getLogger("ticketdrops")


def _ssl_context() -> ssl.SSLContext:
    """python.org builds on macOS ship without a populated CA store, so a plain
    urlopen fails CERTIFICATE_VERIFY_FAILED. Use the system store when it is
    really there, otherwise fall back to certifi. CI runners hit the first path.
    """
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    has_store = bool(
        (paths.openssl_cafile and os.path.exists(paths.openssl_cafile))
        or (paths.openssl_capath and os.path.isdir(paths.openssl_capath))
    )
    if not has_store:
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001
            log.warning("No CA store and no certifi - TLS verification will fail. "
                        "Run '/Applications/Python 3.14/Install Certificates.command'.")
    return ctx


SSL_CTX = _ssl_context()


# ----------------------------------------------------------------------------
# config / env
# ----------------------------------------------------------------------------

def load_env(path: Path) -> dict:
    """Minimal .env reader. KEY=VALUE, # comments, optional surrounding quotes."""
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        env[key.strip()] = val
    return env


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def setup_logging(verbose: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = LOGS / f"run-{datetime.now().strftime('%Y-%m-%d')}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]


# ----------------------------------------------------------------------------
# http
# ----------------------------------------------------------------------------

def get_json(url: str, params: dict, tries: int = 4) -> dict:
    """GET with backoff. Ticketmaster allows 5 req/sec; we stay well under."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    full = f"{url}?{qs}"
    safe = full.replace(params.get("apikey", "\0"), "***") if params.get("apikey") else full

    for attempt in range(1, tries + 1):
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                wait = min(30, 2 ** attempt)
                log.warning("429 rate limited, sleeping %ss", wait)
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                raise RuntimeError(
                    f"Ticketmaster rejected the API key (HTTP {e.code}). "
                    f"Check TICKETMASTER_API_KEY in .env. Response: {body}"
                ) from e
            if 500 <= e.code < 600 and attempt < tries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code} from {safe}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Network error calling {safe}: {e}") from e
    raise RuntimeError(f"Gave up on {safe}")


# ----------------------------------------------------------------------------
# ticketmaster
# ----------------------------------------------------------------------------

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_onsales(api_key: str, segment: str, days: int, page_size: int,
                  max_pages: int, country: str) -> list:
    """Events whose PUBLIC on-sale starts inside the next `days` days."""
    now = datetime.now(timezone.utc)
    # NB: do NOT add sort=onSaleStartDate,asc here. Ticketmaster stamps events
    # with no public on-sale as 1900-01-01, and that sort puts every one of them
    # first - the whole first page comes back as sentinels. Measured 2026-10-01:
    # onsaleOnAfterStartDate + no sort -> 96/100 in-window; with that sort -> 0/100.
    params = {
        "apikey": api_key,
        "countryCode": country,
        "segmentId": SEGMENTS[segment],
        "onsaleOnAfterStartDate": now.strftime("%Y-%m-%d"),
        "size": page_size,
    }
    events, page = [], 0
    while page < max_pages:
        params["page"] = page
        data = get_json(TM_EVENTS, params)
        embedded = data.get("_embedded", {}).get("events", [])
        events.extend(embedded)
        info = data.get("page", {})
        total_pages = info.get("totalPages", 0)
        log.debug("%s page %d/%s -> %d events", segment, page + 1, total_pages, len(embedded))
        page += 1
        # Discovery caps paging at 1000 results deep.
        if page >= total_pages or page * page_size >= 1000 or not embedded:
            break
        time.sleep(0.25)
    kept = [e for e in events if onsale_in_window(e, now, now + timedelta(days=days))]
    log.info("Ticketmaster %s: %d returned, %d with a real on-sale in the next %dd",
             segment, len(events), len(kept), days)
    return kept


def onsale_in_window(ev: dict, lo: datetime, hi: datetime) -> bool:
    """True only for a genuine public on-sale inside [lo, hi].

    Ticketmaster uses 1900-01-01 to mean "no on-sale date set", and those slip
    past the server-side filter, so this is the gate that actually counts.
    """
    s = ((ev.get("sales") or {}).get("public") or {}).get("startDateTime")
    dt = parse_dt(s)
    return bool(dt and dt.year > 2000 and lo <= dt <= hi)


def fetch_attraction_events(api_key: str, name: str, page_size: int) -> list:
    """Watchlist path: find the attraction, then everything it has on sale.

    Deliberately NOT limited to the 14-day on-sale window - the point of the
    watchlist is to catch these artists whenever they surface.
    """
    data = get_json(TM_ATTRACTIONS, {"apikey": api_key, "keyword": name, "size": 5})
    attractions = data.get("_embedded", {}).get("attractions", [])
    if not attractions:
        log.info("Watchlist: no Ticketmaster attraction found for %r", name)
        return []
    att = attractions[0]
    log.info("Watchlist: %r -> %r (id %s)", name, att.get("name"), att.get("id"))
    time.sleep(0.25)
    data = get_json(TM_EVENTS, {
        "apikey": api_key, "attractionId": att["id"],
        "size": page_size, "sort": "date,asc", "countryCode": "US",
    })
    return data.get("_embedded", {}).get("events", [])


# ----------------------------------------------------------------------------
# eventbrite (watchlist only, optional)
# ----------------------------------------------------------------------------

def fetch_eventbrite(token: str, name: str) -> list:
    """Eventbrite retired its public event search in 2019 (/v3/events/search/
    returns 404/410 for everyone now). We still try when a token is present so
    the code lights up if they ever restore it, but we never treat the failure
    as an error - we log it once and carry on.
    """
    url = "https://www.eventbriteapi.com/v3/events/search/"
    qs = urllib.parse.urlencode({"q": name, "sort_by": "date"})
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("events", [])
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            log.info("Eventbrite public search is retired (HTTP %d) - skipping %r", e.code, name)
        else:
            log.warning("Eventbrite error %d for %r", e.code, name)
        return []
    except Exception as e:  # noqa: BLE001 - never let Eventbrite break the run
        log.warning("Eventbrite call failed for %r: %s", name, e)
        return []


# ----------------------------------------------------------------------------
# normalise + score
# ----------------------------------------------------------------------------

def parse_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_VENUES = None


def venue_capacity(name: str):
    """(capacity, source). Ticketmaster returns no capacity for any venue -
    verified against MSG, SoFi, Sphere - so it comes from venues.json: an exact
    table first, then a coarse keyword guess from the room's name."""
    global _VENUES
    if _VENUES is None:
        _VENUES = load_json(HERE / "venues.json", {"exact": {}, "keywords": {}})
    if not name:
        return None, "unknown"
    low = name.lower()
    for known, cap in _VENUES.get("exact", {}).items():
        if known in low:
            return int(cap), "exact"
    for word, cap in sorted(_VENUES.get("keywords", {}).items(), key=lambda kv: -len(kv[0])):
        if word in low:
            return int(cap), "venue type"
    return None, "unknown"


def normalise(ev: dict) -> dict:
    emb = ev.get("_embedded", {})
    venues = emb.get("venues") or [{}]
    v = venues[0]
    attractions = emb.get("attractions") or []

    cls = (ev.get("classifications") or [{}])[0]
    genre = (cls.get("genre") or {}).get("name") or ""
    segment = (cls.get("segment") or {}).get("name") or ""

    sales = ev.get("sales") or {}
    public = sales.get("public") or {}
    presales = sales.get("presales") or []

    prices = ev.get("priceRanges") or []
    pmin = min((p.get("min") for p in prices if p.get("min") is not None), default=None)
    pmax = max((p.get("max") for p in prices if p.get("max") is not None), default=None)

    cap = v.get("capacity")
    try:
        cap = int(cap) if cap else None
    except (TypeError, ValueError):
        cap = None
    cap_source = "ticketmaster" if cap else None
    if cap is None:
        cap, cap_source = venue_capacity(v.get("name") or "")

    upcoming = 0
    for a in attractions:
        tot = ((a.get("upcomingEvents") or {}).get("_total")) or 0
        try:
            upcoming = max(upcoming, int(tot))
        except (TypeError, ValueError):
            pass

    return {
        "id": ev.get("id"),
        "name": ev.get("name") or "(untitled)",
        "url": ev.get("url") or "",
        "segment": segment,
        "genre": genre,
        "artists": [a.get("name") for a in attractions if a.get("name")],
        "artist_upcoming": upcoming,
        "venue": v.get("name") or "",
        "city": ((v.get("city") or {}).get("name")) or "",
        "state": ((v.get("state") or {}).get("stateCode")) or "",
        "capacity": cap,
        "capacity_source": cap_source,
        "event_date": ((ev.get("dates") or {}).get("start") or {}).get("dateTime")
                      or ((ev.get("dates") or {}).get("start") or {}).get("localDate"),
        "onsale_start": public.get("startDateTime"),
        "onsale_end": public.get("endDateTime"),
        # Only ~13% of presales carry a signup url; the rest explain access in
        # description/shortDescription ("Live Nation All Access members..."), so
        # keep both and let the page show whichever exists.
        "presales": [
            {
                "name": p.get("name"),
                "start": p.get("startDateTime"),
                "end": p.get("endDateTime"),
                "url": p.get("url") or "",
                "how": (p.get("shortDescription") or p.get("description") or "")[:180],
            }
            for p in presales
        ],
        "price_min": pmin,
        "price_max": pmax,
    }


def score_event(e: dict, cfg: dict, watch_names: set, seatgeek: dict | None) -> dict:
    """0-100 resale-potential score. Every component is reported so a score can
    be argued with rather than trusted blindly."""
    w = cfg["weights"]
    reasons, parts = [], {}

    # --- demand: how much work the act is doing + whether it's a known flipper
    u = e["artist_upcoming"]
    demand = min(w["demand_max"] - w["watchlist_bonus"], 14 * math.log10(u + 1)) if u else 4.0
    on_watch = any(a.lower() in watch_names for a in e["artists"])
    if on_watch:
        demand += w["watchlist_bonus"]
        reasons.append("on your watchlist")
    if u >= 40:
        reasons.append(f"{u} upcoming dates (heavy routing)")
    parts["demand"] = round(min(demand, w["demand_max"]), 1)

    # --- scarcity: small room + real demand is where markup lives
    cap = e["capacity"]
    if cap:
        lo, hi = math.log10(500), math.log10(80000)
        frac = (math.log10(max(cap, 500)) - lo) / (hi - lo)
        scarcity = w["scarcity_max"] * max(0.0, 1.0 - frac)
        if cap <= 3000:
            reasons.append(f"small room ({cap:,} cap)")
    else:
        scarcity = 0.0  # no capacity at all -> no points. Absence is not evidence.
    parts["scarcity"] = round(scarcity, 1)
    if cap and e.get("capacity_source") == "venue type":
        reasons.append(f"~{cap:,} cap (from venue type)")

    # --- markup: the honest weak spot without a resale feed
    mult_lo, mult_hi, conf = estimate_resale_multiple(e, cfg, seatgeek, on_watch)
    mid = (mult_lo + mult_hi) / 2
    markup = max(0.0, min(w["markup_max"], (mid - 1.0) / 1.2 * w["markup_max"]))
    parts["markup"] = round(markup, 1)
    if mid >= 1.6:
        reasons.append(f"est. {mult_lo:.1f}-{mult_hi:.1f}x face")

    # --- timing: presales and imminence
    timing = 0.0
    if e["presales"]:
        timing += 4
        reasons.append(f"{len(e['presales'])} presale(s)")
    start = parse_dt(e["onsale_start"])
    if start:
        days = (start - datetime.now(timezone.utc)).days
        if days <= 3:
            timing += 6
        elif days <= 7:
            timing += 3
    parts["timing"] = round(min(timing, w["timing_max"]), 1)

    total = sum(parts.values())

    confidence = conf
    if not cap:
        confidence = "low" if confidence != "low" else "low"

    who = e["artists"][0] if e["artists"] else e["name"]
    return {
        **e,
        "seatgeek_url": "https://seatgeek.com/search?" + urllib.parse.urlencode({"search": who}),
        "score": int(round(max(0, min(100, total)))),
        "score_parts": parts,
        "confidence": confidence,
        "on_watchlist": on_watch,
        "resale_multiple": [round(mult_lo, 2), round(mult_hi, 2)],
        "est_resale_min": round(e["price_min"] * mult_lo) if e["price_min"] else None,
        "est_resale_max": round(e["price_max"] * mult_hi) if e["price_max"] else None,
        "reasons": reasons,
    }


def estimate_resale_multiple(e: dict, cfg: dict, seatgeek: dict | None, on_watch: bool):
    """Returns (low, high, confidence).

    With a SeatGeek client id this would be measured against live listings. It is
    off, so this is a genre-table estimate nudged by demand signals - useful for
    ranking, NOT a number to underwrite a purchase with. Confidence says so.
    """
    if seatgeek:
        lo = seatgeek.get("resale_lo")
        hi = seatgeek.get("resale_hi")
        if lo and hi:
            return lo, hi, "high"

    table = cfg["genre_multiples"]
    key = e["genre"] if e["genre"] in table else ("_sports" if e["segment"] == "Sports" else "_default")
    lo, hi = table[key]

    if e["artist_upcoming"] >= 40:
        lo, hi = lo + 0.1, hi + 0.2
    if e["capacity"] and e["capacity"] <= 3000:
        lo, hi = lo + 0.15, hi + 0.3
    if on_watch:
        lo, hi = lo + 0.1, hi + 0.25

    return lo, hi, "low"


# ----------------------------------------------------------------------------
# ntfy
# ----------------------------------------------------------------------------

def ntfy_post(topic: str, title: str, body: str, tags: str = "", click: str = "",
              priority: str = "default", dry: bool = False, actions: str = "") -> bool:
    if dry:
        log.info("[dry-run] ntfy %s | %s | %s", topic, title, body.replace("\n", " / ")[:120])
        return True
    headers = {
        "User-Agent": USER_AGENT,
        "Title": title.encode("ascii", "replace").decode(),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = actions
    req = urllib.request.Request(
        f"{NTFY_BASE}/{topic}", data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            return resp.status < 300
    except Exception as e:  # noqa: BLE001
        log.error("ntfy post failed: %s", e)
        return False


# ----------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------

def fmt_money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "-"


def face_str(e):
    """Face value. Ticketmaster usually withholds priceRanges until the on-sale
    actually opens, so 'TBA' is the common case, not an error."""
    if e["price_min"] and e["price_max"]:
        return f"{fmt_money(e['price_min'])}-{fmt_money(e['price_max'])}"
    if e["price_min"]:
        return f"from {fmt_money(e['price_min'])}"
    return "TBA"


def resale_str(e):
    if e["est_resale_min"] and e["est_resale_max"]:
        return f"{fmt_money(e['est_resale_min'])}-{fmt_money(e['est_resale_max'])}"
    lo, hi = e["resale_multiple"]
    return f"{lo:g}-{hi:g}x face"


_TZ = None


def set_timezone(name: str, fallback_offset: int) -> None:
    """Resolve the display timezone once. A real tz keeps the clock honest
    across the DST flip; the fixed offset is only a last resort."""
    global _TZ
    if ZoneInfo:
        try:
            _TZ = ZoneInfo(name)
            return
        except Exception:  # noqa: BLE001
            log.warning("Unknown timezone %r, falling back to UTC%+d", name, fallback_offset)
    _TZ = timezone(timedelta(hours=fallback_offset))


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(_TZ or timezone.utc)


def local_str(iso, _tz=None):
    dt = parse_dt(iso)
    if not dt:
        return "TBA"
    return to_local(dt).strftime("%a %-d %b, %-I:%M %p")


def when_str(iso):
    dt = parse_dt(iso)
    if not dt:
        return ""
    d = (dt - datetime.now(timezone.utc)).days
    if d < 0:
        return "passed"
    return "today" if d == 0 else ("tomorrow" if d == 1 else f"in {d}d")


# ----------------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------------

def write_json(events: list, meta: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tickets.json").write_text(
        json.dumps({"meta": meta, "events": events}, indent=2), encoding="utf-8"
    )


def write_brief(events: list, meta: dict, tz: int) -> str:
    lines = [f"{len(events)} ticket drops scoring {meta['threshold']}+ in the next {meta['days']} days."]
    if not events:
        lines = [f"No ticket drops cleared {meta['threshold']} in the next {meta['days']} days."]
    for e in events[:12]:
        who = ", ".join(e["artists"][:2]) or e["name"]
        lines.append("")
        lines.append(f"  {e['score']} - {who}")
        lines.append(f"  {e['venue']}, {e['city']} {e['state']}".rstrip())
        lines.append(f"  On sale {local_str(e['onsale_start'], tz)} ({when_str(e['onsale_start'])})")
        lines.append(f"  Event {local_str(e['event_date'], tz)}")
        lines.append(f"  Face {face_str(e)} - est. resale {resale_str(e)} ({e['confidence']} conf)")
    text = "\n".join(lines)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tickets-brief.md").write_text(text, encoding="utf-8")
    return text


PAGE_CSS = """
*{box-sizing:border-box}
:root{--bg:#F5F6F8;--card:#FFF;--ink:#14181F;--ink2:#515A68;--ink3:#8A94A2;
--rule:#E2E6EB;--hot:#B3341F;--warm:#9A6410;--cool:#2F6B8F;--chipbg:#EEF1F4;
--btn:#FFF;--btnon:#14181F;--btnonink:#FFF}
@media(prefers-color-scheme:dark){:root{--bg:#0F1216;--card:#181C22;--ink:#E8ECF1;
--ink2:#A2ACB9;--ink3:#79838F;--rule:#272E37;--hot:#E4765E;--warm:#D2A054;
--cool:#6FA8CC;--chipbg:#20262E;--btn:#181C22;--btnon:#E8ECF1;--btnonink:#0F1216}}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
.wrap{max-width:720px;margin:0 auto;padding:18px 16px 56px}
header{margin-bottom:14px}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--ink3);font-size:13px}
.controls{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;
background:var(--bg);padding:10px 0 12px;margin-bottom:4px;border-bottom:1px solid var(--rule)}
.crow{display:flex;gap:6px;align-items:center;overflow-x:auto;padding-bottom:6px;
-webkit-overflow-scrolling:touch;scrollbar-width:none}
.crow::-webkit-scrollbar{display:none}
.crow+.crow{padding-bottom:0}
.lab{font-size:11px;color:var(--ink3);text-transform:uppercase;letter-spacing:.06em;
flex:0 0 auto;margin-right:2px}
.btn{flex:0 0 auto;font:inherit;font-size:13px;padding:6px 11px;border-radius:999px;
border:1px solid var(--rule);background:var(--btn);color:var(--ink2);cursor:pointer;
white-space:nowrap;-webkit-tap-highlight-color:transparent}
.btn[aria-pressed="true"]{background:var(--btnon);color:var(--btnonink);border-color:var(--btnon);
font-weight:600}
.btn:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
.sep{flex:0 0 auto;width:1px;height:20px;background:var(--rule);margin:0 3px}
.count{font-size:12.5px;color:var(--ink3);padding:8px 0 2px}
.ev{background:var(--card);border:1px solid var(--rule);border-radius:12px;
padding:14px;margin-bottom:12px;display:grid;grid-template-columns:46px 1fr;gap:12px}
.sc{font-weight:700;font-size:19px;text-align:center;padding-top:1px;font-variant-numeric:tabular-nums}
.s75{color:var(--hot)}.s70{color:var(--warm)}.s60{color:var(--cool)}
.ttl{font-weight:600;font-size:16px;line-height:1.25;margin:0 0 2px}
.ttl a{color:inherit;text-decoration:none}
.vn{color:var(--ink2);font-size:13.5px;margin-bottom:8px}
.when{font-size:14px;font-weight:600}
.ago{color:var(--ink3);font-weight:400}
.ed{font-size:13.5px;color:var(--ink2);margin-top:1px}
.money{font-size:13.5px;color:var(--ink2);font-variant-numeric:tabular-nums;margin-top:3px}
.chips{margin-top:9px;display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.chip{background:var(--chipbg);color:var(--ink2);font-size:11px;padding:3px 7px;
border-radius:5px;white-space:nowrap}
.chip.w{background:var(--hot);color:#fff}
.sg{margin-left:auto;font-size:12px;font-weight:600;text-decoration:none;color:var(--cool);
border:1px solid var(--rule);padding:4px 9px;border-radius:999px;white-space:nowrap}
.sg:hover{border-color:var(--cool)}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tab{flex:1 1 0;font:inherit;font-size:14px;font-weight:600;padding:9px 8px;border-radius:10px;
border:1px solid var(--rule);background:var(--btn);color:var(--ink2);cursor:pointer;
-webkit-tap-highlight-color:transparent}
.tab[aria-selected="true"]{background:var(--btnon);color:var(--btnonink);border-color:var(--btnon)}
.tab:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
.tab .n{font-weight:400;opacity:.65}
.pre{margin-top:10px;padding-top:10px;border-top:1px solid var(--rule)}
.pre h4{margin:0 0 6px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ink3);font-weight:600}
.pre ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:7px}
.pre li{font-size:13px;line-height:1.35}
.pre .pn{font-weight:600;color:var(--ink)}
.pre .pt{color:var(--ink3);font-variant-numeric:tabular-nums}
.pre .pt.live{color:var(--hot);font-weight:600}
.pre .how{color:var(--ink3);font-size:12px;display:block;margin-top:1px}
.pre a.sig{color:var(--cool);font-weight:600;text-decoration:none;font-size:12.5px}
.pre a.sig:hover{text-decoration:underline}
.done{display:flex;align-items:center;gap:8px;margin-top:11px;padding-top:10px;
border-top:1px solid var(--rule);font-size:13px;color:var(--ink2);cursor:pointer;user-select:none}
.done input{width:18px;height:18px;accent-color:var(--cool);flex:0 0 auto}
.ev.signed{opacity:.55}
.acts{display:flex;gap:6px;flex-direction:column;align-items:center;padding-top:2px}
.ico{width:30px;height:30px;border-radius:8px;border:1px solid var(--rule);background:var(--btn);
color:var(--ink3);font-size:14px;line-height:1;cursor:pointer;display:flex;align-items:center;
justify-content:center;padding:0;-webkit-tap-highlight-color:transparent}
.ico:hover{border-color:var(--ink3);color:var(--ink)}
.ico.on{background:var(--hot);border-color:var(--hot);color:#fff}
.ico:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
footer{margin-top:28px;color:var(--ink3);font-size:12px;line-height:1.6}
.empty{background:var(--card);border:1px dashed var(--rule);border-radius:12px;
padding:28px 16px;text-align:center;color:var(--ink3)}
"""


def live_presales(presales: list) -> list:
    """Presales worth acting on: drop anything whose window has already closed,
    and mark the ones open right now. A presale that started two days ago may
    still be running - the end time is what decides, not the start."""
    now = datetime.now(timezone.utc)
    out = []
    for pr in presales:
        start, end = parse_dt(pr["start"]), parse_dt(pr["end"])
        if end and end < now:
            continue
        if start and start <= now:
            when = "live now" + (f" \u00b7 ends {local_str(pr['end'])}" if end else "")
        else:
            when = local_str(pr["start"])
        out.append({"n": pr["name"], "t": when, "u": pr["url"], "how": pr["how"],
                    "live": bool(start and start <= now)})
    # soonest first, but anything already open floats to the top
    out.sort(key=lambda x: (not x["live"],))
    return out


def write_page(events: list, meta: dict, tz=None) -> None:
    """Interactive page: data is embedded and the rows are rendered client-side,
    so sorting and filtering work from a static file with no server."""
    rows = []
    for e in events:
        rows.append({
            "s": e["score"],
            "who": ", ".join(e["artists"][:2]) or e["name"],
            "where": f"{e['venue']}, {e['city']} {e['state']}".strip().strip(","),
            "on": e["onsale_start"],
            "onTxt": local_str(e["onsale_start"]),
            "onAgo": when_str(e["onsale_start"]),
            "onDay": (to_local(parse_dt(e["onsale_start"])).strftime("%Y-%m-%d")
                      if parse_dt(e["onsale_start"]) else ""),
            "ev": e["event_date"],
            "evTxt": local_str(e["event_date"]),
            "face": face_str(e),
            "res": resale_str(e),
            "seg": (e["segment"] or "").lower(),
            "act": (e["artists"][0] if e["artists"] else e["name"]).lower(),
            "actName": e["artists"][0] if e["artists"] else e["name"],
            "id": e["id"],
            "pre": live_presales(e["presales"])[:5],
            "nPre": len(live_presales(e["presales"])),
            "w": bool(e["on_watchlist"]),
            "url": e["url"],
            "sg": e["seatgeek_url"],
            "tags": [r for r in e["reasons"] if r != "on your watchlist"][:2] + [f"{e['confidence']} conf"],
        })

    today_local = to_local(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    watch_topic = os.environ.get("WATCHLIST_TOPIC", "") or load_env(HERE / ".env").get("WATCHLIST_TOPIC", "")

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Ticket Drops</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Drops">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0F1216">
<link rel="apple-touch-icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 180 180\'%3E%3Crect width=\'180\' height=\'180\' fill=\'%2314181F\'/%3E%3Ctext x=\'90\' y=\'124\' font-size=\'104\' text-anchor=\'middle\' fill=\'%23E4765E\' font-family=\'system-ui\'%3E&#127915;%3C/text%3E%3C/svg%3E">
<style>{PAGE_CSS}</style>
</head><body><div class="wrap">
  <header>
    <h1>Ticket drops</h1>
    <div class="sub">{meta['generated_local']} &middot; {len(events)} scoring
      {meta['threshold']}+ of {meta['scanned']} on-sales</div>
  </header>

  <div class="tabs" role="tablist">
    <button class="tab" role="tab" data-tab="today" aria-selected="true">
      On sale today <span class="n" id="nToday"></span></button>
    <button class="tab" role="tab" data-tab="later" aria-selected="false">
      Upcoming &amp; presales <span class="n" id="nLater"></span></button>
  </div>

  <div class="controls">
    <div class="crow" role="group" aria-label="Sort">
      <span class="lab">Sort</span>
      <button class="btn" data-sort="score" aria-pressed="true">Score</button>
      <button class="btn" data-sort="onsale" aria-pressed="false">On sale</button>
      <button class="btn" data-sort="event" aria-pressed="false">Concert date</button>
    </div>
    <div class="crow" role="group" aria-label="Filter">
      <span class="lab">Show</span>
      <button class="btn" data-min="60" aria-pressed="true">60+</button>
      <button class="btn" data-min="70" aria-pressed="false">70+</button>
      <button class="btn" data-min="75" aria-pressed="false">75+</button>
      <span class="sep"></span>
      <button class="btn" data-seg="all" aria-pressed="true">All</button>
      <button class="btn" data-seg="music" aria-pressed="false">Music</button>
      <button class="btn" data-seg="sports" aria-pressed="false">Sports</button>
      <span class="sep"></span>
      <button class="btn" data-watch="1" aria-pressed="false">&#9733; Watchlist</button>
      <button class="btn" data-best="1" aria-pressed="true">Best per act</button>
    </div>
    <div class="crow" role="group" aria-label="Status">
      <span class="lab">Status</span>
      <button class="btn" data-st="active" aria-pressed="true">To do <span id="nA"></span></button>
      <button class="btn" data-st="signed" aria-pressed="false">Signed up <span id="nS"></span></button>
      <button class="btn" data-st="trash" aria-pressed="false">Trashed <span id="nT"></span></button>
    </div>
  </div>

  <div class="count" id="count"></div>
  <div id="list"></div>

  <footer>
    Scores are research, not guarantees. Resale figures are genre-table estimates,
    not live listings &mdash; tap <b>SeatGeek</b> on any row to see what it is actually
    reselling for. Ticket-resale rules vary by state, venue and tour; check
    transferability before buying.
  </footer>
</div>
<script>
const DATA = {json.dumps(rows)};
const TODAY = "{today_local}";
const WTOPIC = "{watch_topic}";
const S = {{tab:"today", sort:"score", min:60, seg:"all", watch:false, best:true, st:"active"}};
try {{ Object.assign(S, JSON.parse(localStorage.getItem("drops:view") || "{{}}")); }} catch (e) {{}}

// which shows you have already registered for, so the upcoming list can shrink
let SIGNED = {{}}, HIDDEN = {{}}, WATCH = {{}};
const read = (k, d) => {{ try {{ return JSON.parse(localStorage.getItem(k) || d); }} catch (e) {{ return JSON.parse(d); }} }};
SIGNED = read("drops:signedup", "{{}}");
HIDDEN = read("drops:hidden",   "{{}}");
WATCH  = read("drops:watch",    "{{}}");
const save = (k, v) => {{ try {{ localStorage.setItem(k, JSON.stringify(v)); }} catch (e) {{}} }};
const saveSigned = () => save("drops:signedup", SIGNED);

// Push watchlist edits back to the tracker. The daily job folds these into
// watchlist.json, which is what makes the act actually get fetched and scored.
function pushWatch(name, on) {{
  if (!WTOPIC) return;
  const body = JSON.stringify(on ? {{add: [name]}} : {{remove: [name]}});
  try {{
    fetch("https://ntfy.sh/" + WTOPIC, {{method: "POST", body: body}}).catch(() => {{}});
  }} catch (e) {{}}
}}
const onToday = d => d.onDay === TODAY;  // both are LOCAL calendar dates

const esc = t => String(t == null ? "" : t).replace(/[&<>"]/g,
  c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]);
const FAR = "9999";

function view() {{
  let r = DATA.filter(d => d.s >= S.min
    && (S.seg === "all" || d.seg === S.seg)
    && (!S.watch || d.w)
    && (S.tab === "today" ? onToday(d) : !onToday(d))
    && (S.st === "trash" ? !!HIDDEN[d.act]
        : !HIDDEN[d.act] && (S.st === "signed" ? !!SIGNED[d.id] : !SIGNED[d.id])));
  if (S.best) {{
    const keep = new Map();
    for (const d of r) {{
      const cur = keep.get(d.act);
      if (!cur) keep.set(d.act, Object.assign({{}}, d, {{more: 0}}));
      else {{
        cur.more++;
        if (d.s > cur.s) {{ const m = cur.more; keep.set(d.act, Object.assign({{}}, d, {{more: m}})); }}
      }}
    }}
    r = [...keep.values()];
  }}
  r.sort((a, b) =>
    (S.sort === "score"  ? b.s - a.s || (a.on || FAR).localeCompare(b.on || FAR)
  : S.sort === "onsale" ? (a.on || FAR).localeCompare(b.on || FAR) || b.s - a.s
  :                       (a.ev || FAR).localeCompare(b.ev || FAR) || b.s - a.s));
  return r;
}}

function counts() {{
  const base = DATA.filter(d => d.s >= S.min
    && (S.seg === "all" || d.seg === S.seg) && (!S.watch || d.w));
  const inTab = base.filter(d => S.tab === "today" ? onToday(d) : !onToday(d));
  const vis = base.filter(d => !HIDDEN[d.act]);
  document.getElementById("nToday").textContent = vis.filter(onToday).length;
  document.getElementById("nLater").textContent = vis.filter(d => !onToday(d)).length;
  const acts = x => new Set(x.map(d => d.act)).size;
  document.getElementById("nA").textContent = inTab.filter(d => !HIDDEN[d.act] && !SIGNED[d.id]).length;
  document.getElementById("nS").textContent = inTab.filter(d => !HIDDEN[d.act] && SIGNED[d.id]).length;
  document.getElementById("nT").textContent = acts(inTab.filter(d => HIDDEN[d.act]));
}}

function presaleBlock(d) {{
  if (S.tab !== "later" || !d.pre.length) return "";
  const items = d.pre.map(p => {{
    const link = p.u
      ? ' <a class="sig" href="' + esc(p.u) + '" target="_blank" rel="noopener">Sign up \u2197</a>'
      : "";
    const how = (!p.u && p.how) ? '<span class="how">' + esc(p.how) + '</span>' : "";
    const t = p.live
      ? '<span class="pt live">\u00b7 ' + esc(p.t) + '</span>'
      : '<span class="pt">\u00b7 ' + esc(p.t) + '</span>';
    return '<li><span class="pn">' + esc(p.n) + '</span> ' + t + link + how + '</li>';
  }}).join("");
  const extra = d.nPre > d.pre.length
    ? '<li class="pt">+' + (d.nPre - d.pre.length) + ' more presale(s)</li>' : "";
  return '<div class="pre"><h4>Presales \u2014 register before the on-sale</h4><ul>'
    + items + extra + '</ul></div>';
}}

function render() {{
  counts();
  const r = view();
  const label = S.sort === "score" ? "highest score first"
              : S.sort === "onsale" ? "soonest on-sale first" : "closest concert first";
  document.getElementById("count").textContent =
    r.length + (r.length === 1 ? " drop" : " drops") + " · " + label;

  document.getElementById("list").innerHTML = r.length ? r.map(d => {{
    const cls = d.s >= 75 ? "s75" : d.s >= 70 ? "s70" : "s60";
    const signed = (S.tab === "later" && SIGNED[d.id]) ? " signed" : "";
    const title = d.url
      ? '<a href="' + esc(d.url) + '" target="_blank" rel="noopener">' + esc(d.who) + '</a>'
      : esc(d.who);
    const more = d.more ? '<span class="chip">+' + d.more + ' more date'
      + (d.more > 1 ? 's' : '') + '</span>' : "";
    const chips = (d.w ? '<span class="chip w">watchlist</span>' : "") + more
      + d.tags.map(t => '<span class="chip">' + esc(t) + '</span>').join("");
    return '<article class="ev' + signed + '">'
      + '<div><div class="sc ' + cls + '">' + d.s + '</div>'
      + '<div class="acts">'
      +   '<button class="ico' + (WATCH[d.act] ? ' on' : '') + '" data-star="' + esc(d.act)
      +     '" data-starname="' + esc(d.actName)
      +     '" title="Watchlist this act" aria-label="Watchlist this act">\u2605</button>'
      +   '<button class="ico' + (HIDDEN[d.act] ? ' on' : '') + '" data-trash="' + esc(d.act)
      +     '" title="Hide this act" aria-label="Hide this act">'
      +     (HIDDEN[d.act] ? '\u21ba' : '\u2715') + '</button>'
      + '</div></div><div>'
      + '<p class="ttl">' + title + '</p>'
      + '<div class="vn">' + esc(d.where) + '</div>'
      + '<div class="when">On sale ' + esc(d.onTxt)
      + ' <span class="ago">· ' + esc(d.onAgo) + '</span></div>'
      + '<div class="ed">Concert ' + esc(d.evTxt) + '</div>'
      + '<div class="money">Face ' + esc(d.face) + ' · est. resale ' + esc(d.res) + '</div>'
      + '<div class="chips">' + chips
      + '<a class="sg" href="' + esc(d.sg) + '" target="_blank" rel="noopener">SeatGeek ↗</a>'
      + '</div>'
      + presaleBlock(d)
      + (S.tab === "later"
          ? '<label class="done"><input type="checkbox" data-sign="' + esc(d.id) + '"'
            + (SIGNED[d.id] ? " checked" : "") + '> Signed up</label>'
          : "")
      + '</div></article>';
  }}).join("") : '<div class="empty">' + (S.tab === "today"
      ? "Nothing goes on sale today at this filter."
      : S.st === "signed" ? "Nothing signed up for yet. Tick \u201cSigned up\u201d on a card."
      : S.st === "trash"  ? "Nothing trashed. Tap \u2715 on an act to hide it."
      : "Nothing upcoming matches that filter.") + '</div>';

  document.querySelectorAll("[data-star]").forEach(b => {{
    b.addEventListener("click", () => {{
      const a = b.dataset.star, on = !WATCH[a];
      if (on) WATCH[a] = true; else delete WATCH[a];
      save("drops:watch", WATCH);
      pushWatch(b.dataset.starname, on);  // proper-cased name, not the lookup key
      render();
    }});
  }});
  document.querySelectorAll("[data-trash]").forEach(b => {{
    b.addEventListener("click", () => {{
      const a = b.dataset.trash;
      if (HIDDEN[a]) delete HIDDEN[a]; else HIDDEN[a] = true;
      save("drops:hidden", HIDDEN);
      render();
    }});
  }});
  document.querySelectorAll("[data-sign]").forEach(cb => {{
    cb.addEventListener("change", () => {{
      if (cb.checked) SIGNED[cb.dataset.sign] = true; else delete SIGNED[cb.dataset.sign];
      saveSigned();
      render();
    }});
  }});
}}

function wire(attr, apply) {{
  document.querySelectorAll("[data-" + attr + "]").forEach(b => {{
    b.addEventListener("click", () => {{
      apply(b.dataset[attr]);
      const toggle = (attr === "watch" || attr === "best");
      if (toggle) b.setAttribute("aria-pressed", String(attr === "watch" ? S.watch : S.best));
      else document.querySelectorAll("[data-" + attr + "]").forEach(o =>
        o.setAttribute("aria-pressed", String(o === b)));
      try {{ localStorage.setItem("drops:view", JSON.stringify(S)); }} catch (e) {{}}
      render();
    }});
  }});
}}
wire("sort",  v => S.sort = v);
wire("min",   v => S.min = +v);
wire("seg",   v => S.seg = v);
wire("watch", () => S.watch = !S.watch);
wire("best",  () => S.best = !S.best);
wire("st",    v => S.st = v);

document.querySelectorAll("[data-tab]").forEach(b => {{
  b.addEventListener("click", () => {{
    S.tab = b.dataset.tab;
    document.querySelectorAll("[data-tab]").forEach(o =>
      o.setAttribute("aria-selected", String(o === b)));
    try {{ localStorage.setItem("drops:view", JSON.stringify(S)); }} catch (e) {{}}
    render();
  }});
}});
document.querySelectorAll("[data-tab]").forEach(b =>
  b.setAttribute("aria-selected", String(b.dataset.tab === S.tab)));

// reflect restored state on the buttons
document.querySelectorAll("[data-sort]").forEach(b =>
  b.setAttribute("aria-pressed", String(b.dataset.sort === S.sort)));
document.querySelectorAll("[data-min]").forEach(b =>
  b.setAttribute("aria-pressed", String(+b.dataset.min === S.min)));
document.querySelectorAll("[data-seg]").forEach(b =>
  b.setAttribute("aria-pressed", String(b.dataset.seg === S.seg)));
document.querySelectorAll("[data-watch]").forEach(b =>
  b.setAttribute("aria-pressed", String(S.watch)));
document.querySelectorAll("[data-best]").forEach(b =>
  b.setAttribute("aria-pressed", String(S.best)));
document.querySelectorAll("[data-st]").forEach(b =>
  b.setAttribute("aria-pressed", String(b.dataset.st === S.st)));

render();
</script>
</body></html>
"""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / PAGE_NAME).write_text(page, encoding="utf-8")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def sync_watchlist(env: dict) -> None:
    """Pull watchlist edits the phone pushed to the public watchlist topic and
    fold them into watchlist.json.

    Deliberately ADDITIVE: a message can add names or name removals, but cannot
    replace the file wholesale. The topic is embedded in the published page, so
    anyone who finds the page could post to it - additive-only means the worst
    case is junk entries you can delete, not a wiped watchlist.
    """
    topic = env.get("WATCHLIST_TOPIC", "").strip()
    if not topic:
        log.info("No WATCHLIST_TOPIC set - skipping watchlist sync")
        return

    url = f"{NTFY_BASE}/{topic}/json?poll=1"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("Watchlist sync: could not read topic (%s)", e)
        return

    add, remove = [], []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("event") != "message":
            continue
        try:
            payload = json.loads(msg.get("message") or "{}")
        except json.JSONDecodeError:
            continue
        add += [a for a in payload.get("add", []) if isinstance(a, str)]
        remove += [a for a in payload.get("remove", []) if isinstance(a, str)]

    def clean(n):
        return " ".join(str(n).split())[:80]

    add = [clean(a) for a in add if clean(a)]
    remove = {clean(a).lower() for a in remove if clean(a)}
    if not add and not remove:
        log.info("Watchlist sync: nothing pending")
        return

    wl = load_json(HERE / "watchlist.json", {"artists": []})
    artists = wl.get("artists", [])
    have = {a["name"].lower() for a in artists}

    added = 0
    for name in add:
        if name.lower() in have or name.lower() in remove:
            continue
        if len(artists) >= 50:
            log.warning("Watchlist is at the 50-artist cap - ignoring %r", name)
            break
        artists.append({"name": name, "aliases": [],
                        "added": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        "why": "Added from the tickets page."})
        have.add(name.lower())
        added += 1

    before = len(artists)
    artists = [a for a in artists if a["name"].lower() not in remove]
    removed = before - len(artists)

    if added or removed:
        wl["artists"] = artists
        (HERE / "watchlist.json").write_text(json.dumps(wl, indent=2) + "\n", encoding="utf-8")
        log.info("Watchlist sync: +%d, -%d (now %d artists)", added, removed, len(artists))
    else:
        log.info("Watchlist sync: nothing changed")


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily ticket-drop tracker")
    ap.add_argument("--dry-run", action="store_true", help="write files, send no notifications")
    ap.add_argument("--test-ping", action="store_true", help="send one test notification and exit")
    ap.add_argument("--no-state", action="store_true", help="treat every surfaced event as new")
    ap.add_argument("--out-dir", default="out",
                    help="where to write the page and data (default: out)")
    ap.add_argument("--page-name", default="tickets.html",
                    help="filename for the page (use index.html for GitHub Pages)")
    ap.add_argument("--sync-watchlist", action="store_true",
                    help="fold phone-pushed watchlist edits into watchlist.json, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    global OUT, PAGE_NAME
    out_arg = Path(args.out_dir)
    OUT = out_arg if out_arg.is_absolute() else HERE / out_arg
    PAGE_NAME = args.page_name

    setup_logging(args.verbose)
    # In CI there is no .env - the secrets arrive as real environment variables.
    env = {**load_env(HERE / ".env"), **{k: v for k, v in os.environ.items() if v}}
    cfg = load_json(HERE / "config.json", {})
    if not cfg:
        log.error("config.json missing or unreadable")
        return 1

    topic = env.get("NTFY_TOPIC", "").strip()
    if not topic:
        log.error("NTFY_TOPIC is not set in .env")
        return 1

    if args.sync_watchlist:
        sync_watchlist(env)
        return 0

    if args.test_ping:
        ok = ntfy_post(
            topic,
            "Ticket drops: test",
            "Your ticket-drop tracker is wired up correctly.\n"
            "Real alerts carry artist, venue, on-sale time and est. face vs resale.",
            tags="ticket,white_check_mark", priority="default",
        )
        log.info("Test notification %s (topic %s)", "sent" if ok else "FAILED", topic)
        return 0 if ok else 1

    api_key = env.get("TICKETMASTER_API_KEY", "").strip()
    if not api_key:
        log.error("TICKETMASTER_API_KEY is not set in .env - nothing to pull.")
        return 1

    set_timezone(cfg.get("timezone", "America/Denver"),
                 int(cfg.get("fallback_utc_offset_hours", -7)))
    tz = None
    days = int(cfg.get("lookahead_days", 14))
    threshold = int(cfg.get("score_threshold", 60))
    page_size = int(cfg.get("page_size", 200))
    max_pages = int(cfg.get("max_pages", 5))
    country = cfg.get("country", "US")

    watchlist = load_json(HERE / "watchlist.json", {"artists": []})
    watch_names = set()
    for a in watchlist.get("artists", []):
        watch_names.add(a["name"].lower())
        for alias in a.get("aliases", []):
            watch_names.add(alias.lower())

    # --- pull
    raw = []
    for seg in cfg.get("segments", ["music", "sports"]):
        try:
            raw += fetch_onsales(api_key, seg, days, page_size, max_pages, country)
        except RuntimeError as e:
            log.error("%s pull failed: %s", seg, e)
            if "API key" in str(e):
                return 1

    # --- watchlist pass, separate from the window
    eb_token = env.get("EVENTBRITE_TOKEN", "").strip()
    for a in watchlist.get("artists", []):
        names = [a["name"]] + a.get("aliases", [])
        for n in names:
            try:
                got = fetch_attraction_events(api_key, n, 50)
            except RuntimeError as e:
                log.warning("watchlist pull failed for %r: %s", n, e)
                got = []
            if got:
                raw += got
                break
            time.sleep(0.25)
        if eb_token:
            fetch_eventbrite(eb_token, a["name"])

    # --- dedupe, normalise, score
    seen_ids, norm = set(), []
    for ev in raw:
        if ev.get("id") in seen_ids:
            continue
        seen_ids.add(ev.get("id"))
        norm.append(normalise(ev))

    scored = [score_event(e, cfg, watch_names, None) for e in norm]
    surfaced = [e for e in scored if e["score"] >= threshold]
    surfaced.sort(key=lambda e: (e["onsale_start"] or "9999", -e["score"]))
    log.info("Scanned %d events, %d scored %d+", len(scored), len(surfaced), threshold)

    # --- what's actually new
    state = {} if args.no_state else load_json(STATE_PATH, {})
    known = state.get("notified", {})
    fresh = [e for e in surfaced if e["id"] not in known]
    log.info("%d of those are new since the last run", len(fresh))

    # --- outputs
    now_local = to_local(datetime.now(timezone.utc)).strftime("%a %-d %b, %-I:%M %p")
    meta = {
        "generated_utc": iso_z(datetime.now(timezone.utc)),
        "generated_local": now_local,
        "scanned": len(scored), "surfaced": len(surfaced), "new": len(fresh),
        "threshold": threshold, "days": days,
        "seatgeek": bool(env.get("SEATGEEK_CLIENT_ID", "").strip()),
    }
    write_json(surfaced, meta)
    write_page(surfaced, meta, tz)
    brief = write_brief(surfaced, meta, tz)
    log.info("Wrote %s/{tickets.json,%s,tickets-brief.md}", OUT, PAGE_NAME)

    # --- notify: push is a tighter filter than the page. One push per ARTIST,
    # best-scoring date, because five pushes for the same act on one tour is noise.
    notify_at = int(cfg.get("notify_threshold", cfg.get("score_threshold", 60)))
    by_artist, extra_dates = {}, {}
    for e in sorted(fresh, key=lambda x: -x["score"]):
        if e["score"] < notify_at:
            continue
        key = (e["artists"][0] if e["artists"] else e["name"]).lower()
        if key in by_artist:
            extra_dates[key] = extra_dates.get(key, 0) + 1
        else:
            by_artist[key] = e
    to_send = sorted(by_artist.values(), key=lambda e: (e["onsale_start"] or "9999", -e["score"]))
    log.info("%d new >= %d, %d after collapsing to one per artist",
             sum(1 for e in fresh if e["score"] >= notify_at), notify_at, len(to_send))

    cap = int(cfg.get("max_notifications_per_run", 6))
    for e in to_send[:cap]:
        who = ", ".join(e["artists"][:2]) or e["name"]
        title = f"[{e['score']}] {who}"
        body = (
            f"{e['venue']}, {e['city']} {e['state']}\n"
            f"On sale {local_str(e['onsale_start'], tz)} ({when_str(e['onsale_start'])})\n"
            f"Face {face_str(e)} -> est. resale {resale_str(e)} ({e['confidence']} conf)"
        )
        n_extra = extra_dates.get((e["artists"][0] if e["artists"] else e["name"]).lower(), 0)
        if n_extra:
            body += f"\n+{n_extra} more date(s) for this act"
        tags = "ticket,fire" if e["score"] >= 75 else "ticket"
        ntfy_post(topic, title, body, tags=tags, click=e["url"],
                  actions=f"view, SeatGeek, {e['seatgeek_url']}",
                  priority="high" if e["score"] >= 75 else "default", dry=args.dry_run)
        time.sleep(0.3)

    if len(to_send) > cap:
        ntfy_post(topic, f"+{len(to_send) - cap} more acts",
                  f"{len(to_send)} acts scored {notify_at}+ today, {len(surfaced)} events cleared "
                  f"{threshold}+. Full list on the tickets page.",
                  tags="ticket", dry=args.dry_run)

    # --- digest for the morning brief to read back off the topic
    if not args.dry_run:
        ntfy_post(f"{topic}-brief", f"Tickets digest {datetime.now().strftime('%Y-%m-%d')}",
                  brief, tags="memo")

    # --- persist
    if not args.dry_run:
        known.update({e["id"]: {"score": e["score"], "seen": meta["generated_utc"]} for e in surfaced})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        known = {k: v for k, v in known.items() if v.get("seen", "") >= cutoff}
        STATE_PATH.write_text(json.dumps({"notified": known}, indent=2), encoding="utf-8")

    log.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
